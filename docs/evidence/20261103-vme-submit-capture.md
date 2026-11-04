# VA 提交边界与缓冲区取证（2026-11-03）

## 结论

已核对候选 `AppleIntelTGLGraphicsVADriver` 的提交前 manager 布局，并扩展进程内诊断 interpose：默认保持原行为；`XE_VME_STOP_BEFORE_SUBMIT=1` 在提交前保存 manager 与已用缓冲区并退出；`XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 放行空初始化提交及严格识别的空 control flush，在遇到非零 submit 控制字段、缓存 class、command 字节或未知 sideband 时保存快照并以退出码 77 停止。两种模式必须且只能启用一种。

本次只完成静态字段核对、CPU 夹具和严格编译，没有启动新的硬件编码探针。此前 `Usage=1` 捕获文件验证了 VA 调用点 interpose 和 manager 前缀读取；新增的 command/sideband 实际字节读取尚无新的运行时快照。Usage=0 普通 VME/PAK 编码仍未修复或验收。系统 VA、EFI、NootedGreen、NootRX 和安装目录均未修改。

## 已核对字段

检查对象为 `build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver`。Ghidra 12.1.4 与 `otool` 的反汇编共同确认：`0xA8F6` 调用 `_IOAccelVideoContextSubmitDataBuffers`，返回地址 `0xA8FB`；`0xA893` 的首参数是 manager，进入时保存在 `R14`。

REA 的直接引用显示该导入函数唯一已解析的指令调用点为 `0xA8F6`；`0xA893` 的已解析直接 caller 是 `0xA9DC`。`0xA9DC` 的六个已解析直接 caller 包括 context 初始化 `0x22504`，以及缓冲区分配/调度函数 `0x22708`、`0x227E0`、`0x646A0`、`0x64DF0`、`0x65070`。这收窄了已知静态路径，但 unresolved indirect calls 与其他 runtime 提交路径仍未知，不能把直接引用表当作全运行时证明。

`0x22708`、`0xA9DC` 与 `0xA893` 的字段访问对应如下：

| manager 偏移 | 观察到的用途 |
|---|---|
| `+0x10` | command buffer 起始地址 |
| `+0x18` | command buffer 结束地址 |
| `+0x20` | command buffer 当前写入游标 |
| `+0x58` | 上次提交后缓存的 data-buffer class 数量 |
| `+0x60` | sideband 当前写入游标 |
| `+0x68` | sideband 结束地址 |
| `+0x80` | `IOAccelVideoContextGetSidebandBuffer` 返回的 sideband 基址指针 |

`0xA9DC` 只有在 command 当前游标非空时才写缓冲区：先按需要补 4 个零字节使游标对齐，再追加 8 个零字节，随后写入一条 8 字节 type-2 sideband 记录。command 游标为空时，这些写入会整体跳过；这与旧版 `Usage=1` manager 快照中的零 command/sideband 游标相符，但该快照未记录调用者，不能据此确认具体调用路径。成功提交后，`0xA893` 将 sideband 头部写为 `0x20100` 并把游标重置到基址 `+0x18`。

因此不能把任何 sideband 字节都当作空记录，也不能把 `0xA9DC` 追加的零尾标本身误认作有效 command。当前 `XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 将非零 submit request/flags/fault、缓存 class、command 或 sideband 已用字节视为待判数据；仅当这些控制字段与 class 数全为零、command 恰为 8 或 12 个零字节，且 sideband 恰为 32 字节、头部/计数/type-2 记录与 command dword 数全部匹配时，才把它识别为该空 control flush 并转发原提交。其他 sideband 形状或内容一律保存并在原 IOAccel submit 前退出。快照总复制上限为 64 MiB；指针次序无效、长度越界或复制不完整都会拒绝快照。`+0x80` 指针只作为 sideband 数据基址，不解析其所指对象的其他结构。

