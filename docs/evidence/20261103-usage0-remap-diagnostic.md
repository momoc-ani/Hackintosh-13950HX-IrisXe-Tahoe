# Usage=0 VA 映射诊断（2026-11-03）

本轮调查 Usage=0 的编码 context 分派及首帧失败边界。EFI、系统 VA、NootedGreen 和 NootRX 均未修改；没有安装驱动或重启。创建结果、截停采集和崩溃摘要保存于[结构化证据](20261103-usage0-remap-validation.json)。

## 映射范围

此前进程日志显示，Usage=0 的 `kind=0x20, capability=0x4000` 请求能让 `CreateContexts` 返回成功，但 streamout 参数没有 context，`SetContextParameters` 返回 4，VideoToolbox 最终返回 `-12915`。REA 对候选 VA 的静态结果显示，编码 context 创建分支为 `kind=0x20` 建立 `0x400`、`0x80`、`0x40` 对象；普通请求的 `0x4000` 没有对应分支。

新增 `xe-usage0-remap.c`，只在显式设置 `XE_USAGE0_REMAP=1` 时，将 `kind=0x20, capability=0x4000` 映射到已验证的 VDENC `0x400`。创建请求与 0x30 字节参数项都单独映射；其他 kind、capability、节点和参数字段不变。映射仅存在于注入探针的进程中。

CPU 测试先因映射函数缺失而失败，再在实现后通过。覆盖请求链字段保留、参数数组字段保留、空链处理、默认环境不改写，以及 opt-in 环境中的日志和原函数返回值。相关测试为：

- `tools/diagnostics/gpu-tests/test-usage0-remap.py`
- `tools/diagnostics/gpu-tests/test-va-context-trace.py`

## 进程验证

未启用映射的基线复现 `create_status=-12915`：streamout 参数 1 的 `context_present=false`，参数回调返回 4。

启用映射后，`--create-only` 的结果为 `create_status=0`、`phase=created-only`、`UsingHardwareAcceleratedVideoEncoder=true`。dyld 日志确认本次加载的是 `build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver` 和本地诊断 dylib。该次运行没有提交编码帧。

随后只请求编码一张 1280×720 强制 I 帧。会话创建、属性设置、prepare 均返回 0，`EncodeFrame` 返回 0；但 `VTCompressionSessionCompleteFrames` 执行 VA 编码命令时进程触发 `EXC_BAD_ACCESS`，没有产生可验收码流。API 接受帧不能证明驱动已向 GPU 提交该帧。崩溃记录位于 `~/Library/Logs/DiagnosticReports/xe-h264-encode-modes-lab-2026-11-03-101700.ips`。

崩溃前同一 context 的参数日志为：

| 参数 | 映射后 capability | context | 返回值 |
|---:|---:|---|---:|
| 1（streamout） | `0x400` | 存在 | 0 |
| 11 | `0x400` | 存在 | 5 |
| 5 | `0x400` | 存在 | 0 |
| 7 | `0x400` | 存在 | 0 |

因此，创建成功不能代表普通编码兼容。参数 11 在映射后的 context 中仍不受支持。

## 静态崩溃定位

REA 在候选 VA 中将崩溃地址 `0x273e3` 定位到 `FUN_0002737e`。指令 `MOV RCX, [RCX]` 读取空地址；崩溃报告的 faulting thread 寄存器也记录 `RCX=0`。该函数先在 `0x273aa` 将命令节点 `+0x20` 的帧参数指针放入 RBX，随后在 `0x273dc` 从帧参数 `+0x440` 读取指针，最后在 `0x273e3` 对其解引用。该函数也读取重建图像及缩小图像状态。

这里的 `+0x440` 属于帧参数数据块，**不是编码器 context 对象**。`FUN_00026ac0` 对 context 状态区的清零不能解释这个帧参数字段的来源，之前将二者联系的推断撤回。`FUN_00027202` 是 context 参数处理函数；它对参数 11 返回 5，但目前没有证据证明参数 11 与该空指针有因果关系。

当前证据支持：映射补上了 context 创建和 streamout；首帧仍缺少 VDENC 路径要求的帧参数，不能猜测补齐空指针。

补查系统诊断目录后，发现同次实验的 `Kernel_2026-11-03-101651_Mac-Pro.gpuRestart`。报告时间为 2026-11-03 10:16:51，Application 为 `xe-h264-encode-m`，Signature 813；RCS 忙且卡在 batch 中，VCS0 为 disabled/idle。该记录将重置关联到本地探针，撤回此前仅凭统一日志得出的“未观察到新 reset”判断。不能将这次报告与先前 `mediaanalysisd` 的 Signature 1000 报告混为一谈。

## 原调度前截停对照

新增 `xe-va-command-trace.c`：在 VA 工厂发布的表项 11（偏移 `+0x58`、原函数 `0x159a4`）前记录命令。该命令链的 next 位于 `+0x30`，区别于创建请求的 `+0x38`。开关 `XE_VA_STOP_BEFORE_SCHEDULE=1` 会在调用原 VA 调度前以 77 退出；启用已知不兼容映射 `XE_USAGE0_REMAP=1` 时也强制截停，不允许仅因漏设截停变量就重走失败实验。

