# VA 提交前 manager 前缀取证（2026-11-03）

## 当前结论

已在受控 `Usage=1` 单帧运行中命中 VA 的 `IOAccelVideoContextSubmitDataBuffers` 调用点，并在原始提交 API 执行前保存 0x88 字节的 VA manager 前缀。捕获分支按设计以退出码 77 结束进程；原始 GPU 提交没有执行。

该结果验证了进程内 interpose 和调用点守卫，但没有捕获本帧 data-buffer class、GPU 命令 payload、sideband 或实际已用长度，不能作为普通 VME/PAK 编码成功的证据。没有修改系统 VA、EFI、NootedGreen、NootRX 或安装目录，无需重启。

## 静态依据

检查对象为 `build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver`，使用 Ghidra 12.1.4 函数分析和 `otool` 反汇编核对 `0xA893` 至 `0xA9DB`：

- `0xA8A1` 将函数首参数（manager）保存到 `R14`；`0xA8F6` 是对 `_IOAccelVideoContextSubmitDataBuffers` 的直接调用，返回地址为 `0xA8FB`，调用指令字节为 `e8 15 38 23 00`。
- 提交调用前，函数读取 `manager+0x58`。若非零，会遍历先前缓存的 class 记录、更新 used-bytes 状态并清理记录。
- 原始提交返回成功且错误标志为空后，函数才在 `0xA918` 调用 `_IOAccelVideoContextGetDataBufferClassCount`，并把新计数写入 `manager+0x58`；随后才逐项获取 resource、data bytes、capacity 和 client-shared 信息。
- 同一成功路径随后读取 `manager+0x80` 指针并访问其指向的数据。静态代码只证明该指针在提交成功后被使用；该状态的准确类型和它与 GPU 命令 payload 的关系仍未知。

因此，提交前快照中的 `manager+0x58=0` 只说明入口处没有已缓存的 class 记录，不能推出本次提交没有 class 或命令数据。此前在同一边界调用 getter 得到空资源，与“新 class 列表在提交成功后才读取并保存”的调用顺序一致。

## 捕获范围

实现位于 `tools/diagnostics/gpu-tests/xe-vme-submit-capture.c` 和同目录头文件。interpose 只在目标 VA image 的 `__TEXT` 段、`0xA8F6` 调用字节和返回地址均与已分析位置匹配，并且显式设置 `XE_VME_CAPTURE_DIR` 与 `XE_VME_STOP_BEFORE_SUBMIT=1` 时捕获。

捕获器从调用者保留的 `R14` 读取 manager 地址，使用 `mach_vm_read_overwrite` 复制固定 0x88 字节。`manager+0x80` 的内容只作为快照字节保存，不在捕获路径中解引用。`+0x58` 计数仅按原值记录；捕获器不根据该字段遍历内存或限制其取值。内存复制不完整或输入范围无效时拒绝采集。输出文件使用 `O_EXCL`，避免覆盖已有结果。

## 运行时证据

- 本次 `Usage=1` 单帧探针命中目标 VA 调用点，捕获文件为 `build/validation-encode-and-kext-icons-20261102/vme-submit-prefix-usage1-20261103/manager-15298.bin`，元数据为同目录的 `submit-events-15298.jsonl`。
- 元数据记录 `request=0`、`flags=0`、`fault=0`、`cached_class_count=0`、`captured_bytes=136`。原始前缀的 `+0x80` 位置包含非空指针值；该值未被解引用或解释。
- 受控停止发生在原始提交 API 之前，所以本次运行没有实际提交 GPU 编码命令。`Usage=1` 仅作为当前证据中的 VDENC 路径对照，不代表 `Usage=0` 普通 VME/PAK 路径已验证。
- CPU 夹具覆盖部分复制拒绝、零缓存数、超范围原始计数保留、原始前缀不变和不透明指针原样保留；合成 dylib 夹具覆盖非目标调用转发。
- 2026-11-03 重跑 `tools/diagnostics/gpu-tests/test-*.py` 的 11 个脚本，共 50 项测试全部通过；捕获器与 interpose 使用 `-Wall -Wextra -Werror` 编译。

## 后续证据缺口

当前入口位于原始提交之前，而新 class/resource 信息在该提交成功后才由 VA 读取。要获取 `Usage=0` 的实际 VME/PAK 资源及 payload，需要另行确定能够观测提交结果的边界；在此之前，不从 `manager+0x80` 推断命令格式，也不制作编码补丁。
