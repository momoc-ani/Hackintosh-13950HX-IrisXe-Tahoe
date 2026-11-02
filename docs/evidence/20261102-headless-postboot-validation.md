# 4-bank 无头模式启动验收

本次启动：2026-11-02 11:50:41。

## 当前结论

`ngreen-l3=4` 已由 EFI 在正常启动中生效；未通过测试程序临时改能力值。Intel 32 EU GT1 的顶点 ID、实例 ID、顶点缓冲属性、固定位置点、完整三角形和 compute 回归全部通过。RX 6750 XT 继续负责 2560×1440@75 Hz 的显示；Intel 被 Metal 枚举为 headless，系统仅发布 AMD 的四个 framebuffer。

120 个已安装 App 的 NSWorkspace 图标在 32/128/512 像素下共 360 项，均有有效图像；Intel 合成与 AMD 对照的最大 BGRA 通道差为 1/255。五组共 92 帧 H.264/HEVC 硬解输出，与软件解码的完整有效 Y/UV 像素一致。

**不能把当前状态写成“全部所需功能正常”。** 显式要求 Intel GPU 创建 H.264、HEVC 硬件编码会话均返回 `-12908`（找不到编码器）。用户还报告应用内部项目名称行、菜单区域有元素缺失；其渲染缓存已备份并重建，用户已在重开后确认仍然缺失，已排除仅由旧缓存导致。长时间负载和休眠唤醒未纳入这轮验收。

## 启动与渲染

- 实际 boot-args 中为 `ngreen-eu=32 ngreen-l3=4`，没有 `-x`，保留 `-ngreenheadless`。
- Intel `xe-topo=8196`（`0x2004`）；Metal 报告 `headless=1`。`DisplayPipeSupported=0`、`TransactionsSupported=0`。
- 加载的 NootedGreen UUID 为 `448B99E3-98B4-3FE1-AC66-BCF731B0A4A7`，与写入参数前的二进制相同；此次修正未依赖新 kext。
- `xe-vertex-urb --gpu intel`：compute 4096/4096；6 个 vertex/instance ID 组合正确；显式顶点缓冲属性正确；三角形 4096/4096 蓝色像素；16×16 点为 256 蓝色像素。4/4 测试通过。
- `xe-vertex-urb --gpu amd`：相同四项通过。
- `system_profiler` 仍只在显示器栏目列出 RX 6750 XT 的显示链路；它不是 Intel headless 渲染失败的依据。

## 图标和缓存

首次取图（清缓存前）的 360 项 NSWorkspace 样本已全部非空。随后依用户要求，备份移走当前用户的 `com.apple.iconservices` 索引和 `com.apple.dock.iconcache`，并向用户图标服务、Dock、Finder 发送重启请求；Dock、Finder 已以新进程恢复。

清理后重新采集完整数据：

| 来源 | Intel 样本 | AMD 样本 | 空白输出 | 最大通道差 |
|---|---:|---:|---:|---:|
| NSWorkspace / IconServices | 360 | 360 | 两者均为 0 | 1/255 |
| 直接读取应用声明图标资源 | 360 | 360 | 两者均为 6 | 1/255 |

直接读取资源时，Books 的旧 ICNS 图像没有有效像素、用户目录中的 URL handler 未声明图标，各产生三个无有效输入的样本。NSWorkspace 能为这两个应用提供图标，三种尺寸均非空，因此这六项未算作 GPU 渲染失败。

用户提交截图显示，图标恢复后仍有应用内两处元素缺失。检查其文件缓存后，备份移走 11 处 Graphite/Dawn/GPU/Metal 渲染缓存，合计 29,122,233 字节。应用于约 12:25 完整重开后，主进程变为 PID 12642，新 GraphiteDawnCache 已生成。

缓存备份与逐项路径保存在 `build/validation-20261102-115041/chatgpt-render-cache-refresh.json` 及同目录的 `chatgpt-render-cache-backup-*`，系统图标缓存备份为 `icon-cache-backup-*`。没有修改应用代码、账户或会话存储。用户在应用重开后确认项目名称、菜单以及输入框按钮图形仍有缺失；第二张截图补充了更多应用内图标异常，因此已将“仅缓存导致”排除。应用实时渲染路径尚未修复。

## 硬件解码像素

通过 libx264/libx265 的软件编码生成固定 testsrc2 MP4。`xe-codec-decode` 用 AVAssetReader 读压缩包，分别要求硬件解码和禁用硬件解码，按 PTS 排序后导出每帧有效 Y/UV 行。`compare-codec-frames.py` 同时检查会话属性、帧数、PTS、图像布局、文件长度和完整像素。

| 输入 | 尺寸 | 硬解帧数 | 回调/提交错误 | 与软件参考的有效像素差 |
|---|---|---:|---:|---:|
| H.264 8-bit | 1920×1080 | 30/30 | 0 | 0 |
| HEVC 8-bit | 1920×1080 | 30/30 | 0 | 0 |
| HEVC Main10 | 1920×1080 | 16/16 | 0 | 0 |
| H.264 8-bit | 3840×2160 | 8/8 | 0 | 0 |
| HEVC 8-bit | 3840×2160 | 8/8 | 0 | 0 |

另用提交前整理后的工具重复验证 30 帧 H.264 1080p，硬件与软件有效像素仍一致。已有 `xe-vttest` 的 8 帧已知蓝色画面也得到 Y/Cb/Cr=`41/240/110`。

五组硬件会话的 `UsingHardwareAcceleratedVideoDecoder=true`。运行中的 VTDecoderXPCService 实际加载 `/Library/GPUBundles/AppleIntelTGLGraphicsVADriver.bundle`，IntelAccelerator 下存在 VideoContextMain/Media/VEBox。公共 API 的 GPURegistryID 对内置解码器返回空，因此该空值本身不作为具体 GPU 归属证据；本轮也未为单帧新增硬件寄存器采样。

## 编码状态

`xe-encode-capability intel h264` 和 `intel hevc` 均在显式指定 Intel registryID、强制硬件编码时返回 `-12908`。这说明当前安装配置不能创建所需的 Intel 硬编码器，未进行 Intel 编码帧验收。

默认 HEVC 路径和显式 AMD HEVC 可以创建硬件编码会话；只创建会话不说明生成码流正确，所以也未把这些项目列为编码通过。没有启用实验性编码补丁，当前配置继续保留已验证的渲染与解码路径。

## 稳定性与可复跑证据

截至最后一次报告采集，未发现 11:50:41 本次启动之后新增的 GPU restart、panic、WindowServer 或视频服务崩溃报告。11:47:21 的 GPU restart 早于本次启动，不能计入本次稳定性结论。此项仅说明这轮有限测试未发现上述报告，不代表长期稳定性已验收。

精简证据随版本保存在 `docs/evidence/20261102-headless-postboot/`；完整视频、原始图像、日志与缓存备份在 `build/validation-20261102-115041/`。编译和用法见三个诊断源文件开头的注释：`xe-codec-decode.m`、`compare-codec-frames.py`、`xe-encode-capability.m`。原有渲染/图标工具不变。
