# 2026-11-02：KEXT 图标核对与 Intel 编码实验

用户确认上一轮 Blit3D URB 修补后，应用内原先缺失的文字、按钮与图标已恢复。本轮核对 Finder / Quick Look 中的 KEXT 图标，然后处理 Intel 硬编码。

**本轮未替换系统驱动、EFI、NVRAM 或内核扩展。编码候选仅通过 `DYLD_LIBRARY_PATH` 加载到自有进程。当前通用 Intel 硬编码仍未完成，普通 P 帧仍会挂起 VCS0。**

## KEXT 图标

截图白色积木与当前系统资源一致：

```text
/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/KEXT.icns
```

自有 CARenderer 探针对该资源与实际 `AMFIPass.kext` 的 NSWorkspace 类型图标，分别在 Intel / AMD 渲染同一输入。16、32、128、512 px 共 8 项，全部非空且逐字节相同，最大通道差为 0。复测结果在 [kext-icons.json](20261102-kext-icons-and-encode/kext-icons.json)。不能据此断言另一 macOS 版本一定采用相同图案，但未发现截图中此类图标的 Intel 渲染故障。

## 编码器选择与实际执行

本机 macOS 为 `26.4.1 / 25E253`，Intel Metal registryID=`4294968992`。启动仍为 2026-11-02 13:36:51，未重启。系统仍未发布 `IOGVAH264EncodeCapabilities`，显式 Intel 系统 H.264/HEVC 编码会话返回 `-12908`。会话列表或系统默认 HEVC 会话的成功不作为 Intel 编码证据。

自有探针仅在进程内注册当前系统的 GVA H.264 factory：

- `VTRegisterVideoEncoderWithInfo` 的实现 ID 字段为 `CMClassImplementationID`；选择 ID 为 `local.test.intel.h264`。
- H.264 factory 在当前 VideoToolbox image 的 `+0x46a370`，检查入口字节；探针限制为系统构建 `25E253`。
- 显式设置 Intel GPU ID、硬件标志和禁止沙盒编码；使用内部 `Usage=1` 进入 VDENC 分支。
- `UsingHardwareAcceleratedVideoEncoder=True`。内置编码器的 `UsingGPURegistryID` 可以为空，不把空属性直接视为软件回退。
- 输出真实 Annex-B 码流和完整 NV12 输入参考。独立 `ffmpeg -hwaccel none` 软件解码，检查帧数、尺寸、关键帧、连续 PTS、完整有效像素和 PSNR。

公共低延迟 specification 与显式 ID / 硬件要求的部分组合会被系统前置校验拒绝，早期 `-12902` 不全是 VA 故障。

## 已实现的局部修补：普通像素转换回退

基础编码候选沿用实验目录的 VPHAL 初始化、SPS 布局、状态寄存器、表面格式和关闭坏媒体 kernel 等补丁。**没有应用 `patch_vdencintra.py`**。

原候选在普通外部 1080p NV12 输入下，经 `VTPixelTransferSession → AVFPixelProcess → VPHAL` 崩溃；`0x126520` 的子 renderer 对象为空。仅加空指针 return 会在下一处 renderer 调用继续崩溃，不能作为修复。

在实际创建上下文入口观察到：

| 请求 | type | capability | 用途字段 +0x14 |
|---|---:|---:|---:|
| 编码器内部 scaler | 2 | 0x800 | 1 |
| 普通缩放 / 颜色转换 | 2 | 0x800 / 0x200 | 0 |

新增 `macos/vadriver/encode-experimental/patch_vphalfallback.py`：在 VA `CreateContexts` 入口 `0x13e43` 遍历请求节点，仅对用途 0 的普通 VPHAL 缩放 / 颜色转换返回不支持，让 VideoToolbox 在初始化前选择软件转换；编码器内部 scaler、解码器节点与其他功能继续执行原函数。

它不关闭 Intel Metal，不改变 framebuffer，不在提交后假装转换成功。此前试过“调度阶段返回错误”，虽然编码回调全部成功，码流画质仅 6.29 dB，已判失败并排除。

修补代码放在已核对的空闲区 `0xe60..0xeff`，回放原函数前 6 字节后跳回 `0x13e49`。脚本逐字节检查入口和空闲区、支持相同补丁幂等，强制写独立输出副本。`test-vphal-fallback.py` 在可执行内存中运行实际生成的 x86_64 代码，8 项覆盖缩放、颜色转换、内部 scaler、解码器、节点链、多个入口、空请求和幂等。修补前已观察普通转换测试失败，修补后通过。

