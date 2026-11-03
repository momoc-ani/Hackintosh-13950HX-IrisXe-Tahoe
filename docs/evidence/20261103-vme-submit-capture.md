# VA 提交边界与缓冲区取证（2026-11-03）

## 结论

已核对候选 `AppleIntelTGLGraphicsVADriver` 的提交前 manager 布局，并扩展进程内诊断 interpose：默认保持原行为；`XE_VME_STOP_BEFORE_SUBMIT=1` 在提交前保存 manager 与已用缓冲区并退出；`XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 放行空初始化提交，在遇到缓存 class 或非空 command 区时保存快照并以退出码 77 停止。两种模式必须且只能启用一种。

本次只完成静态字段核对、CPU 夹具和严格编译，没有启动新的硬件编码探针。此前 `Usage=1` 捕获文件验证了 VA 调用点 interpose 和 manager 前缀读取；新增的 command/sideband 实际字节读取尚无新的运行时快照。Usage=0 普通 VME/PAK 编码仍未修复或验收。系统 VA、EFI、NootedGreen、NootRX 和安装目录均未修改。

## 已核对字段

检查对象为 `build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver`。Ghidra 12.1.4 与 `otool` 的反汇编共同确认：`0xA8F6` 调用 `_IOAccelVideoContextSubmitDataBuffers`，返回地址 `0xA8FB`；`0xA893` 的首参数是 manager，进入时保存在 `R14`。

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

`0xA9DC` 每次 flush 会先向 sideband 写入一个 8 字节控制记录。因此 sideband 有已用字节本身不作为“存在编码命令”的判据；显式首个非空模式只在 class 数非零或 command 游标超过 command 基址时停止。停止快照会同时复制 command 与 sideband 已用区间。总复制上限为 64 MiB，指针次序无效、长度越界或复制不完整都会拒绝快照。`+0x80` 指针被读取作为 sideband 数据基址；不再把它所指对象当作未知结构解析。

调用顺序仍是关键限制：`0xA893` 在提交前使用已缓存 class 数；只有原始提交成功后才重新取得新 class/resource 并更新 `+0x58`。所以快照记录的是提交时 VA 暴露的 command 和 sideband 已用范围，不保证涵盖提交返回后才产生的所有 kernel 资源状态。

## Usage=0 风险边界

现有 Usage=0 证据在普通路径的首次 `FrameStatisticsContext` 调度前截停时观察到 command 构建槽尚为零；这表示命令尚未由原调度函数构造。继续该调度后，已有一次 Usage=0 探针发生 RCS GPU reset，详见 `docs/evidence/20261103-usage0-remap-diagnostic.md` 及 `docs/evidence/20261103-usage0-frame-statistics-validation.json`。

本提交拦截点位于 VA 原调度之内的 IOAccel 提交边界。若仍启用 `XE_VA_STOP_BEFORE_SCHEDULE=1`，执行会先在更早的调度入口退出，因而到不了本 interpose；若移除该保护，原调度和可能导致 reset 的前置任务会先运行。故本轮没有把 Usage=0 探针放行到这个钩子。继续硬件采集前，需要先让 schedule-level 观察器能在经过已验证的单一前置任务后，于后续危险调度前停止；否则该提交钩子不能单独作为 Usage=0 的保护边界。

## 验证

- 之前的受控 `Usage=1` 运行在 `0xA8F6` 命中，退出码 77，输出 `build/validation-encode-and-kext-icons-20261102/vme-submit-prefix-usage1-20261103/manager-15298.bin` 与 `submit-events-15298.jsonl`。它只验证旧版 manager 前缀捕获；不能证明本版 command/sideband 复制已在真实提交中运行。
- 新增 CPU 夹具覆盖 command 与 sideband 范围、仅有 sideband 控制记录、空初始化提交、反向游标、超预算、部分复制、原始前缀不变和两种互斥模式。
- `tools/diagnostics/gpu-tests/test-*.py` 的 11 个脚本、50 项测试全部通过。
- 捕获 interpose 与 VA 诊断源码使用 `clang -arch x86_64 -mmacosx-version-min=15.0 -dynamiclib -O2 -Wall -Wextra -Werror -undefined dynamic_lookup` 严格编译通过；产物位于 `build/validation-encode-and-kext-icons-20261102/xe-va-vme-submit-capture-nonempty-lab.dylib`，`__DATA_CONST,__interpose` 段存在。
