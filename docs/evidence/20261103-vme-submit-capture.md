# 普通 VME/PAK 提交前捕获器（2026-11-03）

## 当前结论

已为 A788 / Raptor Lake-S/HX 的 VideoToolbox `Usage=1`（当前证据对应 VDENC）路径准备独立、进程内的提交前捕获器。捕获器仅在已验证的 VA 调用点触发，并要求显式设置 `XE_VME_STOP_BEFORE_SUBMIT=1`；完整快照写入后以退出码 77 结束，目标分支不调用原提交 API。

这只是适配前的证据工具，当前没有取得运行时提交快照，也没有生成可用于实际 GPU 编码的普通 VME/PAK 修补。系统 VA、EFI、NootedGreen、NootRX 和安装目录没有修改，无需重启。

## 静态依据

- 当前独立 VA 候选的 `0xA8F6` 是对 `_IOAccelVideoContextSubmitDataBuffers` 的直接调用；x86_64 call 指令为 `e8 15 38 23 00`，返回地址相对 VA image base 为 `0xA8FB`。
- `0xA893` 在提交成功后读取 data-buffer class 数量、逐项取得资源、读取数据地址与容量，并随后清理 VA 侧状态。资源必须在提交前尝试捕获。
- 普通 VME/PAK 与已验证 VDENC 的帧布局不同。已有 Usage=0 证据尚未证明任何普通编码修补可用，因此本工具不映射 capability，也不重写命令数据。

## 捕获边界

实现位于 `tools/diagnostics/gpu-tests/xe-vme-submit-capture.c` 和同目录的头文件。运行时仅在当前进程中 interpose `IOAccelVideoContextSubmitDataBuffers`，并检查：

- VA image 的 `__TEXT` 段从 image base 0 开始，段范围包含目标调用点，调用点字节完全匹配。
- 返回地址正好对应 `0xA8FB`，调用者可通过 `dladdr` 识别为 Apple Intel VA。
- `XE_VME_CAPTURE_DIR` 和 `XE_VME_STOP_BEFORE_SUBMIT=1` 均显式设置。
- data-buffer class 数量必须为 1 至 32，单个资源容量不超过 16 MiB，总复制量不超过 64 MiB；当前进程读取使用 `mach_vm_read_overwrite`，部分复制和越界一律拒绝。

输出文件使用 `O_EXCL` 避免覆盖既有采集。二进制文件保存每个 class 的完整资源容量；元数据记录 request、flags、fault、资源地址、容量和存储偏移。`IOAccelResourceGetDataSize` 只能确认为容量，实际已用长度仍未知。Sideband 也未采集，不能把当前输出解释为完整提交包。

捕获函数只使用由 VA 实际导入的 getter，随后目标分支直接退出。非目标调用保持原始 API 转发。合成 dylib 测试验证了本 interpose 中直接调用原导出的 dyld 转发方式；单测也验证了调用点策略、容量边界、源缓冲区不变和完整复制要求。

## 运行时验证状态

- `test-vme-submit-capture.py` 的 CPU 夹具及合成 interpose 测试通过。全套 11 个 `tools/diagnostics/gpu-tests/test-*.py` 文件、50 项测试均通过。
- 捕获库与既有 VA 诊断源码已合并编译成 build 目录中的 `xe-va-vme-submit-capture-lab.dylib`；严格编译通过，`__interpose` 段存在。
- 使用最终合并库运行 `--describe-options` 只解析配置，没有打开 VA 或提交 GPU 命令；返回码为 0，确认插入库加载且 `Usage=1` 参数有效。
- 真实编码探针曾在插入库初始化后以退出码 2 结束，stderr 只包含 `XE-VME capture armed`，没有 `target VA submit callsite verified`、VA 调度日志或快照文件。该运行没有验证拦截器，也没有到达目标 VA 提交点；现有输出不能唯一说明它在 VideoToolbox factory 校验还是 Intel Metal 设备选择处提前退出。
- 一次较早的试跑曾在插入库构造阶段过早解析 `RTLD_NEXT` 并以 79 退出。当前插入库不在构造阶段解析原导出，目标分支使用 dyld 的直接原导出转发；合成 dylib 已对该转发进行了 CPU 验证。

## 下一步

先在正常 macOS 启动环境用 `--describe-options` 以外的受控 `Usage=1` 单帧测试验证 VA 版本与提交钩子。以下命令使用 build 目录的候选 VA 和合并诊断库；输出位置按本次日期固定为新目录：

```sh
mkdir -p build/validation-encode-and-kext-icons-20261102/vme-submit-usage1-20261103
DYLD_LIBRARY_PATH="$PWD/build/validation-encode-and-kext-icons-20261102/va-gen12-native" \
DYLD_INSERT_LIBRARIES="$PWD/build/validation-encode-and-kext-icons-20261102/xe-va-vme-submit-capture-lab.dylib" \
XE_VME_CAPTURE_DIR="$PWD/build/validation-encode-and-kext-icons-20261102/vme-submit-usage1-20261103" \
XE_VME_STOP_BEFORE_SUBMIT=1 \
"$PWD/build/validation-encode-and-kext-icons-20261102/xe-h264-encode-modes-lab" \
  "$PWD/build/validation-encode-and-kext-icons-20261102/vme-submit-usage1-20261103/run" \
  1280 720 1 --usage 1
```

预期退出码为 77，stderr 中出现 `XE-VME target VA submit callsite verified`，并且 `submit-events-<pid>.jsonl` 中出现 `submit-before-original`；目录中还应出现 `submit-<pid>-class-<n>.bin`。如果退出码为 2 且没有 VA 验证行，说明测试程序在加载目标 VA 前退出，不能据此判断捕获器或驱动状态。

该 `Usage=1` 结果只验证提交拦截与提供 VDENC 对照快照。确认它命中后，再讨论如何在保持提交前保护的条件下收集 Usage=0 首个 FrameStatistics/VME-PAK 提交；不得把 Usage=1 结果当作 Usage=0 的证据。