## 当前候选实际回归

候选 `va-vphal-fallback` 未依赖应用设置软件像素转换，也未指定编码器缓冲池。完整结果在 [h264-intra-validation.json](20261102-kext-icons-and-encode/h264-intra-validation.json)。

| H.264 全 I 帧 | 输出 / 请求 | 回调错误 / 丢帧 | 软件解码 PSNR |
|---|---:|---:|---:|
| 1280×720 | 30 / 30 | 0 / 0 | 53.34 dB |
| 1920×1080 | 30 / 30 | 0 / 0 | 52.73 dB |
| 3840×2160 | 8 / 8 | 0 / 0 | 45.63 dB |

独立解码确认全部为 I 帧。4K 当前使用同一 8 Mbit/s 设置，画质数值只对应此固定样本，不代表所有实际内容。

普通像素转换在启用硬件转移的会话中，与显式软件会话对照，5 / 5 有效像素完全一致：NV12 同尺寸拷贝、NV12→BGRA、BGRA→NV12、BGRA 缩放、NV12 缩放。结果在 [pixel-transfer.json](20261102-kext-icons-and-encode/pixel-transfer.json)。

额外的 BGRA 实际编码完成 1080p 四帧，回调无错误、无丢帧，软件可解码。输入由软件将已知 NV12 转为 BGRA；对原始 NV12 的整体链路 PSNR 为 42.21 dB，包含色彩转换与有损编码误差，详见 [BGRA 结果](20261102-kext-icons-and-encode/h264-bgra-quality.json)。此项不代表所有 BGRA 场景均已验收。

候选硬解回归使用新增 `xe-codec-decode --local-gva`，H.264 调用当前系统 factory，HEVC 使用系统 `AppleGVAHEVCDecoder` 导出的 factory，在本进程强制选择 Intel；dyld 记录均确认加载 `va-vphal-fallback` 候选，而非系统 XPC 的旧包。

| 候选硬解回归 | 帧数 | 与新采集的软件参考的完整有效像素差 |
|---|---:|---:|
| H.264 1080p | 30 | 0 |
| HEVC 1080p | 30 | 0 |
| HEVC Main10 1080p | 16 | 0 |
| H.264 4K | 8 | 0 |
| HEVC 4K | 8 | 0 |

共 92 帧。证据为 [选择与路径](20261102-kext-icons-and-encode/decode-candidate-selection.json)、[完整像素比较](20261102-kext-icons-and-encode/decode-pixel-comparison.json)。候选保持了这些已测试的硬解能力。

`verify-h264-encode.py` 的 6 项纯 CPU 测试还覆盖无样本回调、丢帧、软件编码、帧间模式、重复 PTS 和错误画面，防止只凭会话与回调验收。

## P 帧仍失败，不能发布通用编码

原 VDENC 真实 P 帧测试中，首帧通过，后续回调无有效样本，VCS0 在 `MI_FLUSH_DW` 挂起。全 I 帧与“VDENC 强制 intra、PAK 写 P”属于不同路径，均不能证明帧间预测已修复。

本轮已在第二帧提交前退出的探针中，捕获 I/P 的图像状态、完整资源 token 与命令流；保留上帧重建和缩小参考图，CPU 读回 luma 与输入基本一致。用户态捕获发生在内核资源绑定之前，资源表中的旧地址不作为本帧真实 GPU 地址证据。

对照 Intel 官方 `media-driver` 的 Gen11 / Gen12 定义：

- 图像状态 35 DWORD 的核心字段布局基本一致。
- 当前 VA 的模式命令为 2 DWORD，Gen12 官方构造为 6 DWORD。
- 扩展模式命令时，早期自有探针漏更新命令流容量及写入基址，导致控制字被后续指令覆盖；这些失败测试不能用于证明完整 Gen12 模式初始化无效。
- 修正上述两点后，提交前 I/P 命令的长度、后续源表面与 slice 偏移全部对应，详见 [gen12-mode-dryrun.json](20261102-kext-icons-and-encode/gen12-mode-dryrun.json)。修正候选全 I 四帧通过、PSNR 53.04 dB；真实 I+P 两帧仍仅一帧有效，详见 [帧间报告](20261102-kext-icons-and-encode/inter-gen12-mode-v2-report.json)。因此单独补齐该命令未解决 P 帧问题，没有把此实验候选加入安装链。

