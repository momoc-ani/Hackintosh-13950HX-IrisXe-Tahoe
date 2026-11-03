# Usage=0 前置统计任务与 VFE 分派核对（2026-11-03）

本轮在[创建映射与截停诊断](20261103-usage0-remap-diagnostic.md)基础上，继续还原普通编码的第一条 RCS 任务。系统 VA、内核扩展和 EFI 均未修改，无需重启。新增副本仅通过 CPU 核对；Usage=0 真实编码仍未通过。

动态截停、CPU 构建字节、实验副本差异与限制见[结构化证据](20261103-usage0-frame-statistics-validation.json)。没有执行已知失败的映射编码路径，没有绕过原调度前退出保护。

## 前置链的实际类型与字段

Ghidra 对独立 `va-gen12-native` 的观察如下：

| 地址或字段 | 观察 |
|---|---|
| 构造器 `0x21a9a` | 设置 vtable `0x7549b0`，块宽和块高各 16 |
| RTTI 名称 `0x31f51c` | `26AVD_FrameStatisticsContext` |
| vtable `+0x28` | 通用调度入口 `0x2190a` |
| vtable `+0x60` | 统计参数构建入口 `0x22236` |
| vtable `+0x88` | 表面状态构建入口 `0x21d22` |
| 帧参数 `+0x00/+0x08` | 当前表面和前一帧表面，各 8 字节 |
| 帧参数 `+0x10/+0x14` | 统计槽与块数，各 4 字节 |
| 下一节点的帧参数 `+0x08` | 关联转换输出表面 |

`0x22236` 将第一节点的当前/前一帧表面保存到 context，并读取下一节点的关联表面。因此，第二节点 `kind=16/cap=0x20` 没有独立 context，不能据此判定创建异常。

新增采集严格区分统计任务与 VDENC：统计帧只有 `0x18` 字节，不能套用包含 `+0x440` 等字段的编码大结构。CPU 测试将其结尾贴到不可访问内存页，验证 `+0x10/+0x14` 分别按 32 位读取、不越界、不解引用表面数值；同时验证空 context 与未知 kind。

## 原调度前动态采集

新诊断库为 `build/validation-encode-and-kext-icons-20261102/xe-va-statistics-trace-lab.dylib`。Usage=0 和 Usage=1 均以 77 退出，最后事件为 `stop-before-original`，没有调用原 `ScheduleAVDCommands`。

Usage=0 的首条链仍为 `kind=2/cap=0x1000 → kind=16/cap=0x20`，实际 context 为：

- 分辨率 1280×720，分块 16×16。
- CURBE 为 32 字节，内核数为 1，首个内核长 `0x2050`（8,272）字节。
- 当前表面非空，前一帧表面为空，统计槽为 0，调度前块数为 0。
- 第二节点的关联输出表面为空。

这些零值不能直接作为故障原因：首帧没有前一帧，`0x21d22` 有对应的清零分支；关联输出仅在输入像素格式为 `2vuy` 时使用，本次探针输入为 NV12；块数由 `0x22236` 在原调度之后写入，720p 应为 80×45=3,600。当前仍未获得 Usage=0 最终 PAK 帧参数的动态快照。

## 原生 CPU 构建与历史重置的关联

新增 `tools/diagnostics/gpu-tests/inspect-frame-statistics-cpu.py`，只映射已分析 VA 的 `__TEXT`，在普通内存夹具中执行 `0x2c84a`、`0x2cc16` 及其三个 CPU 辅助函数。它不加载 bundle，不调用 VA 调度、IOKit 或表面绑定函数。线程数是调用者明确提供的夹具参数，不代表本次实机线程采集。

720p / 224 线程夹具生成：

| 命令或位置 | CPU 结果 |
|---|---|
| 整条 batch | 288 字节 |
| `MEDIA_VFE_STATE` | 9 DWORD，头 `0x70000007` |
| `MEDIA_CURBE_LOAD` | 4 DWORD，载入 32 字节 |
| `MEDIA_INTERFACE_DESCRIPTOR_LOAD` | 4 DWORD，载入 64 字节 |
| `MEDIA_OBJECT_WALKER` | 17 DWORD，头 `0x7103000f`，网格 80×45 |
| walker 起点 / 终点 | `0xAC` / `0xF0` |

