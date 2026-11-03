# Usage=0 前置统计任务与 VFE 分派核对（2026-11-03）

本轮在[创建映射与截停诊断](20261103-usage0-remap-diagnostic.md)基础上，继续还原普通编码的第一条 RCS 任务。系统 VA、内核扩展和 EFI 均未修改，无需重启。VFE legacy 副本只放行过一次统计调度，随后该进程报告 GPU reset；普通 Usage=0 完整编码仍未通过。

动态截停、CPU 构建字节、实验副本差异与限制见[结构化证据](20261103-usage0-frame-statistics-validation.json)。严格截停采集与历史上放行过一次统计调度的实验分别记录，不能把探针退出码 77 当作整个实验没有 GPU 提交的证明。

## 硬件身份与指令目标

本机按用户提供的 Q1LP 魔改 i9-13950HX 配置记录。Intel 规格列出其集显为 Intel UHD Graphics for 13th Gen Intel Processors、32 EU、PCI ID `0xA788`、支持 Quick Sync 和两个多格式编解码引擎。Alpine 采集的物理 PCI ID 为 `8086:A788`，拓扑为 32 EU / 1 slice / 2 DSS；Linux 6.12 的 RPL-S 设备表将其映射到 `adl_s_info` 和 `GEN12_FEATURES`，i915 日志报告 display version 12.00、stepping D0。macOS IORegistry 显示注入后的 `device-id=0x9A49` 及 Iris Xe 模型，这是为加载 TGL 兼容驱动而使用的身份，不是物理 PCI ID。当前固件/IORegistry 证据和平台设备表彼此区分记录。

本地 IGA 的 `12p1` 模型映射到 XE/TGL/TGLLP/DG1，没有独立 RPL-S 别名。原始 8,272 字节统计 kernel 可按 IGA Gen11 解码，但直接按 `12p1` 解码有 896 处指令错误；这说明它不是可直接用于 IGA Gen12 解码的二进制。后续离线转换候选由 IGA 按 `12p1` 重新编码，候选汇编和往返解码通过，只证明 IGA 能自洽处理转换后的字节，不证明和 A788 硬件 ISA 或原 kernel 语义等价。

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

同一诊断库增加了 kernel 存储字段的只读采集。2026-11-03 16:03 的严格截停复测重复观察到：容量 `12288` 字节，原 kernel 装入后已用 `8320` 字节，原 kernel 长 `8272` 字节，副本地址与存储基址一致。`0x2c566` 的容量检查是 `used + length <= capacity`，写入后按 64 字节递增；因此原 kernel 对齐占用 `8320` 字节。离线 Gen12 候选为 `8824` 字节，对齐需 `8864` 字节；若在初始化阶段替换唯一原 kernel，低于 12 KiB 容量；若在已装入原 kernel 的 context 后追加，则超出剩余 `3968` 字节，helper 会拒绝。容量不是替换路径的阻塞点；候选指令语义和执行行为尚未证明。

这些零值不能直接作为故障原因：首帧没有前一帧，`0x21d22` 有对应的清零分支；关联输出仅在输入像素格式为 `2vuy` 时使用，本次探针输入为 NV12；块数由 `0x22236` 在原调度之后写入，720p 应为 80×45=3,600。严格截停采集没有到达最终 PAK；历史放行实验捕获了后续 PAK 请求字段，但该请求没有进入其原调度，也没有证明帧布局兼容。

## 主机 CPU 夹具与历史重置的关联

新增 `tools/diagnostics/gpu-tests/inspect-frame-statistics-cpu.py`，只映射已分析 VA 的 `__TEXT`，在普通内存夹具中执行 `0x2c84a`、`0x2cc16` 及其三个 CPU 辅助函数。它不加载 bundle，不调用 VA 调度、IOKit 或表面绑定函数。线程数是调用者明确提供的夹具参数，不代表本次实机线程采集。

720p / 224 线程 CPU 夹具生成。224 是显式传给分析脚本的数值，不是从实机读取的 CPU 线程数：

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

2026-11-03 13:23:14 启动的受控探针使用 VFE legacy 副本。日志记录统计调度返回 `status=0`，并在后续 PAK 请求的原调度入口前退出 77；日志文件修改时间为 13:23:23，单条事件没有精确墙钟时间。系统于 13:23:24 生成 `Kernel_2026-11-03-132324_Mac-Pro.gpuRestart`，Application 为 `xe-h264-encode-m`，Signature 813、IPEHR 为 `0x7103000f`、ACTHD 为 `0x400100f0`。统计任务已提交但返回 0 不等于 GPU 完成；reset 与探针同进程、时间相邻，但没有完整硬件 batch 或可配对的 submission trace ID，因此不能宣称已唯一证明统计 kernel 根因。后续严格截停复测没有调用原调度，截至复查未观察到更新的 GPU reset 报告。

Intel Gen12 定义中的该 RCS walker 本身也是 17 DWORD；此前 VDENC 的 27 DWORD 修补针对另一种命令 `0x7087…`，不能照搬到这里。