后续需要继续定位参考帧的最终硬件绑定、命令格式与编码同步。普通导出 Usage=0、HEVC 编码、其他 BGRA 内容与布局、码率控制、系统 XPC 编码及并发编码尚未验收。

## 本次启动的稳定性记录

本次启动中存在多次 GPU reset，不能延用首次重启验收早期观察窗口的“无新增 reset”结论。

- Intel 自有编码实验：14:23:41、15:14:58、15:17:52、15:31:56、15:47:09，均为 VCS0；其中三次来自不完整的命令扩展探针或候选，最后一次是修正扩展后的真实 P 帧测试。
- Intel `mediaanalysisd`：14:22:30，独立于编码探针，未定位根因。
- AMD 也产生了多份报告，包括 13:59:07、14:11:39、14:13:36、14:36:20、15:49:54（WindowServer）以及 16:15:22（OpenClaw）；不能把这些直接归因为本轮 Intel 编码修改。

完整按应用、GPU 分类的读取结果在 [gpu-restarts.json](20261102-kext-icons-and-encode/gpu-restarts.json)。系统未重启，已安装包未变更，但本次运行已受实验性 GPU reset 影响，不能把它作为干净启动的长期稳定性验收。VPHAL 相关的自有探针崩溃调用栈节选保存在 [probe-crashes.json](20261102-kext-icons-and-encode/probe-crashes.json)。

## 复现范围

所有原始实验文件在：

```text
build/validation-encode-and-kext-icons-20261102/
```

仅对实验副本生成分流候选：

```sh
python3 macos/vadriver/encode-experimental/patch_vphalfallback.py \
  build/validation-encode-and-kext-icons-20261102/va-candidate/AppleIntelTGLGraphicsVADriver \
  build/validation-encode-and-kext-icons-20261102/va-vphal-fallback/AppleIntelTGLGraphicsVADriver
```

真实编码与独立验收：

```sh
DYLD_LIBRARY_PATH="$PWD/build/validation-encode-and-kext-icons-20261102/va-vphal-fallback" \
  build/validation-encode-and-kext-icons-20261102/xe-h264-encode-lab \
  build/validation-encode-and-kext-icons-20261102/h264-local 1920 1080 30
python3 tools/diagnostics/gpu-tests/verify-h264-encode.py \
  build/validation-encode-and-kext-icons-20261102/h264-local
```

实验 VA 仍未安装。全 I 帧候选可用于验证硬件路径，但不能作为用户已经批准的“通用 Intel 硬编码完成”替代品；系统部署前需先解决普通帧间编码并完成回归。

## 变更管理与审查限制

本轮诊断工具和 VPHAL 分流补丁分别提交，未混入既有 `20261030-render-and-codec-validation.md` 的未提交内容。已执行实际构建、机器码测试、码流与像素回归。尝试安排独立审查时，审查代理未执行文件读取或实际审查，因此本记录**不宣称独立代码审查通过**。

## 2026-11-02 16:35 重启后的参考资源绑定排查

本阶段启动时间为 **2026-11-02 16:35:45 CST**。Intel registryID 已变为 `4294968951`；不能复用重启前的 registryID 或稳定性观察窗口。系统驱动、EFI 和 NVRAM 未在本阶段修改。仍使用进程内 VA 候选，通用 P 帧编码尚未通过。

新增 [xe-vdenc-binding-capture.c](../../tools/diagnostics/gpu-tests/xe-vdenc-binding-capture.c) 采集三个阶段：用户态提交前、内核提交返回后，以及首帧 fence 完成后。REA 对当前 VA 的 `0xa893`、`0xa9dc`、`0x226e6`、`0x22708` 和 `0x27724` 的读取确认：提交后 VA 会重新选择数据缓冲区，必须在此之前保存原缓冲区，不能只在下一帧观察资源表残留值。

默认采集在第二帧提交前退出。显式 `XE_BINDING_SECOND_END=1` 则在第二批次开头写 `MI_BATCH_BUFFER_END`，让内核回填资源地址，GPU 不执行后续 P 帧命令；提交返回即退出。首帧仍执行 I 帧编码。CPU 测试直接运行采集核心及 dyld 拦截器，验证截停、阶段数据独立和重叠复制。

[实际绑定与校验](20261102-kext-icons-and-encode/postbind-reference-validation.json) 显示：

