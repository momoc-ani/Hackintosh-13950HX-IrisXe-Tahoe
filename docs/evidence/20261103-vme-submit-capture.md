# VA 提交边界与缓冲区取证（2026-11-03）

## 结论

已核对候选 `AppleIntelTGLGraphicsVADriver` 的提交前 manager 布局，并扩展进程内诊断 interpose：默认保持原行为；`XE_VME_STOP_BEFORE_SUBMIT=1` 在提交前保存 manager 与已用缓冲区并退出；`XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 放行空初始化提交及严格识别的空 control flush，在遇到非零 submit 控制字段、缓存 class、command 字节或未知 sideband 时保存快照并以退出码 77 停止。两种模式必须且只能启用一种。

本轮先完成静态字段复核、CPU 夹具和严格编译；随后已有一次受控 Usage=0 捕获，结果记录在 `build/validation-encode-and-kext-icons-20261102/vme-submit-usage0-nonempty-20261104/`。该次只截取提交前快照，未调用被截获的原 IOAccel submit，因此不能算普通 VME/PAK 硬编码通过。系统 VA、EFI、NootedGreen、NootRX 和安装目录均未修改。

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

本提交拦截点位于 VA 原调度之内的 IOAccel 提交边界。若启用 `XE_VA_STOP_BEFORE_SCHEDULE=1`，执行会先在更早的调度入口退出，因而到不了本 interpose；本次受控运行跳过该诊断 wrapper，VA 构建执行到提交点。日志记录了三个空 submit 被转发，随后首个有 class/command/sideband 的提交被保存并在原 IOAccel submit 前退出。它验证的是该次运行的已知提交边界，不涵盖其他尚未解析的间接提交路径，也不能单独解释既往 RCS reset 的原因。

为使后续受控探针能到达提交钩子，`xe-va-context-trace.c` 增加了默认关闭的 `XE_VA_SKIP_SCHEDULE_TRACE=1`。该选项只阻止诊断库替换 VA 函数表的 schedule 表项；context 与参数追踪、显式 Usage=0 映射仍独立运行。组合方式是启用 `XE_VME_CAPTURE_NONEMPTY_SUBMIT=1` 和 `XE_VA_SKIP_SCHEDULE_TRACE=1`，不设置 `XE_VA_STOP_BEFORE_SCHEDULE`；精确识别的空 control flush 会转发，其后的首个未知或非空提交会在调用原 IOAccel submit 前保存并退出。

REA 在当前 VA 中观察到 `_IOAccelVideoContextSubmitDataBuffers` 的一个直接调用点 `0xA8F6`，但 `all_runtime_submission_paths_proven=false`。本次已有捕获证明这个已知调用点可以在原提交前截停，不证明不存在其他调度或提交入口；不能据此声称所有硬件执行路径都被拦截。

## 2026-11-04 Usage=0 捕获

现有目录包含 `submit-events-48718.jsonl` 与 `submit-48718.bin`。事件记录显示 interpose 命中已校验的 VA 调用点，当前提交为 `request=0`、`flags=0`、`fault=0`、`cached_class_count=1`；快照共 632 字节，其中 manager 前缀 136 字节、command 296 字节、sideband 200 字节。stderr 先记录了 3 次无缓存 class、无已用缓冲区的空 submit 放行，之后捕获该非空提交。

stderr 还显示 Usage=0 请求中的 `kind=32, capability=0x4000` 被映射到 `0x400`，其 context 请求返回 0；同一映射下 `parameter=11` 返回状态 5，后续参数 5、7 返回 0。提交快照发生在第一个非空 submit 调用内，捕获器写文件后以退出码 77 结束进程，所以该 296 字节 command 与 200 字节 sideband 没有提交给原 IOAccel API。stdout 到 `prepare=0` 为止，没有 `submit=0`、编码回调或码流报告；目录中也没有 `.h264` 或 `.nv12` 输出。它证明了当前已知拦截点能取得一份真实待提交缓冲区，不证明该缓冲区对应的完整编码阶段，也没有产出或验证 bitstream。

离线读取 sideband 尾部最后六个 dword 得到 `0, 0x38000, 0, 1, 0x200, 0x4a`。VA 的 `0x227E0` 在调用 `0xA9DC` 前按顺序写入 `0x38000`、计数字段和 event slot；`0xA9DC` 随后追加 type-2 控制记录与 command dword 数。`0x22604` 的静态逻辑是在 `_IOAccelDeviceTestEvent` 成功后返回 event slot；捕获中的 slot 值为 1。因此该尾部与 `0x227E0` 事件 flush 的结构一致，推测这次命中了该路径。捕获器未记录 `0xA9DC` 的调用者栈，这仍是结构相关推断，不能当作直接运行时 caller 观测。

再把捕获文件中 296 字节 command 与 `usage0-command-state-remap-20261103/final-cpu-builder.json` 的 288 字节 CPU 统计 batch 按 little-endian dword 比较：72 个构造 dword 中 70 个逐项相同，差异仅在 dword 23（CPU 夹具 `1`、捕获 `0x1001`）和 dword 25（CPU 夹具 `1`、捕获 `0x3001`）；末尾多出的 8 字节全零与 `0xA9DC` 的对齐/零尾标记相符。CPU-only 字段扰动及 Ghidra 反编译将这两个差异追到 context 字节偏移 `+0x11A0`、`+0x190`：`0x2C84A` 分别读取这两个字段并 OR `1`，再写入它构造的状态数据。`0x2CC16` 另会读取资源对象的 `+0x100` 字段，但现有证据没有把这些值与 dword 23/25 关联；此前的资源槽猜测不成立。

Intel Gen12 头文件 `mhw_render_hwcmd_g12_X.h` 的字段布局与捕获序列相符：dword 9 `0x69041301` 是选择 Media pipeline 的 `PIPELINE_SELECT`；dword 10 `0x6101000e` 开始 `STATE_BASE_ADDRESS`，捕获长度字段对应总计 16 个 dword。按该头文件前 16-dword 布局，dword 11–12 是 General State Base Address（值 `1,0`，更新使能位为 1、地址字段为 0），dword 13 为 0；dword 14–15 是 Surface State Base Address（值 `0,0`，未设置更新使能位）；dword 16–17、18–19、20–21 分别是 Dynamic State、Indirect Object、Instruction Base Address（各为 `1,0`，低位更新使能为 1、地址字段为 0）。dword 22、24 分别是 General State 与 Indirect Object Buffer Size（值 1，即只置更新使能位）；dword 23 是 DW13 `DynamicStateBufferSize`，dword 25 是 DW15 `InstructionBufferSize`，分别为 `0x1000 | 1` 与 `0x3000 | 1`。

这两个大小字段的来源也与分配器相符：`0x2C25C` 由 `0x2C158` 以 `0x600` 字节调用，按页向上取整后写入 `+0x11A0` 并用作清零长度；`0x2C158` 将 context `+0x164` 的存储长度按页取整写入 `+0x190`，CPU 夹具的 `0x2080` 对应 `0x3000`。因此 dword 23/25 更符合动态状态与指令缓冲区大小，而不是资源地址或资源槽标签。上述命令字段与通用 Gen12 头文件布局吻合；具体 Tiger Lake 16-dword 变体和硬件所见的基地址仍待确认。

同一 Intel Gen12 头文件当前定义的 `STATE_BASE_ADDRESS` 扩展结构有 22 个 dword，而捕获中的命令头是 16-dword 形式；前 16-dword 字段位置与本批数据相符，但尚未用匹配的 Tiger Lake 命令规范确认这份旧式长度对目标硬件的完整要求。最初 `submit-48718.bin` 的确在原 submit 前被截停；2026-11-04 后续单次放行探针另行捕获了逐字节相同的 command，并将它提交给原 API，详见下方运行记录。其余命令边界为：dword 26–34 是 9-dword `MEDIA_VFE_STATE`，35–38 为 `MEDIA_CURBE_LOAD`，39–42 为 `MEDIA_INTERFACE_DESCRIPTOR_LOAD`，43–59 为 17-dword `MEDIA_OBJECT_WALKER`；随后 dword 60–65 和 66–71 各为一条 6-dword `PIPE_CONTROL`。实测 `VFE.DW3=0x00DF1000`（224 线程）、walker 网格为 80×45，walker 结束相对偏移为 `0xF0`。dword 0–2 解码为 3-dword `MI_LOAD_REGISTER_IMM`，寄存器偏移为 0、写入值为 `0x80000040`；VA 统计构造器 `0x2C84A` 调用的 helper `0x1C1B` 正是写出这三个 dword 的来源，因此它是驱动生成的预期前缀，当前仍未知该寄存器访问对白名单/引擎的具体约束。dword 3–8 是 `PIPE_CONTROL`，DW1=`0x001010a0`（DC flush、PIPE_CONTROL flush、render-target cache flush、command-streamer stall）。

manager 前缀还能补充缓冲区范围：捕获的 command 已用长度为 296 字节；`0xA893` 在成功 submit 后将 command limit 设为 `IOAccelResourceGetDataBytes - 0x80 + IOAccelResourceGetDataSize`，本快照的 base/limit 差为 `0xFF80`，据此反推该 command resource 的 `GetDataSize` 为 `0x10000`（64 KiB）。sideband base/current/limit 给出的已用长度为 200 字节、VA 可用跨度为 `0x1FF80`；sideband 头部的 dword 0=`0x7FFC` 与 `0xA893` 的 limit 计算一致，dword 2=`0x2E` 表示 46 个 payload dword，另有 16 字节头部。manager 显示一个 cached data-buffer class，且其 data-bytes 起点与 command base 相同；这确认了 class 与命令缓冲区的边界关系，但没有确定 kernel 指令在 GPU 地址空间中的映射。

REA 继续追踪分配链：`0x9710` 对资源表中的每个条目按记录长度调用 `vm_allocate`，随后调用 `0x9628`；`0x9628` 将该 CPU backing 地址和长度传给 `_IOAccelResourceCreate`，再取得 `ClientShared` 并设置其标志。`+0x11A0` 与 `+0x190` 因此同时参与 CPU backing 的分配长度和 `STATE_BASE_ADDRESS` 的大小字段。该链没有观察到 GPU 虚拟地址或最终页表映射，CPU 地址与 `ClientShared` 指针不能替代这一缺失证据。

这批数据与 `AVD_FrameStatisticsContext` 的静态构造器输出高度一致，因此可将本次 Usage=0 首个非空提交归类为**统计任务 batch 的提交前捕获**，而不是 PAK 帧码流提交。历史 Signature 813 报告记录 `IPEHR=0x7103000f`、`ACTHD=0x400100f0`，与 walker 头和结束偏移相符；但历史 reset 没有可配对的完整硬件 batch/submission trace，本次又在原 submit 前截停，所以该相关性不能证明唯一故障根因，也不证明本次捕获的 batch 已被 GPU 接受或执行。

对照 2026-11-03 10:16:51 与 13:23:24 的 GPU restart 报告，两者都显示 RCS busy in batch，`RING_HEAD=0x30`、`RING_TAIL=0x68`、有效 `BB_ADDR=0x400100f1`（偏移 `0x400100f0`）、`ACTHD=0x400100f0`、`IPEHR=0x7103000f` 及 `INSTDONE=0xffdcffff`（VFE、TSG0、CS 未完成）。报告记录的 `IPEIR`、`EIR`、`ESR` 和 command fault register 为零，没有显式页故障标志。13:23:24 报告来自单变量 VFE DW3 bit 6 试验：统计调度返回 0 后，日志记录 PAK 请求在原调度前被截停，GPU reset 报告约一秒内生成；该报告仍停在同一 walker 指令/偏移。因此 bit 6 修改单独不足以消除这次 hang。由于没有将完整硬件提交 trace 配对到报告，且记录只能证明该进程的调度顺序，统计 batch 与 reset 的直接归因仍未完成。

继续核对 `frame-statistics-kernel-11.asm` 中统计 kernel 的七条 DataPort 内存 send：Media Block Read descriptor 低字节为 BTI 0、1；Media Block Write descriptor 为 BTI 2、3；OWord Dual Block Write descriptor 为 BTI 4。Tiger Lake PRM 将 BTI `0x00..0xEF` 定义为 binding-table surface，stateless aliases 为 `0xFD/0xFF`。因此，这七条已识别的 kernel 消息使用 bindful surfaces；虽然 batch 中 General State Buffer Size 更新为零，但当前没有证据表明这些消息会访问该 stateless general-state 区域，不能据此归因 reset。Surface State Base Address 在该 SBA 中没有设置 modify-enable，故其有效来源仍依赖先前 context 状态；BTI 0–4 对应的表项内容、执行时的实际 GPU 映射和内核图像兼容性仍未知。

command resource 大小和 sideband 的 VA 可用跨度已由 manager 字段恢复；cached class 与 command base 相同，但 kernel 代码、class 元数据与 GPU 虚拟地址之间的映射仍未知。16-dword `STATE_BASE_ADDRESS` 是否完全符合目标 Tiger Lake 变体、LRI 所写寄存器对白名单/引擎的具体约束、event flush 的内核处理结果也尚未确认。3 次空 submit 已被原样转发，但其内核侧处理结果未单独观测。2026-11-04 复测已将该统计 batch 交给原 IOAccel submit，API 返回 0；随后系统仍记录 RCS reset，尚未观察到 GPU 完成该 walker 或统计任务。reset 根因与具体资源映射仍未唯一确定。`all_runtime_submission_paths_proven=false` 仍然成立。

### 2026-11-04 单次统计放行

在 macOS 26.4.1（build 25E253）上，以项目内未修补的 `va-gen12-native/AppleIntelTGLGraphicsVADriver` 运行 1280×720、1 帧、Usage=0。门控配置为只允许匹配的 `FrameStatisticsContext` 调度通过，并在捕获首个非空 command/sideband 后放行一次；第二个非空 submit 会在原调用前停止，其他 schedule 也不放行。REA 进程捕获从 2026-11-04 12:56:23.147 到 12:56:32.058（+08:00）结束，未触发超时。

快照位于 `build/validation-encode-and-kext-icons-20261102/vme-submit-forward-20261104/submit-68190.bin`，manager 前缀 136 字节、command 296 字节、sideband 200 字节；事件记录为 `request=0, flags=0, fault=0, cached_class_count=1`。296 字节 command 与 `vme-submit-usage0-nonempty-20261104/submit-48718.bin` 中的 command 段逐字节相同。REA 留存的进程日志显示：首个非空 VME submit 被转发后返回 `status=0`、`flags=0`、`fault=0`；resource class count 为 1，`IOAccelResourceGetDataSize` 为 65,536 字节，`GetDataBytes` 与 `ClientShared` 指针均被记录。`ClientShared+0x100=108`、`+0x1c=0`；这些 CPU 可读字段没有给出 GPUVA。统计 schedule 返回 0，VideoToolbox 随后报告 `submit=0 status=0 flags=1`。下一条 `kind=0x20/capability=0x400` 的 PAK schedule 在原调度前以 77 截停。VME sidecar event 名称仍为 `submit-buffers-before-original`，所以 API 返回、resource getter 和 PAK 截停证据来自 REA 留存日志，而非该 sidecar。

测试进程运行期间新生成 `/Library/Logs/DiagnosticReports/Kernel_2026-11-04-125631_Mac-Pro.gpuRestart`：Application 为 `xe-h264-encode-m`、Signature 813，RCS busy in batch；`IPEHR=0x7103000f`、`ACTHD=0x400100f0`、`BB_ADDR` 偏移 `0x400100f0`，`INSTDONE=0xffdcffff`（VFE、TSG0、CS 未完成）。报告没有非零 `IPEIR/EIR/ESR` 或 command fault register。进程日志证明本轮只有统计 schedule 被原样调用，而 PAK 原调度已被拦截；因此该 GPU reset 与放行的统计工作相关性显著增强，但报告中的 hardware context 没有映射到具体 VME command，仍不能作出唯一 batch 归因。它与 2026-11-03 单变量 VFE bit6 版本一样停在 walker 偏移。GPUVA 仍未知。

### 2026-11-04 命令构造与 Context 偏移复核

REA/Ghidra 对 `0x2C84A` 的静态结果与 CPU batch 及真实捕获的 dword 位置相互吻合：context `+0x4238` 写入 `0x69041301`（`PIPELINE_SELECT`）；`+0x423C` 开始 16-dword `STATE_BASE_ADDRESS`（首 dword `0x6101000e`）。因此 `+0x423C` 是 SBA 命令，不是 interface descriptor 数据。

`0x2C84A` 在 context `+0x42E4` 构造 4-dword `MEDIA_INTERFACE_DESCRIPTOR_LOAD`，字段为 `DW0=0x70020002`、`DW2=0x40`、`DW3=0`；这表示命令请求从其相对起始地址 0 加载 64 字节 interface descriptor。该命令所读取的 64 字节内容及其 CPU 来源目前尚未定位。`0x2CC16` 会读取并组织这组命令字段，但现有调用证据没有证明对应字节最终落在 GPU dynamic-state 地址 0。

`0x1C40` 从静态模板 `0x31D744` 复制 17 个 dword（68 字节），模板首 dword 为 `0x7103000f`，随后按输入几何参数改写 Walker 字段。其已解析 caller `0x2C84A` 将结果写到 context `+0x42F4`，每个实例步长 `0x44`；`0x2CC16` 在对应路径复制这 17 个 dword。真实 batch 的 Walker 位于 dword 43–59，即相对偏移 172–239、结束偏移 240，与该构造器的 68 字节长度一致。这确认了 CPU 侧 walker 构造与捕获命令的结构对应，但不能把 GPU reset 报告唯一配对到这个资源实例。

捕获中的 `MEDIA_CURBE_LOAD` 长度为 32 字节，相对起始地址为 `0x400`。在 `0x22236` 路径中，驱动把 context `+0x8E1` 的 32 字节传给 `0x2C7A0`；该函数将其复制到 context 当前 resource 的 CPU 映射 `+0x400 + curbe_index * 0x100`。这把该路径的 CPU 输入和 CURBE 命令偏移对上了，但没有给出该 resource 的 GPUVA，也没有证明其 GPU 映射状态。统计 kernel 的已识别 DataPort 消息引用 BTI 0–4，但 interface descriptor、binding-table 条目及对应 surface state 的最终 GPU 映射仍未解出；`0x2CC16` 中从 context `+0x427C` 起按 `0x24` 字节步长复制的记录，现阶段没有足够类型或映射证据可认定为这些表项。以上为静态复核，未再提交 GPU 工作负载，也未修改系统 VA、EFI 或已安装驱动。

另沿 `AVD error: FrameStatistics: Fail in findding Prev` 的代码引用追到 `0x21D22`。它从 context `+0x21B8 + kernel_index * 0x20` 取一个 CPU 指针，并写入 `+0x40`、`+0x80`、`+0xC0`、`+0x100`、`+0x140` 五个相隔 `0x40` 的状态块：首块使用当前 IOSurface 的格式、宽高和行跨度；第二块使用前一 IOSurface，若没有前一帧则清零；中间两块在对应像素格式分支下读取另一 IOSurface 的 plane 0/1；最后一块读取 `+0x4700` 下资源对象的字段。五块的数量与 kernel 已识别的 BTI 0–4 相同，构成了可核查的 surface 候选布局；但绑定表内容未恢复，不能据数量和顺序断言它们分别对应 BTI 0–4，也不能把 CPU 指针当作 GPU 地址。之前运行记录中的 `previous_surface=0` 与该函数清零前一帧状态的分支一致，但不证明该分支导致 RCS reset。

继续向前追这些 CPU 指针的来源：`0x2C158` 以 `0x240` 调用 `0x2C314`；后者按 kernel 和资源索引建立 context `+0x21A8` 起的资源记录，并对该记录对应的 CPU 区域清零。它按页申请的分配跨度为 `0x1000`，逻辑状态长度仍是 `0x240`。随后 `0x2C41C` 对每个状态区初始化 8 个、每个 `0x40` 字节的 surface-state 候选记录，起始偏移依次为 `+0x40` 至 `+0x200`；FrameStatistics 的 `0x21D22` 会更新其中前五项。这解释了五个候选 surface 状态的分配来源与步长。状态区最前面的 `0x40` 字节没有被这两个初始化函数解释；它可能承载 binding-table 数据，但表项写入者、表项内容、Surface State Base Address 实际指向和 GPU 映射仍未确定。

## 验证

- 之前的受控 `Usage=1` 运行在 `0xA8F6` 命中，退出码 77，输出 `build/validation-encode-and-kext-icons-20261102/vme-submit-prefix-usage1-20261103/manager-15298.bin` 与 `submit-events-15298.jsonl`。它只验证旧版 manager 前缀捕获；不能证明本版 command/sideband 复制已在真实提交中运行。
- 新增 CPU 夹具覆盖 command 与 sideband 范围、未知 sideband fail-closed、非零 request/flags/fault、8/12 字节空 control flush 签名、非零 command/class/错误 sideband 记录拒绝、空初始化提交、反向游标、超预算、部分复制、原始前缀不变和两种互斥模式。
- `test-va-context-trace.py` 覆盖 schedule 包装默认启用、设为 `0` 仍启用、设为 `1` 才跳过；跳过选项尚未在真实 VA 函数表上做运行时验证。
- `tools/diagnostics/gpu-tests/test-*.py` 的 11 个脚本、50 项测试全部通过；本轮新测试先分别以退出码 97（未知 sideband 被当作空）、104（空 flush 判据缺失）和 120（非零 request 未被视为数据）失败，修正后通过。
- 捕获 interpose 与 VA 诊断源码使用 `clang -arch x86_64 -mmacosx-version-min=15.0 -dynamiclib -O2 -Wall -Wextra -Werror -undefined dynamic_lookup` 严格编译通过；产物位于 `build/validation-encode-and-kext-icons-20261102/xe-va-vme-submit-capture-nonempty-lab.dylib`，`__DATA_CONST,__interpose` 段存在。
