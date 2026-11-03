# H.264 hardware encode: work in progress (not part of the build)

2026-11-02 / 13950HX A788 更新：[`patch_vphalfallback.py`](patch_vphalfallback.py)
在普通 VPHAL 转换的上下文创建阶段返回不支持，保留编码器内部 scaler。
新增 [`patch_vdencgen12.py`](patch_vdencgen12.py)，在 VA 自身生成 27 DWORD 的 Gen12 walker，
同时扩大命令预留、更新 slice 累计，并将单活动参考的 MFX 索引和 token 槽一并映射到零。
**独立 VA 候选已通过 Usage=1 的真实 I/P 编码；尚未安装系统、修改 EFI 或发布全局编码能力。**
不能将此结果外推到 Usage=0、多活动参考、系统 XPC 编码或长期稳定性。
后续创建诊断确认：Usage=0 请求的 `kind=0x20/capability=0x4000` 没有对应的对象创建分支，streamout
得到空 context 并返回 4。复杂动态输入下，4 Mbit/s 实际约 3.98 Mbit/s，而 1 Mbit/s
实际约 1.83–1.86 Mbit/s；帧画质通过不等于码率控制通过。两进程各 60 帧的有限并发已通过。
完整记录、限制和复现命令见
[本轮编码证据](../../../docs/evidence/20261102-kext-icons-and-encode.md)。
2026-11-03 的进一步诊断发现：将该请求映射为 VDENC `0x400` 仅能通过会话创建；
普通 VME/PAK 链与 VDENC 帧参数布局不同，单帧实验同时暴露 RCS 重置和空指针。
该映射保留在默认关闭的进程诊断工具中，并强制在原 VA 调度前截停，不合入候选 VA。
详见 [Usage=0 分派与首帧证据](../../../docs/evidence/20261103-usage0-remap-diagnostic.md)。
同日新增 [`patch_vfelegacy.py`](patch_vfelegacy.py)，先供独立 CPU 验证：通用媒体 walker 构建器的
VFE DW3 bit 6 改为 1，与 Intel Gen12 AVC 的 legacy 分派设置一致。它只改变一个文件字节，
原生 CPU 对照确认 batch 只改变这一位；该副本未重新签名、未加入安装链。随后受控探针加载此副本，
只放行一条 FrameStatisticsContext 调度，返回 0 后同一实验链出现 Signature 813 的 RCS reset。
该实验未证明统计路径恢复，reset 的具体任务归因仍未确认；此副本不能作为已验证的安装候选。
当前 RCS `MEDIA_OBJECT_WALKER` 已是 Gen12 定义的 17 DWORD，不能套用 VDENC 的 27 DWORD 修补。
普通 PAK / VDENC 帧接口错配仍未解决，映射诊断继续强制在原调度前退出。
详见[统计任务、重置边界与 CPU 验证](../../../docs/evidence/20261103-usage0-frame-statistics.md)。
以下 2026-10-24 的 XPS 测试属于历史记录，不能替代当前 A788 验收。

The historical patches below apply, in the listed order, to the 16.0.32 VA plugin prepared by this repository.
They are **not** applied by `install.sh`, because the result is not safe to install yet (see
"Open problems"). Each script checks the original bytes before writing.

| Patch | What it does |
|---|---|
| `patch_vphal.py`, `patch_vpnull.py`, `patch_vpnull2.py` | Let the VPHAL video processor create its context (kernel-table size) and skip the NULL sub-renderers it touches. Needed because AppleGVA's encoder creates a scaler when the session opens |
| `patch_encsps.py` | Tahoe's H.264 SPS layout on the encode side (same shift as `patch_spsshift.py` for decode) |
| `patch_nocksum.py` | Hides the FrameChecksum capability: its EU media kernel hangs the render engine on the first frame |
| `patch_nowe.py` | Hides the WeightEstimate capability: its EU media kernel hangs the render engine from the second P frame |
| `patch_vdencfix.py` | Gen12 VCS status registers (`0x1c08a0…` instead of the Gen9/11 `0x128a0…`; without it every frame is "dropped") and the VDENC surface format (PLANAR_420_8 in the VDENC layout; without it the picture is read as RGBA 10:10:10:2 and comes out stretched 4×) |
| `patch_vdencintra.py` | Interim: VDENC uses its intra template for every frame. The PAK still writes P slices |
| `patch_noscaler.py` | Experiment: hides the VPHAL scaler capability (see below) |

The accelerator must also advertise `IOGVAH264EncodeCapabilities` (`nootedgreen-ngreenenc.diff`, opt-in boot-arg
`-ngreenenc`).

## What works (tested 2026-10-24 on the XPS 13 Plus)

- Low-latency sessions (`kVTVideoEncoderSpecification_EnableLowLatencyRateControl`, as FaceTime and streaming apps
  use), through the system encode service: 1280×720 and 60/60 frames at 4 Mbit/s, Y-PSNR 52.8 dB against the source,
  no GPU hang. VideoToolbox picks the hardware encoder by itself.