| 资源 | 首帧写入的 GPU 地址 | 第二帧参考地址 | GPU pitch |
|---|---|---|---:|
| 完整重建图 Y | `0x40020000` | `0x40020000` | 1280 |
| 4 倍缩小图 Y | `0x4017e000` | `0x4017e000` | 384 |

第二帧三个完整参考槽均引用首帧完整重建图，两个缩小参考槽均引用首帧缩小图；句柄与最终 GPU 地址匹配。完整图 UV 与 Y 的地址差除以 pitch 为 736，缩小图为 192，与内核写入的表面 chroma 行偏移对应。CPU IOSurface 的 pitch 与 GPU 资源 pitch 是两种布局，不能用 320 与 384 的差异直接判定故障。

首帧 `submit status=0 flags=0 fault=0`、`finish status=0 fence=1`，提交后与完成后的 token、batch 内容一致。第二帧仅做绑定也返回成功。两次原始布局采集没有新增 GPU reset。原始 token、batch 与事件日志保存在 [vdenc-binding](20261102-kext-icons-and-encode/vdenc-binding)；测试使用这些小型采集文件，无需依赖被忽略的 build 目录。

此结果排除了本次样本中“P 帧引用了错误句柄或错误 GPU 地址”的假设，**没有证明所有参考读取状态和同步机制都正确**。

### 地址命令扩展实验与失败结论

当前 VA 的 `VDENC_PIPE_BUF_ADDR_STATE` 长 37 DWORD，与 Intel 官方 `intel-vaapi-driver/src/gen9_vdenc.c` 中的短格式一致；官方 `media-driver` Gen12 定义为 71 DWORD。前 37 DWORD 中本次使用的地址槽位置一致。仅补齐尾部未使用字段、修改长度，并移动后续命令，属于独立实验；没有合并进 VA 安装补丁。

新增 [命令布局转换](../../tools/diagnostics/gpu-tests/xe-vdenc-command-layout.h)、[CPU 字节校验](../../tools/diagnostics/gpu-tests/test-vdenc-command-layout.py) 与 [进程内实验库](../../tools/diagnostics/gpu-tests/xe-vdenc-address-lab.c)。校验覆盖真实 I/P 批次、原资源内容、后续命令、slice 偏移、容量和 VA 原有长度预估差值。VA token 中的长度来自预估函数，部分帧比实际已构建命令多一 DWORD，扩展保留这个差值。

第一次硬件实验暴露了自有诊断代码的错误：macOS 的 `memmove` 可与 `memcpy` 共用导出入口，原拦截器只正向复制，导致重叠移动反复复制 QPT 命令，在提交前就已破坏 batch。该实验于 **17:11:10** 产生 VCS0 reset（IPEHR=`0x404c3008`）。这不是 Gen12 长命令本身的有效反证。新增实际加载 dylib 的 CPU 测试先复现退出 99，再修正为按重叠方向复制。两个实验库均通过此测试。

修正后重新采集，[提交前字节验证](20261102-kext-icons-and-encode/gen12-address-preflight-validation.json) 确认前缀、地址 payload 和完整后续命令保留，QPT 只出现一次；总长度和偏移同步更新，另行扩大预留区。随后硬件结果为：

| 实验 | 结果 | 结论 |
|---|---|---|
| 71 DWORD，首帧执行、第二帧仅绑定 | 提交/完成/绑定返回 0，无新增 reset | 内核接受此布局，资源回填可采集 |
| 71 DWORD，全 I 四帧 | 4/4，软件解码均为 I，PSNR 53.04 dB | 短回归通过 |
| 71 DWORD，真实 I+P 两帧 | 仅首帧有效，第二帧无有效样本 | P 帧失败，不能安装 |

最后一项于 **17:31:24** 产生 VCS0 reset，仍停在 `MI_FLUSH_DW`（IPEHR=`0x13000003`，INSTDONE=`0x3bffffff`，VIN/VCS/GAC 未完成），与原 P 帧挂起一致。因此**单独补齐地址命令长度没有解决根因**，与此前“单独补齐模式命令”的失败结论并列；不能把两项各自失败当作同时修改后的结果。

[全 I 画质结果](20261102-kext-icons-and-encode/gen12-address-intra-quality.json)、[真实帧间输出清单](20261102-kext-icons-and-encode/gen12-address-inter-manifest.json)、[阶段与 reset 分类](20261102-kext-icons-and-encode/vdenc-postbind-stage-summary.json) 均保留。报告落盘有延迟，实验进程刚退出时的 `new_gpu_reports=[]` 不能替代稍后生成的 GPU restart 报告。本阶段系统已有上述两次 reset，不能声称当前启动保持稳定。