本轮两次采集均创建 1280×720 会话、请求一帧，且都在原 VA 调度前以 77 退出。创建、设置及 prepare 均返回 0。

| 采集条件 | 首次调度的节点链 | 是否获得编码帧参数 | 原 VA 调度是否执行 |
|---|---|---|---|
| Usage=0，启用映射 | `kind=2/cap=0x1000` → `kind=16/cap=0x20` | 否，截停在前置链 | 否 |
| Usage=1，未启用映射 | `kind=0x20/cap=0x400` | 是，帧参数 `+0x440` 非零 | 否 |

因此，本轮动态采集证明两种用途在进入驱动前已分成不同链路；**尚未获得 Usage=0 的最终 PAK 帧参数快照**，不声称已完成两种编码帧数据的逐字段动态对照。

参数 11 在这次 Usage=1 对照中也返回 5，之后仍到达正常的 VDENC 调度节点。结合已通过的 Usage=1 帧实验，参数 11 返回 5 不能单独解释 Usage=0 的失败。

AppleGVA 静态调用链提供进一步区分：普通分支包含 `Branch_Encoder1_VMEPAK`，其 `executePAK` 例程（现有反汇编中的 `0x7ffa0c574af6`）调用 `0x7ffa0c571824` 构造 PAK 命令。该构造函数使用帧数据 `+0x458`、`+0x460` 保存 SPS/PPS，`+0x478` 为分片参数指针数组。已验证的 VA VDENC 读取 SPS/PPS 的位置则为 `+0x410`、`+0x418`，并将 `+0x478` 当作重建图像表面。两条链路的接口含义不同，修改 capability 值不能把一条完整转换为另一条。

当前结论包含两个待修边界：普通路径首帧实验发生 RCS 重置，且首次调度是一条额外前置链；将后续 PAK 请求替换为 VDENC 又存在帧布局不兼容。尚未用提交前后快照确定 RCS 卡死对应前置链中的哪一个具体节点。没有将任一处用忽略返回码、虚构图像指针或跳过必要任务掩盖。Usage=0 没有软件解码或逐帧质量验收结果。

强制截停增加后，另用原诊断库路径复核只设置 `XE_USAGE0_REMAP=1`、漏设 `XE_VA_STOP_BEFORE_SCHEDULE` 的调用；结果仍为退出码 77，命中相同前置链且未调用原调度。两份本地诊断 dylib 已重新编译为包含该强制截停的版本。

## 复现诊断

先编译独立诊断库；以下命令只写 build 产物：

```sh
clang -arch x86_64 -mmacosx-version-min=15.0 -dynamiclib -O2 \
  -Wall -Wextra -Werror \
  tools/diagnostics/gpu-tests/xe-va-context-trace.c \
  tools/diagnostics/gpu-tests/xe-usage0-remap.c \
  tools/diagnostics/gpu-tests/xe-va-command-trace.c \
  -o build/validation-encode-and-kext-icons-20261102/xe-va-command-trace-lab.dylib
```

在仓库根目录运行截停探针。退出码 77 表示已在原 VA 调度前终止，不能计为编码通过：

```sh
DYLD_LIBRARY_PATH="$PWD/build/validation-encode-and-kext-icons-20261102/va-gen12-native" \
DYLD_INSERT_LIBRARIES="$PWD/build/validation-encode-and-kext-icons-20261102/xe-va-command-trace-lab.dylib" \
XE_USAGE0_REMAP=1 XE_VA_STOP_BEFORE_SCHEDULE=1 \
build/validation-encode-and-kext-icons-20261102/xe-h264-encode-modes-lab \
  build/validation-encode-and-kext-icons-20261102/usage0-preschedule-repeat \
  1280 720 1 --usage 0
```

三个相关 CPU 测试分别执行 `test-usage0-remap.py`、`test-va-context-trace.py` 和 `test-va-command-trace.py`。测试直接编译 C 核心，不连接 GPU。新增代码完成后逐个运行 `tools/diagnostics/gpu-tests/test-*.py`，9 个文件共 38 项测试全部通过。由于测试文件名含连字符，通用 `unittest discover` 未发现测试时不能当作通过。

## 状态

此映射不是 Usage=0 修复，不能安装到系统或用于真实帧编码。它保留为默认关闭、供 `--create-only` 及原 VA 调度前截停的分派诊断。后续需要还原 AppleGVA 普通编码分支的实际帧参数布局，确认 `+0x440` 的来源；在该结构被证实之前，不再用这个映射执行原 VA 编码命令。系统 VA 与 EFI 仍保持原样，不需要重启。

同日后续已确认首条前置链的类型为 `AVD_FrameStatisticsContext`，补齐其原调度前字段采集，并在普通 CPU 内存中执行真实命令构建器。RCS walker 的 17 DWORD 长度与 Gen12 定义一致；VFE 融合 EU 分派位存在独立差异，仅制作了单字节 CPU 验证副本，尚未运行 GPU。详见[前置统计任务与 VFE 核对](20261103-usage0-frame-statistics.md)。这不改变上述映射的截停限制，也不代表普通编码通过。