- Ordinary sessions (usage 0, e.g. export) fail inside AppleGVA (they need the VME path, whose EU kernels hang this GPU)
  and VideoToolbox falls back to the software encoder.

## Open problems

以下列表为 2026-10-24 XPS 历史记录。当前 A788 中，普通 VPHAL 创建阶段回退、Gen12 walker
及单活动参考映射已经通过限定范围内的进程测试；系统能力发布、Usage=0 和长期稳定性仍未通过验收。
不要将历史 `patch_vdencintra.py` 合入本次候选，它会强制帧内预测，不能验证真实 P 帧。

1. **VPHAL in the whole system.** With the VPHAL patches, `VTPixelTransferSession` (AVFPixelProcess) also starts using
   the GPU video processor for every app, and it crashes on 1920×1080 `420v` buffers. Hiding the scaler capability
   fixes that but then the encoder refuses to open (it needs at least one scaler). The encoder uses the scaler entry at
   `__DATA 0x78fc00`, pixel transfers use `0x787a30`; keeping only the former still crashes in VPHAL (`fn 0x126520`).
   This is what blocks installing.
2. **Inter prediction.** Real P frames hang VCS0 in `MI_FLUSH_DW` right after the first slice. With the VDENC forced to
   intra the same batch does not hang, so the problem is in the VDENC reference path; the VDENC commands are in the
   Gen11 (Ice Lake) layout. Addresses of FwdRef0 / DsFwdRef0 are valid; disabling TLB prefetch, sub-pel search or BME
   does not help.
3. The low-latency rate control drops frames at low bitrates, because intra-only P frames are larger.
4. Each GPU hang is recovered (XE-GPUREC v2 for the render engine, Apple's reset for VCS), but several in a row leave
   the desktop sluggish until a reboot.

## A788 独立候选复现

当前基线为本轮已经通过全 I 与硬解回归的 `va-vphal-fallback`，其中包含编码 SPS、VDENC
表面/寄存器修补和坏媒体 kernel 能力隐藏，不包含强制 intra。补丁生成器只写独立输出副本：

```sh
mkdir -p build/validation-encode-and-kext-icons-20261102/va-gen12-native \
  build/validation-encode-and-kext-icons-20261102/native-hardware
python3 macos/vadriver/encode-experimental/patch_vdencgen12.py \
  build/validation-encode-and-kext-icons-20261102/va-vphal-fallback/AppleIntelTGLGraphicsVADriver \
  build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver
codesign -f -s - build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver
python3 tools/diagnostics/test-vdenc-gen12-patch.py
clang -fobjc-arc -Wall -Wextra -Werror tools/diagnostics/gpu-tests/xe-h264-encode-lab.m \
  -framework Foundation -framework Metal -framework VideoToolbox -framework CoreMedia -framework CoreVideo \
  -o build/validation-encode-and-kext-icons-20261102/xe-h264-encode-lab
```

测试只把候选目录传给自有探针，不需要诊断 dylib。确认调用环境没有遗留的
`DYLD_INSERT_LIBRARIES` 后，再运行有限帧数的硬件测试：

```sh
DYLD_LIBRARY_PATH="$PWD/build/validation-encode-and-kext-icons-20261102/va-gen12-native" \
  build/validation-encode-and-kext-icons-20261102/xe-h264-encode-lab \
  build/validation-encode-and-kext-icons-20261102/native-hardware/inter-720p-60 \
  1280 720 60 --inter
python3 tools/diagnostics/gpu-tests/verify-h264-encode.py \
  build/validation-encode-and-kext-icons-20261102/native-hardware/inter-720p-60 --inter
```

硬件实验可能引起 GPU 重置；CPU 机器码测试不连接 GPU。`--inter` 要求真实 I/P 输出，
默认模式为全 I；`--bgra` 将已知 NV12 先软件转换为 BGRA，并将原 NV12 保留为包含颜色往返的参考。
每帧 PSNR 均须达到 35 dB。当前矩阵包括 720p 60 帧、1080p 30 帧、4K 8 帧、1080p
缓冲池输入及 1080p BGRA 30 帧。候选硬解 H.264/HEVC/Main10 共 92 帧与软件参考逐像素一致，
五种普通转换的回退结果也与软件参考一致。详细证据见上方本轮记录。

用途与码率调查可以传 `--usage 0|1|default`、`--bitrate 1000000`；`--create-only` 只创建并销毁
会话，验收脚本会拒绝将这种报告作为帧编码成功。`--describe-options` 仅输出解析后的参数，不连接 GPU。
`--input-nv12 path` 要求输入包含请求数量的完整有效 NV12 帧，并把实际提交像素保存为参考。
验收脚本的 `--usage`、`--bitrate` 可要求报告条件与实验吻合；其画质通过标志不代表实际输出码率合格。