### 本阶段交付范围与后续边界

已取得首帧与第二帧内核回填后的实际绑定，并修正可复现的诊断器重叠复制问题。4 项采集/布局 CPU 测试、原有 8 项 VPHAL 机器码测试及 6 项编码报告验收测试均通过；这些测试通过不代表 P 帧编码已修好。

当前仍不能发布系统 Intel 通用编码能力，没有可要求用户重启安装的编码版 EFI。后续根因调查应继续核对 P 帧独有参考状态、缩小参考状态、图像状态与同步要求；本阶段没有增加新的猜测性 VA 或内核修补。普通 Usage=0、HEVC 编码、系统 XPC 调用、并发和长期稳定性仍未验收。

本阶段没有完成独立代理审查，不宣称独立代码审查通过。

### P 帧后续：完整 walker 与单活动参考映射

本节记录同一次启动（2026-11-02 16:35:45）中 17:50–18:56 的实验。系统驱动与 EFI 未安装本节修正，编码器仅在自有探针进程内注册。

原 `VDENC_WALKER_STATE` 长 4 DWORD，Intel 官方 Gen12 构造为 27 DWORD。新增 [状态转换器](../../tools/diagnostics/gpu-tests/xe-vdenc-reference-state.h) 保留原始前四字，设置 DW5 的 source width−1、DW14=`0x3f400000`、DW16=`0x003f3f3f`，其余新增字清零；每 slice 增加 23 DWORD，并同步 token 的 slice 长度、原有命令长度预估及实际/预留尾指针。

仅补齐 walker 后，最小 I+P 两帧通过，60 帧却只有 10.11 dB。六帧逐帧采集进一步显示：第三张 P 开始，硬件重建图仍接近输入，码流经独立软件解码却漂移；VDENC 引用的实际 GPU 地址仍为上一帧。这个结果排除了“回调成功即编码正确”，也说明此前两帧参考地址核对不足以证明连续帧正确。

官方 `codechal_vdenc_avc_g12.cpp` 的 Gen12 AVC VDENC 显式设置 `m_oneOnOneMapping=true`；`mhw_vdbox_mfx_g12_X.cpp` 使用活动引用替代整个 DPB，`mhw_vdbox_mfx_generic.h::AddMfxAvcRefIdx` 配套从零分配活动 reference ID。当前 VA 沿用 DPB 池轮换的 `0,2,4,6` 索引。新实验对**单活动参考**将 MFX reference index 映射到槽零，同时将对应 token 的 resource index/flags 移到槽零，供内核回填地址；多参考和 I 帧保持原分支。

参考源码来自 Intel 官方 `intel/media-driver` 与 `intel/intel-vaapi-driver`，原文保留在 `build/validation-encode-and-kext-icons-20261102/`。初始结构核对保留在 [参考与同步审计](20261102-kext-icons-and-encode/p-reference-sync-audit.json)，六帧漂移证据与组合验证保留在 [P 帧阶段结果](20261102-kext-icons-and-encode/p-frame-walker-reference-results.json)。

完整 walker 加单参考映射后的结果如下。独立解码强制软件路径，检查真实 I/P 类型、全部回调、PTS、完整有效像素；新验收还要求每帧都达到 35 dB，防止少数坏帧被平均值掩盖。

| 输入 | 完整帧数 | 整段 PSNR / dB | 最差单帧 / dB |
|---|---:|---:|---:|
| 720p，两个 GOP | 60/60 | 52.49 | 51.86 |
| 1080p | 30/30 | 51.95 | 51.46 |
| 4K | 8/8 | 51.97 | 51.65 |
| 1080p，编码器缓冲池输入 | 30/30 | 51.95 | 51.46 |

独立的默认 `VDENC_WEIGHTSOFFSETS_STATE` 实验并未解决 P 帧挂起，于 17:50:40 产生 VCS0 reset；该状态没有合入成功组合。18:19:02 与 18:22:51 还有应用为 `mediaanalysisd`、Signature 1000 的独立报告，不能据此声称整机稳定，也不能直接归因于自有 VCS0 编码实验。

CPU 验证共 30 项通过：状态转换 3、参考审计 3、地址布局 1、采集器 4、VPHAL 分流 8、编码验收 11。原始六帧重建图、命令及五组码流保留在 build 目录；本节提交小型 JSON 证据，未提交大体积原始图像。

