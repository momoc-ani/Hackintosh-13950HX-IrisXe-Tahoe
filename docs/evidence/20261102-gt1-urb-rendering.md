# 32 EU GT1 URB 与 Intel Metal 渲染测试

日期：2026-11-02

系统启动：2026-11-02 09:32:35

模式：RX 6750 XT 输出显示，Intel TGL 被 Metal 枚举为 `headless=1`。

状态：根因通过进程内对照得到复现；待 4-bank 启动参数生效后验证系统图标服务和重启稳定性。

## 结论

在当前 `ngreen-l3=8` 配置下，Intel 顶点着色器被调用，但 `vertex_id` 和 `instance_id` 都读回 0，顶点缓冲输入也不能形成正确画面。Metal 命令状态仍为 Completed，compute 通道 4096/4096 正确，因此仅凭命令完成会漏报这个渲染故障。

测试进程中把 TGL 用户态驱动能力结构里的 L3 bank 值从 8 改为 4，并在创建 pipeline 前调用驱动自己的 URB 计算后，系统 ID、顶点属性、固定位置点和三角形像素均恢复。只改能力字段而不重新生成 URB 的对照仍失败。独立 120 个已安装应用图标测试同样显示，4-bank 的 354 个有效 AMD 参照样本全部通过，最大通道差为 1/255。

这将当前顶点 ID 全零和大量 Intel Metal 空白渲染的故障定位到 **32 EU / 1 slice GT1 的 URB 分配仍按 8-bank 计算**。项目已有 `ngreen-l3` 参数会写入 NootedGreen 的 `L3BankCount`；这次测试支持用 `ngreen-l3=4` 修正实际驱动能力。已于 11:42 写入启动 EFI，并通过回读校验；尚未用新参数启动电脑。

系统 `NSWorkspace` / IconServices 图标请求在当前运行会话中另有空白样本：175/360 项在 Intel 4-bank 与 AMD 测试中都在输入阶段为空。这不属于可由 CARenderer 进程内 L3 改值修复的路径；须在新的启动配置生效后重新验证 IconServices 和桌面图标。

## 追踪证据

