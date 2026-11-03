#!/usr/bin/env python3
"""验证普通 VME/PAK 提交捕获核心只读复制并在边界处 fail-closed。"""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name("xe-vme-submit-capture.c")


class VmeSubmitCaptureTests(unittest.TestCase):
    """用 CPU 内存夹具验证提交前捕获的结构边界。"""

    def build_harness(self, root: Path) -> Path:
        """生成并编译一个只调用捕获核心的 CPU 夹具。"""
        source = root / "harness.c"
        source.write_text(
            '#define XE_VME_CAPTURE_TEST 1\n'
            '#include "' + str(SOURCE) + '"\n'
            r'''
#include <string.h>

static unsigned char class0[] = {0x01, 0x02, 0x03, 0x04};
static unsigned char class1[] = {0xa0, 0xb0, 0xc0, 0xd0, 0xe0, 0xf0};
static int partial_copy;
static int class_count = 2;
static int null_resource;
static int oversized_resource;
static int overflow_span;
static int zero_resource;

// 返回固定的 data-buffer class 数量，模拟 IOAccelerator getter。
static int mock_class_count(void *context) {
    (void)context;
    return class_count;
}

// 返回夹具中的资源句柄，句柄只用于选择对应的 CPU 缓冲区。
static void *mock_resource(void *context, uint32_t index) {
    (void)context;
    if (null_resource && index == 1) return NULL;
    return index == 0 ? class0 : (index == 1 ? class1 : NULL);
}

// 返回资源对应的 CPU 数据地址。
static const void *mock_data_bytes(void *resource) {
    if (overflow_span && resource == class1)
        return (const void *)(uintptr_t)(UINTPTR_MAX - 2);
    return resource == class0 ? class0 : (resource == class1 ? class1 : NULL);
}

// 返回资源的完整容量，捕获器必须按容量读取而不是猜测帧格式。
static uint64_t mock_data_size(void *resource) {
    if (zero_resource && resource == class1) return 0;
    if (oversized_resource && resource == class1) return XE_VME_MAX_CLASS_BYTES + 1ULL;
    return resource == class0 ? sizeof(class0) : (resource == class1 ? sizeof(class1) : 0);
}

// 模拟无故障复制；开启 partial_copy 时故意少复制一个字节。
static size_t mock_copy(void *destination, const void *source, size_t size) {
    size_t copied = partial_copy && size ? size - 1 : size;
    memcpy(destination, source, copied);
    return copied;
}

// 构造一个带两个 class buffer 的 provider。
static XeVMEProvider mock_provider(void) {
    XeVMEProvider provider = {
        .get_class_count = mock_class_count,
        .get_resource = mock_resource,
        .get_data_bytes = mock_data_bytes,
        .get_data_size = mock_data_size,
        .copy_bytes = mock_copy,
    };
    return provider;
}

// 验证正常捕获保留 class 顺序、容量和每个缓冲区的字节。
static int test_success(void) {
    unsigned char storage[32] = {0};
    XeVMESubmitSnapshot snapshot = {0};
    char error[128] = {0};
    XeVMEProvider provider = mock_provider();
    if (xe_vme_capture_snapshot(NULL, 0x1234, 0x5678, 0x9abc,
                                &provider, &snapshot, storage, sizeof(storage),
                                error, sizeof(error)) != 0) return 10;
    if (snapshot.class_count != 2 || snapshot.captured_classes != 2) return 11;
    if (snapshot.classes[0].size != sizeof(class0) ||
        snapshot.classes[1].size != sizeof(class1)) return 12;
    if (snapshot.classes[0].storage_offset != 0 ||
        snapshot.classes[1].storage_offset != sizeof(class0)) return 13;
    if (memcmp(storage, class0, sizeof(class0)) != 0 ||
        memcmp(storage + sizeof(class0), class1, sizeof(class1)) != 0) return 14;
    if (memcmp(class0, (unsigned char[]){0x01, 0x02, 0x03, 0x04}, sizeof(class0)) != 0 ||
        memcmp(class1, (unsigned char[]){0xa0, 0xb0, 0xc0, 0xd0, 0xe0, 0xf0}, sizeof(class1)) != 0) return 16;
    if (snapshot.request != 0x1234 || snapshot.flags != 0x5678 ||
        snapshot.fault != 0x9abc) return 15;
    return 0;
}

// 验证部分复制被拒绝，避免保存不完整的 GPU 提交证据。
static int test_partial_copy_rejected(void) {
    unsigned char storage[32] = {0};
    XeVMESubmitSnapshot snapshot = {0};
    char error[128] = {0};
    XeVMEProvider provider = mock_provider();
    partial_copy = 1;
    int result = xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider,
                                         &snapshot, storage, sizeof(storage),
                                         error, sizeof(error));
    partial_copy = 0;
    return result == 0 ? 20 : 0;
}

// 验证 class 数量和存储容量边界均被拒绝。
static int test_bounds_rejected(void) {
    unsigned char storage[4] = {0};
    XeVMESubmitSnapshot snapshot = {0};
    char error[128] = {0};
    XeVMEProvider provider = mock_provider();
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 30;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage) - 1, error,
                                sizeof(error)) == 0) return 31;
    return 0;
}

// 验证异常资源、过多 class 和超大资源均在复制前拒绝。
static int test_invalid_metadata_rejected(void) {
    unsigned char storage[32] = {0};
    XeVMESubmitSnapshot snapshot = {0};
    char error[128] = {0};
    XeVMEProvider provider = mock_provider();
    null_resource = 1;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 40;
    null_resource = 0;
    class_count = 0;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 44;
    class_count = 2;
    class_count = XE_VME_MAX_CLASSES + 1;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 41;
    class_count = 2;
    zero_resource = 1;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 45;
    zero_resource = 0;
    oversized_resource = 1;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 42;
    oversized_resource = 0;
    overflow_span = 1;
    if (xe_vme_capture_snapshot(NULL, 0, 0, 0, &provider, &snapshot,
                                storage, sizeof(storage), error, sizeof(error)) == 0) return 43;
    overflow_span = 0;
    return 0;
}

// 验证只有已识别 VA 调用点且显式截停时才捕获，其他不确定状态拒绝。
static int test_submit_intercept_policy(void) {
    if (xe_vme_submit_action(1, 1, 1, 1, 1) != XE_VME_ACTION_CAPTURE) return 50;
    if (xe_vme_submit_action(1, 1, 1, 1, 0) != XE_VME_ACTION_REJECT) return 51;
    if (xe_vme_submit_action(1, 1, 1, 0, 1) != XE_VME_ACTION_REJECT) return 52;
    if (xe_vme_submit_action(1, 0, 1, 1, 1) != XE_VME_ACTION_REJECT) return 53;
    if (xe_vme_submit_action(1, 1, 0, 0, 1) != XE_VME_ACTION_FORWARD) return 54;
    if (xe_vme_submit_action(0, 0, 0, 0, 0) != XE_VME_ACTION_FORWARD) return 55;
    return 0;
}

// 验证目标 VA 提交调用的固定字节必须完整匹配且位于足够长的 image 中。
static int test_target_callsite_guard(void) {
    unsigned char image[XE_VME_SUBMIT_CALLSITE_OFFSET + 5] = {0};
    const unsigned char expected[] = {0xe8, 0x15, 0x38, 0x23, 0x00};
    memcpy(image + XE_VME_SUBMIT_CALLSITE_OFFSET, expected, sizeof(expected));
    if (!xe_vme_callsite_matches(image, sizeof(image))) return 60;
    image[XE_VME_SUBMIT_CALLSITE_OFFSET] = 0x90;
    if (xe_vme_callsite_matches(image, sizeof(image))) return 61;
    if (xe_vme_callsite_matches(image, sizeof(image) - 1)) return 62;
    return 0;
}

// 运行所有核心断言并返回第一个失败编号。
int main(void) {
    int result = test_success();
    if (result) return result;
    result = test_partial_copy_rejected();
    if (result) return result;
    result = test_bounds_rejected();
    if (result) return result;
    result = test_invalid_metadata_rejected();
    if (result) return result;
    result = test_submit_intercept_policy();
    if (result) return result;
    return test_target_callsite_guard();
}
''',
            encoding="utf-8",
        )
        executable = root / "harness"
        result = subprocess.run(
            ["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return executable

    def test_cpu_capture_core(self):
        """验证数据只读复制、元数据边界和完整复制要求。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_harness(Path(temporary))
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_interposer_forwards_original_export(self):
        """通过合成 API 验证生产 interposer 直接转发原导出。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            framework_dir = root / "IOAccelerator.framework" / "Versions" / "A"
            framework_dir.mkdir(parents=True)
            api_source = root / "fake-ioaccel.c"
            api_source.write_text(r'''
#include <stdint.h>

// 返回固定状态，确认 interposer 转发到了下一层导出。
int IOAccelVideoContextSubmitDataBuffers(void *context, uint32_t request,
                                         uint32_t *flags, uint32_t *fault) {
    (void)flags;
    (void)fault;
    return context == (void *)0x1234 && request == 41 ? 42 : 99;
}
''')
            api = framework_dir / "IOAccelerator"
            built = subprocess.run(
                ["clang", "-dynamiclib", "-Wall", "-Wextra", "-Werror",
                 str(api_source), "-o", str(api)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)

            app_source = root / "app.c"
            app_source.write_text(r'''
#include <stdint.h>

// 调用被 interpose 的 API，成功状态只能来自 fake-ioaccel.dylib。
extern int IOAccelVideoContextSubmitDataBuffers(void *, uint32_t,
                                                 uint32_t *, uint32_t *);
int main(void) {
    uint32_t flags = 0;
    uint32_t fault = 0;
    return IOAccelVideoContextSubmitDataBuffers((void *)0x1234, 41,
                                                &flags, &fault) == 42 ? 0 : 98;
}
''')
            app = root / "app"
            built = subprocess.run(
                ["clang", str(app_source), str(api), "-Wl,-rpath," + str(root),
                 "-o", str(app)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)

            interposer = root / "xe-vme-submit-capture.dylib"
            built = subprocess.run(
                ["clang", "-dynamiclib", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-undefined", "dynamic_lookup", str(SOURCE), "-o", str(interposer)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            environment = os.environ.copy()
            environment.pop("XE_VME_CAPTURE_DIR", None)
            environment.pop("XE_VME_STOP_BEFORE_SUBMIT", None)
            environment["DYLD_INSERT_LIBRARIES"] = str(interposer)
            environment["DYLD_LIBRARY_PATH"] = str(root)
            result = subprocess.run([str(app)], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            missing_directory = environment.copy()
            missing_directory.pop("XE_VME_CAPTURE_DIR", None)
            missing_directory["XE_VME_STOP_BEFORE_SUBMIT"] = "1"
            result = subprocess.run([str(app)], env=missing_directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("capture directory is required",
                          result.stderr)


if __name__ == "__main__":
    unittest.main()