此时的结论限定为**探针进程中、Usage=1、单活动参考、已测 NV12 I/P 输入通过**。独立 VA 候选、BGRA、普通像素转换、硬解回归、Usage=0、系统 XPC 编码、低码率、并发和长期稳定性仍需后续验证，尚不能发布系统通用 Intel 编码能力。

### 独立 VA 候选与回归（19:14–19:19）

新增 [Gen12 VA 候选生成器](../../macos/vadriver/encode-experimental/patch_vdencgen12.py)，将两项有效修正直接写入独立 VA 副本。候选在 walker 生成处直接写入 108 字节，不在提交时追加命令；整帧预估和每 slice 累计均增加 23 DWORD。提交前在原 `fflush` 调用点执行两遍参考检查与映射，保留原 `fflush` 调用、资源 flag 和非单参考路径。

补丁使用已核对空闲的 `0xca0`、`0xda0`、`0xf90`、`0x1128`，总计 503 字节；未改变 Mach-O 段布局或增加动态库依赖。原 SPS、VPHAL、pitch、HEVC 与能力节点补丁保留。生成器核对入口和空区字节，只写指定副本，重复执行保持相同结果。

[5 项原生机器码测试](../../tools/diagnostics/test-vdenc-gen12-patch.py) 在普通 CPU 内存中实际执行 walker 与最终映射：检查真实 I/P 命令、四个 DPB 池槽、相邻模板、活动资源标志、I/多参考分支、原调用语义、版本与空区占用。连同此前 CPU 检查，共 35 项通过。

在任何 P 帧执行前，另行采集首帧提交前及第二帧仅绑定的 batch；两帧均与原成功实验的生成结果逐字节一致。采集器将反编译推导的 `encoder+0x10 → video context+0x48` 与原 builder 的实际 context 对比，两帧均一致。首帧完成和第二帧仅绑定的状态均为 0。

硬件回归期间没有 `DYLD_INSERT_LIBRARIES`，候选仅由 `DYLD_LIBRARY_PATH` 供自有探针加载。dyld 日志确认每组编码、转换和硬解加载的是 `va-gen12-native/AppleIntelTGLGraphicsVADriver`。逐组原始报告与选择证据汇总在 [独立候选验收](20261102-kext-icons-and-encode/native-gen12-candidate-validation.json)。

| 编码输入 | 实际帧数 | 整段 PSNR / dB | 最差单帧 / dB |
|---|---:|---:|---:|
| NV12 720p，两个 GOP | 60/60 | 52.49 | 51.86 |
| NV12 1080p | 30/30 | 51.95 | 51.46 |
| NV12 4K | 8/8 | 51.97 | 51.65 |
| NV12 1080p，缓冲池输入 | 30/30 | 51.95 | 51.46 |
| BGRA 1080p | 30/30 | 44.82 | 39.50 |
| NV12 720p，全 I 回归 | 4/4 | 53.04 | 51.75 |

P 帧测试均由软件解码确认实际 I/P 类型，包含池轮换和 30 帧 GOP 边界；没有使用强制 intra 补丁。BGRA 使用探针新增 `--bgra`，将已知 NV12 显式软件转为 BGRA，再交给编码器；对原 NV12 的 PSNR 包含颜色往返和编码误差。每帧均超过 35 dB。所有探针均在 Usage=1、8 Mbit/s、30 fps 下测试，未将此结果外推到其他码率与参考结构。

普通转换五项均与显式软件参考逐像素一致，包括 NV12 拷贝、NV12/BGRA 双向转换及两种格式缩放；这验证的是普通 VPHAL 创建阶段回退，不能称为 Intel 硬件颜色转换通过。强制 Intel 本地 factory 的 H.264 1080p/4K、HEVC 1080p/4K、HEVC Main10 1080p 共 92 帧，与新采集的软件解码参考逐像素一致。

截至 19:19 后的报告检查，本组实验未发现新增 GPU reset 报告。18:23:44、18:27:01、18:27:35 的 `mediaanalysisd`、Signature 1000 报告在候选测试之前，另行保留；本次启动早前已有 reset，长期稳定性仍未验收。

系统 VA 文件仍为 2026-10-30 13:29:39 安装的版本；本节只生成和测试 build 副本，**系统驱动与 EFI 均未改动，目前不需要重启**。下一阶段尚需普通 Usage=0、系统 XPC 编码、低码率、并发与长期稳定性验收，之后再决定系统安装范围；HEVC 编码也未验收。