1080p / 112 线程夹具另确认网格向上取整为 120×68；线程参数只改变对应的 VFE 字段。

历史失败报告 `Kernel_2026-11-03-101651_Mac-Pro.gpuRestart` 中，IPEHR 为 `0x7103000f`，batch 基址为 `0x40010000`，ACTHD 为 `0x400100f0`。其相对偏移 `0xF0` 与本次 CPU 构建的 walker 终点相符。这将调查收敛到统计任务的媒体 walker / 执行状态边界，但没有同次完整 GPU batch，不能仅凭偏移相符宣称已唯一证明卡死节点或内核根因。

Intel Gen12 定义中的该 RCS walker 本身也是 17 DWORD；此前 VDENC 的 27 DWORD 修补针对另一种命令 `0x7087…`，不能照搬到这里。

## VFE 分派差异与独立实验副本

Intel 官方 Gen12 `MEDIA_VFE_STATE.DW3` 的 bit 6 为 `FusedEuDispatch`，值 1 表示 legacy 模式、关闭融合 EU 分派。官方 `CodechalEncodeAvcEncG12::AddMediaVfeCmd` 和 `CodechalVdencAvcStateG12::AddMediaVfeCmd` 都将 `bFusedEuDispatch` 设为 false，渲染接口将其转换成该位的 1。

当前 VA 的通用媒体 walker 构建器在 `0x2c9ae` 执行 `OR EDX,0x1000`，生成的该位为 0。新增 `macos/vadriver/encode-experimental/patch_vfelegacy.py` 仅将其改成 `OR EDX,0x1040`：

- 文件只改变 `0x2c9b0` 的一个字节，`0x00 → 0x40`。
- 720p 和 1080p 的原生 CPU 对照中，batch 只改变 `0x74` 处 VFE DW3 的 bit 6。
- walker、CURBE、描述符加载、资源引用、线程字段及后续命令保持原有字节。
- 作用域是通用媒体构建器的 walker 分支，包含统计任务，并非仅限某个 context；MEDIA_OBJECT 分支和另一个 AVCENC 构建器不在本次修改范围。

验证副本位于 `build/validation-encode-and-kext-icons-20261102/va-vfe-legacy-cpu-only/AppleIntelTGLGraphicsVADriver`。它未重新签名、未供 GPU 运行、未加入安装链，原 `va-gen12-native` 保持不变。**这只验证了分派差异及修改范围，尚未证明它能解决 RCS 重置。**

即使后续验证统计任务恢复，普通 PAK 与 VDENC 的帧接口错配仍独立存在，不能直接取消映射诊断的强制截停，也不能将该副本作为可安装的 Usage=0 驱动。

## 复现与后续边界

CPU 构建核对可执行：

```sh
python3 tools/diagnostics/gpu-tests/inspect-frame-statistics-cpu.py \
  build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver \
  --hardware-threads 224
python3 tools/diagnostics/gpu-tests/test-frame-statistics-cpu.py
python3 tools/diagnostics/gpu-tests/test-va-command-trace.py
```

原生 CPU 测试需要上述已构建的独立候选，不以缺失输入或 0 项发现作为通过。本轮共运行 12 个测试文件、57 项 CPU 测试，包含旧 Gen12 VDENC 和 VPHAL 回退检查；严格编译诊断库通过。审查子任务未产生实际代码审查结果，不计为独立审查通过。

下一步仍需核对统计内核的 EU 指令、接口描述符及真实表面状态，并还原普通编码的 context / 帧接口。当前保持原调度前截停；没有安排重启、系统安装或已知失败路径的再次执行。

参考源码来自 Intel 官方 `intel/media-driver`，2026-11-03 下载到同一验证目录：`mhw_render_hwcmd_g12_X.h:701`、`:1026`，`mhw_render_g12_X.cpp:32`，`codechal_encode_avc_g12.cpp:5271` 和 `codechal_vdenc_avc_g12.cpp:2158`。参考仓库为 `https://github.com/intel/media-driver`。
