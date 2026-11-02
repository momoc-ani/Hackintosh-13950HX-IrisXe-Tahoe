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