调用顺序仍是关键限制：`0xA893` 在提交前使用已缓存 class 数；只有原始提交成功后才重新取得新 class/resource 并更新 `+0x58`。所以快照记录的是提交时 VA 暴露的 command 和 sideband 已用范围，不保证涵盖提交返回后才产生的所有 kernel 资源状态。

## Usage=0 风险边界

现有 Usage=0 证据在普通路径的首次 `FrameStatisticsContext` 调度前截停时观察到 command 构建槽尚为零；这表示命令尚未由原调度函数构造。继续该调度后，已有一次 Usage=0 探针发生 RCS GPU reset，详见 `docs/evidence/20261103-usage0-remap-diagnostic.md` 及 `docs/evidence/20261103-usage0-frame-statistics-validation.json`。

本提交拦截点位于 VA 原调度之内的 IOAccel 提交边界。若仍启用 `XE_VA_STOP_BEFORE_SCHEDULE=1`，执行会先在更早的调度入口退出，因而到不了本 interpose；若移除该保护，原调度和可能导致 reset 的前置任务会先运行。故本轮没有把 Usage=0 探针放行到这个钩子。继续硬件采集前，需要先让 schedule-level 观察器能在经过已验证的单一前置任务后，于后续危险调度前停止；否则该提交钩子不能单独作为 Usage=0 的保护边界。

为使后续受控探针能到达提交钩子，`xe-va-context-trace.c` 增加了默认关闭的 `XE_VA_SKIP_SCHEDULE_TRACE=1`。该选项只阻止诊断库替换 VA 函数表的 schedule 表项；context 与参数追踪、显式 Usage=0 映射仍独立运行。组合方式是启用 `XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 和 `XE_VA_SKIP_SCHEDULE_TRACE=1`，不设置 `XE_VA_STOP_BEFORE_SCHEDULE`；精确识别的空 control flush 会转发，其后的首个未知或非空提交会在调用原 IOAccel submit 前保存并退出。

REA 在当前 VA 中观察到 `_IOAccelVideoContextSubmitDataBuffers` 的一个直接调用点 `0xA8F6`，但 `all_runtime_submission_paths_proven=false`。因此，即使此组合把已知调用点截在原提交前，也还没有静态证明不存在其他调度/提交入口；本次只编译和做 CPU 测试，不据此宣称硬件执行绝对不会发生，也不运行它。

## 验证

- 之前的受控 `Usage=1` 运行在 `0xA8F6` 命中，退出码 77，输出 `build/validation-encode-and-kext-icons-20261102/vme-submit-prefix-usage1-20261103/manager-15298.bin` 与 `submit-events-15298.jsonl`。它只验证旧版 manager 前缀捕获；不能证明本版 command/sideband 复制已在真实提交中运行。
- 新增 CPU 夹具覆盖 command 与 sideband 范围、未知 sideband fail-closed、非零 request/flags/fault、8/12 字节空 control flush 签名、非零 command/class/错误 sideband 记录拒绝、空初始化提交、反向游标、超预算、部分复制、原始前缀不变和两种互斥模式。
- `test-va-context-trace.py` 覆盖 schedule 包装默认启用、设为 `0` 仍启用、设为 `1` 才跳过；跳过选项尚未在真实 VA 函数表上做运行时验证。
- `tools/diagnostics/gpu-tests/test-*.py` 的 11 个脚本、50 项测试全部通过；本轮新测试先分别以退出码 97（未知 sideband 被当作空）、104（空 flush 判据缺失）和 120（非零 request 未被视为数据）失败，修正后通过。
- 捕获 interpose 与 VA 诊断源码使用 `clang -arch x86_64 -mmacosx-version-min=15.0 -dynamiclib -O2 -Wall -Wextra -Werror -undefined dynamic_lookup` 严格编译通过；产物位于 `build/validation-encode-and-kext-icons-20261102/xe-va-vme-submit-capture-nonempty-lab.dylib`，`__DATA_CONST,__interpose` 段存在。