- 从测试 shader 还原的 AIR bitcode 含 `air.vertex_id` 和 `air.position`，旧 TGL 编译器成功返回；IGA 反汇编显示顶点机器码从 `r2` 读取系统顶点值，并将位置经 `r1` 对应的 URB handle 写出。
- REA/Ghidra 反编译 `IGVertexShaderInfo::initialize`（`0xeba8`）确认驱动初始化顶点程序信息；`generate3DStateVF:posh:`（`0xa212`）按顶点和实例系统值生成 `VF_SGVS` / `VF_SGVS2`；`generate3DStateURB:`（`0xbeab`）读取硬件能力，计算并生成 URB 状态。
- `getUrbSizeInUrbAllocations`（`0xcf0e`）以 `L3Banks / slices * 64 * 0.125` 计算 URB allocation 数。`setupURBState:`（`0xcf48`）使用该总量分配 VS/HS/DS/GS，`populateURBState:stageIndex:startAddress:`（`0xd6a6`）再编码为命令。
- L3=8 时 capture 到 `3DSTATE_URB_VS`（header `0x78300000`）为 `0x10030500`；L3=4 时重新计算成 `0x10030200`。`VF_SGVS` 中 VertexID Enable 已置位，`VF_SGVS2` 的默认系统值设置也存在。前端模板不是失败点；allocation 输入对该芯片不匹配时，才观察到 ID/URB 输出异常。
- 项目中 `ngreen-l3` 在 [kern_gen11.cpp](../../src/NootedGreen/kern_gen11.cpp#L28) 解析，并在 [getGPUInfo](../../src/NootedGreen/kern_gen11.cpp#L6634) 写为 `L3BankCount`。参数默认值仍为 8；32 EU 的 GPU topology fixture 为 1 slice、2 DSS、32 EU。

REA 会话分析对象为实际安装的 `AppleIntelTGLGraphicsMTLDriver`，分析提供者为 Ghidra 12.1.4。反编译内容属于 shipped binary 静态证据；URB 状态与着色器行为另由本机进程内探针验证。

## 对照矩阵

`tools/diagnostics/gpu-tests/xe-vertex-urb.m` 验证 compute、三顶点×两实例的系统 ID、显式顶点缓冲属性，以及不依赖 vertex ID 生成坐标的固定点。蓝色/白色像素计数用于确认实际 render target 输出。

| 运行 | Compute | 系统 ID | 顶点属性 | 固定点 | 三角形 |
|---|---:|---|---|---|---:|
| Intel，L3=8 | 4096/4096 | 6 次调用均为 0 | ID=0，输出全白 | 0/4096，FAIL | 0/4096 |
| Intel，进程内把 L3 改 4 并重算 URB | 4096/4096 | 完整的 3 ID × 2 instance ID | 属性与 ID 正确 | 256/4096，PASS | 4096/4096 |
| AMD RX 6750 XT 对照 | 4096/4096 | 完整的 3 ID × 2 instance ID | 属性与 ID 正确 | 256/4096，PASS | 4096/4096 |

进一步隔离：只把 `hwInfo.L3Banks` 改为 4、保留既有 URB 模板时仍为 ID `0,0,0`、蓝色 `0/4096`；重新调用驱动的 URB 分配后为 ID `0,1,2`、蓝色 `4096/4096`。这说明修正点是**让驱动按 4-bank 能力重建 URB 分配状态**，不是单独修改一个能力展示值。

## 已安装应用图标数据集

`tools/diagnostics/gpu-tests/xe-app-icons.m` 扫描 `/Applications`、`/System/Applications`、`~/Applications` 和 Finder，找到 120 个应用，包括 119 个可直接读取声明图标资源的应用。每个资源分别以 32、128 和 512 像素进行独立 CARenderer 合成；另单独采集 NSWorkspace 来源，区分 IconServices 的输入结果与 Metal 合成器输出。

逐通道 BGRA 像素比较文件为 `build/icon-dataset-20261102-v2/summary.json`、`comparison.csv` 和 [可筛选图标图库](../../build/icon-dataset-20261102-v2/index.html)。原始 BGRA 与透明 PNG 分 GPU、来源和尺寸保存于该目录，未放进 Git 历史。

| 资源直接读取样本（共 360 项） | Intel 当前 L3=8 | Intel 进程内 L3=4 | AMD 参照 |
|---|---:|---:|---:|
| 有效参照 | 354 | 354 | 354 |
| 对照通过 | 171 | 354 | 354 |
| 对照失败 | 183 | 0 | 0 |
| 无有效参照 | 6 | 6 | 6 |
| 与 AMD 逐字节相同 | 177/360 | 356/360 | 360/360 |
| 最大 BGRA 通道误差 | 255/255 | 1/255 | 0/255 |

4-bank 两个有差异的图标样本是 OpenCore Configurator 和 ZCode 在 128、512 像素尺寸的抗锯齿边缘；单通道差值为 1，未发现超过阈值的像素。

NSWorkspace 来源在当前会话中为 175/360 空白输入，AMD 与 Intel 4-bank 数量相同。这组结果表明 IconServices 来源应作为单独启动后验收项，不能用资源直读的 CARenderer 对照替代。

完整的逐样本比较摘要和 CSV 另存于 `docs/evidence/20261102-gt1-urb/`，便于随提交保留；原始图像仍在 `build/`。

## 配置写入与回退

- 写入前以只读方式核对 `disk0s1`，其 `config.plist` 的 boot-args 与正在运行的 `nvram boot-args` 完全一致，含 `ngreen-l3=8`。用户重新挂载到 `/Volumes/NO NAME` 并授权写入后，再次核对分区身份和原配置。
- 候选配置位于 `build/gt1-l3-4-candidate-20261102/config.plist`，回退原文为同目录 `config.before.plist`。候选配置仅把 boot-args 中的 `ngreen-l3=8` 改为 `ngreen-l3=4`，长度不变；其他 plist 内容无语义变化。
- OpenCore 1.0.7 官方 `ocvalidate` 对候选配置返回 `No issues found`。
- 2026-11-02 11:42:39 已写入 `/Volumes/NO NAME/EFI/OC/config.plist`。EFI 内备份为 `config.plist.before-gt1-l3-4-20261102-114239`；写入前后仅有一个字节不同，回读及 `ocvalidate 1.0.7` 再验证通过。此次没有修改系统驱动、NVRAM 或内核扩展，也没有重启。

## 复现命令

```sh
clang -fobjc-arc -Wall -Wextra -Werror tools/diagnostics/gpu-tests/xe-vertex-urb.m \
  -framework Foundation -framework Metal -o /tmp/xe-vertex-urb
/tmp/xe-vertex-urb --gpu intel
/tmp/xe-vertex-urb --gpu intel --l3-banks 4
/tmp/xe-vertex-urb --gpu amd

clang -fobjc-arc -Wall -Wextra -Werror tools/diagnostics/gpu-tests/xe-app-icons.m \
  -framework AppKit -framework QuartzCore -framework Metal -o /tmp/xe-app-icons
/tmp/xe-app-icons --source resources --output /tmp/icons-intel4 --gpu intel --l3-banks 4
python3 tools/diagnostics/gpu-tests/compare-app-icons.py build/icon-dataset-20261102-v2
```

进程内对照在退出时消失；不会写驱动、EFI 或 NVRAM。新配置启动后，必须重新验证 Intel vertex/instance ID、顶点属性、窗口和应用图标、VideoToolbox 解码像素，以及新的 GPU restart/panic 记录，之后才能把 `ngreen-l3=4` 作为实际启动修复确认。
