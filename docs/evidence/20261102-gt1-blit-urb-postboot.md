# Blit3D URB 修补后的首次重启验收

本次启动时间为 **2026-11-02 13:36:53 +0800**。用户报告已重启后，读取实际 boot-args、系统安装驱动及注册表，再用系统路径的新进程执行 GPU、图标与解码回归。本轮没有修改驱动、EFI、NVRAM 或内核扩展。

## 实际加载与显示角色

- `ngreen-eu=32 ngreen-l3=4 -ngreenheadless` 生效，无 `-x`。
- 系统安装主程序的两个 Blit3D VS URB 字段均为 `0x10030200`，即 512 条目；bundle 签名验证通过。
- 新探针的 dyld 记录确认加载 `/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle` 主程序和同目录 `libxedrv.dylib`，未指定候选路径或绕过原有拷贝补丁。
- Intel Metal registryID=`4294968992`，`headless=1`，能力为 32 EU / 1 slice / L3=4；AMD registryID=`4294969024`，`headless=0`。
- IGPU 有 `XeNG-Headless-IONDRV=blocked`。原生 `AppleIntelFramebufferController` 和 `IONDRVFramebuffer` 都没有实例，系统仅发布四个 AMD framebuffer。
- 系统报告列出 RX 6750 XT，Metal 3、12 GB，实际主显示器为 2560×1440@75 Hz。Intel 作为无头 Metal 设备的证据来自枚举和实际执行，系统报告只列出显示独显不作为失败判据。

## 重启后硬件执行结果

| 用例 | 结果 |
|---|---|
| Intel buffer 清零、对齐/非对齐上传、覆盖与 private 串联，4 轮 | 432/432 通过 |
| AMD 相同 buffer 矩阵，2 轮 | 216/216 通过 |
| Intel 对齐上传后索引绘制 | compute、两个绘制及固定点，4/4 通过 |
| Intel 非对齐上传后索引绘制 | 4/4 通过 |
| Intel 普通非索引渲染 | 4/4 通过 |
| AMD 对齐上传后索引绘制 | 4/4 通过 |
| 120 个 App × 3 尺寸，NSWorkspace | 360/360 非空 |
| 120 个 App × 3 尺寸，直接读取资源 | 354 个有效样本与 AMD 通道差不超过 1/255，6 个无有效输入在两颗 GPU 上相同 |

NSWorkspace 的跨进程批量像素比较中，Xcode 和 Journal 的 512 px 图标分别出现最大 97/255 和 15/255 的通道差；没有图标输出空白，不能仅凭覆盖率相同就忽略这些差异。

为复查两个差异样本，新增一次性证据探针：每轮只调用一次 `layerContentsForContentsScale:`，将同一个 `_NSImageLayerContents` 对象交给两颗 GPU，保留原批量页面尺寸和图块位置，交替 GPU 执行顺序。每图标重复三次，合计 **6/6 通过**，全部通道差不超过 1/255。该复测未复现固定输入下的 GPU 差异；此前跨进程差异的原始输入对象未保存，仍保留在结果记录中，不将其直接认定为驱动错误或证明其具体来源。

探针源码和六次输出放在本记录的同名证据目录。原始批量图像与完整结果在 `build/validation-postboot-blit-urb-20261102-133653/`。

## 硬解回归

相同固定输入分别要求硬件解码，并与此前已保存的软件解码完整帧参考逐像素对照。本轮五组硬件会话属性均为真，VTDecoderXPCService 实际加载 Intel TGL VA bundle。

| 输入 | 帧数 | 有效 Y/UV 像素差 |
|---|---:|---:|
| H.264 1080p | 30 | 0 |
| HEVC 1080p | 30 | 0 |
| HEVC Main10 1080p | 16 | 0 |
| H.264 4K | 8 | 0 |
| HEVC 4K | 8 | 0 |

合计 92 帧。硬件会话成功、帧数、PTS、图像布局、有效像素长度与像素内容全部通过。该验收没有使用单一颜色样本或仅凭回调成功判断。

## 仍需确认的范围

截至证据目录中 `crash-reports-final.json` 记录的采样时间，本次 13:36:53 启动之后没有发现新增 GPU reset、panic、WindowServer 或视频编解码服务崩溃报告。这是有限观察窗口，不能代替长期稳定性或休眠唤醒验收。

本轮独立复现和图标回归通过；用户原先在应用内部看到的项目文字、菜单、SVG 与按钮缺失，还需要用户在当前启动中复看，实际界面暂记为待确认。Intel 硬编码未在本轮重新验收，不能将硬解通过等同于硬编码可用。