## VFE 分派差异与独立实验副本

Intel 官方 Gen12 `MEDIA_VFE_STATE.DW3` 的 bit 6 为 `FusedEuDispatch`，值 1 表示 legacy 模式、关闭融合 EU 分派。官方 `CodechalEncodeAvcEncG12::AddMediaVfeCmd` 和 `CodechalVdencAvcStateG12::AddMediaVfeCmd` 都将 `bFusedEuDispatch` 设为 false，渲染接口将其转换成该位的 1。

当前 VA 的通用媒体 walker 构建器在 `0x2c9ae` 执行 `OR EDX,0x1000`，生成的该位为 0。新增 `macos/vadriver/encode-experimental/patch_vfelegacy.py` 仅将其改成 `OR EDX,0x1040`：

- 文件只改变 `0x2c9b0` 的一个字节，`0x00 → 0x40`。
- 720p 和 1080p 的原生 CPU 对照中，batch 只改变 `0x74` 处 VFE DW3 的 bit 6。
- walker、CURBE、描述符加载、资源引用、线程字段及后续命令保持原有字节。
- 作用域是通用媒体构建器的 walker 分支，包含统计任务，并非仅限某个 context；MEDIA_OBJECT 分支和另一个 AVCENC 构建器不在本次修改范围。

验证副本位于 `build/validation-encode-and-kext-icons-20261102/va-vfe-legacy-cpu-only/AppleIntelTGLGraphicsVADriver`。它未重新签名、未加入安装链，原 `va-gen12-native` 保持不变。随后 13:23 的受控统计探针确实加载了这个副本，只放行一条 `FrameStatisticsContext` 原调度；VA 返回 `status=0`，随后出现 Signature 813 的 RCS reset。因此 VFE DW3 bit 6 的 legacy 分派单独不足以解决统计任务卡死；这次仍不是完整 Usage=0 编码验收。

即使后续验证统计任务恢复，普通 PAK 与 VDENC 的帧接口错配仍独立存在，不能直接取消映射诊断的强制截停，也不能将该副本作为可安装的 Usage=0 驱动。

## 候选 Kernel 边界

原始 kernel 与离线转换候选各有 581 条指令。消息条数相同：4 条媒体块读取、2 条 HDC 媒体块写入、1 条 DC0 oword 双块写入、1 条 EOT。候选将其改写成 XE `send.dc1`、`send.dc0` 和 `send.ts` 形式。该表面对应关系尚未证明地址模式、缓存策略、头寄存器和数据通道语义相同，也没有核对额外 DC0 写入对 AppleGVA 下游的预期结果。

原始前三条读取包含 `{NoPreempt}`。候选不包含该标记；随仓库 IGA 的 `Models.hpp` 规定 XE send 模型不支持 `NoPreempt`，其 GED encoder 对不支持的平台会丢弃该选项。旧控制项不能直接带入新编码格式，其在原平台的作用与目标平台是否有对应要求仍需核对。标记变化本身不能证明候选一定有故障，更不能解释此前 reset 的唯一根因；转换候选没有安装或提交到 GPU。13:23 VFE legacy 探针运行的是另一份 VA 二进制和原始统计 kernel，不能算作转换候选的硬件测试。

## 复现与后续边界

CPU 构建核对可执行：

```sh
python3 tools/diagnostics/gpu-tests/inspect-frame-statistics-cpu.py \
  build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver \
  --hardware-threads 224
python3 tools/diagnostics/gpu-tests/test-frame-statistics-cpu.py
python3 tools/diagnostics/gpu-tests/test-va-command-trace.py
```

主机 CPU 夹具测试需要上述已构建的独立 VA，不以缺失输入或 0 项发现作为通过。先前一轮历史验证运行过 12 个测试文件、57 项测试；本轮诊断门控修改后重新运行 `test-va-command-trace.py` 和 `test-frame-statistics-cpu.py`，共 2 个文件、10 项测试通过，诊断库严格编译也通过。完整 Usage=0 编码仍未通过；早前 RCS reset 的统计任务归因仍不唯一。

下一步需基于 RPL-S/A788 的权威 ISA 与消息定义，重新建立统计 kernel 各读写操作的格式和同步语义，并继续还原普通 VME/PAK context 与帧接口。context 容量为 12 KiB，单个 8,824 字节候选若能被证明等价，可替换唯一原 kernel；追加到已有原 kernel 后则不够容量。当前诊断库删除了允许首条调度通过的两个环境变量，Usage=0 映射开启时一律在原调度前退出。系统 VA、EFI 与安装目录未修改。

参考源码来自 Intel 官方 `intel/media-driver`，2026-11-03 下载到同一验证目录：`mhw_render_hwcmd_g12_X.h:701`、`:1026`，`mhw_render_g12_X.cpp:32`，`codechal_encode_avc_g12.cpp:5271` 和 `codechal_vdenc_avc_g12.cpp:2158`。参考仓库为 `https://github.com/intel/media-driver`。
