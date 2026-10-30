# 2026-10-30 独显显示模式图形与编解码复核

## 本轮所处启动状态

- 当前系统启动时间：2026-10-30 16:22:55。
- 已挂载 EFI 的 `config.plist` 修改时间：2026-10-30 16:37:58。
- 磁盘配置包含 `AAPL,iokit-ignore-ndrv=<01>`，但当前运行的
  `IOService` 和 `IODeviceTree` 中都找不到该属性。
- 当前运行树仍有 IGPU 子项 `IONDRVFramebuffer`，并有
  `AAPL,ndrv-dev=Yes`。

因为 EFI 配置在本次启动之后才修改，本次运行没有验证该属性能否阻止
IONDRV 匹配。Ghidra 12.1.4 对当前 Tahoe `IONDRVSupport` 的反编译显示：
`IONDRVFramebuffer::probe` 在调用基础 framebuffer probe 前先向 provider
查询 `AAPL,iokit-ignore-ndrv`；该属性存在时直接返回空。

## 图标和合成复现

- `tools/diagnostics/xe-icontest.m`：Calculator、Notes、App Store、Chrome
  和 Finder 的 `NSWorkspace iconForFile:` 输出在 32/128/512 像素测试中均为
  0% 不透明像素；HeliPort 图标约为 59% 不透明。
- 直接从 Calculator 的 `AppIcon.icns` 创建 `NSImage` 可得到约 61.4% 平均
  alpha；从 `NSWorkspace` 取图的同一路径为 0%。ICNS 文件本身有效。
- 强制 Metal 设备的 `xe-carender` 测试中，RX 6750 XT 可合成普通 sRGB 与
  图标层；Intel TGL 模拟设备的普通 sRGB 图层输出透明，使用特定扩展浮点
  色彩空间的图层可以显示。
- 把图标直接从 ICNS 文件载入后，RX 路径可合成 NSImage/CGImage 图标层；Intel
  路径仍出现格式相关的透明输出。普通 CoreAnimation 色块在 RX 路径正常。
- Intel 与 AMD 的加速器树上都能找到 `iconservicesagent` 客户端。当前 Metal
  默认设备是 RX 6750 XT；Intel TGL 被枚举为 `headless=false`，不支持 Metal 3。

这些结果证明图标问题可以复现，并且 Intel TGL 合成路径存在格式相关的
空白；但它们还不能证明所有毛玻璃异常由同一原因造成，也不能单独确定
IconServices、Intel Metal 驱动及窗口服务器的具体责任边界。

## RX 6750 XT 卡顿记录

- 诊断目录中有 13 份 GPU restart 报告；全部标记为 RX 6750 XT 的 GFX 通道
  重置，待处理任务包括 Chrome、WindowServer 和 Codex。
- 这些报告的 VM protection fault 计数为 0；其中一份记录两个 RSMU timeout。
- 最新报告时间为 15:38，早于当前启动时间 16:22。故当前启动还没有新 GPU
  restart 证据，历史记录也不能单独说明哪一次任务导致卡顿。

## VideoToolbox 测试修正与结果

原 `xe-vttest.m` 和 `xe-vtdec.m` 把 `420v`、`BGRA` FourCC 写成 Objective-C
多字符字符常量，Clang 会把它们缩窄为一个字符；VideoToolbox 因格式无效而
返回 `-6680`。修复后的诊断使用完整的 `OSType` 数值，变更单独记录在提交
`830a1d0`。

- 修正后，H.264 与 HEVC 的硬件解码会话均报告硬件解码已启用，回调成功，
  每个会话可返回 30 帧。
- 加入已知图案的独立复测后，H.264 硬件解码回调给出全零 YUV，而同一输入的
  软件解码输出符合预期的 YUV 41/240/110。会话成功不等于像素结果正确。
- `VTSessionCopyProperty` 报告硬件解码已启用，但所选 GPU Registry ID 为空；
  API 文档把空值同时用于内置 GPU 与软件解码，因此不能只靠该字段确认具体
  使用 Intel 还是 AMD。IORegistry 中同时存在 Intel 与 AMD 的
  `VTDecoderXPCServ` 加速器客户端。
- 当前 HEVC 合成输入在不同路径上的像素结果不一致，因此这轮 HEVC 结果仍需
  用固定参考码流和逐帧画面检查确认，不能按“30 帧回调成功”判为通过。

因此，原先仅按会话创建和帧计数判硬解通过的标准不充分；至少还要检查解码
像素，并把硬件是否启用、实际 GPU 归属和输出内容分别记录。

## 下一次启动后的判定项

先只验证当前 EFI 已经包含的 NDRV 忽略属性，不叠加驱动代码变化：

```sh
ioreg -p IOService -r -n IGPU@2 -l -w0 | grep -E 'AAPL,iokit-ignore-ndrv|IONDRVFramebuffer|AAPL,ndrv-dev|XeNG-Trace'
ioreg -p IODeviceTree -r -n IGPU@2 -l -w0 | grep -E 'AAPL,iokit-ignore-ndrv|name|IOName'
ioreg -rc IntelAccelerator -d1 | grep -E 'xe-topo|IOVARendererID|IOGVAHEVCDecode'
```

重启后必须确认属性出现在实际 IGPU 节点，并确认该 IGPU 下是否还注册
`IONDRVFramebuffer`。如果属性未出现，应先查清启动使用的 EFI/DeviceProperties
注入路径；如果属性出现而 IONDRV 仍注册，再继续分析 probe 的实际 provider
和匹配调用链。图标合成与 H.264/HEVC 像素测试分别记录，不用其中一项代替
另一项的结论。
