#!/usr/bin/env python3
"""验证 VA helper 前缀快照、调用点守卫与 dyld interpose 转发。"""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name("xe-vme-submit-capture.c")


class VmeSubmitCaptureTests(unittest.TestCase):
    """用 CPU 夹具验证 helper 前缀捕获与提交拦截策略。"""

    def build_harness(self, root: Path) -> Path:
        """生成并编译一个只调用捕获核心的 CPU 夹具。"""
        source = root / "harness.c"
        source.write_text(
            '#define XE_VME_CAPTURE_TEST 1\n'
            '#include "' + str(SOURCE) + '"\n'
            r'''
#include <string.h>

static int partial_copy;

// 模拟只读复制，开启故障开关时故意少复制一个字节。
static size_t mock_copy(void *destination, const void *source, size_t size) {
    size_t copied = partial_copy && size ? size - 1 : size;
    memcpy(destination, source, copied);
    return copied;
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

// 验证提交时只复制固定 helper 前缀，并保留请求字段和缓存的 class 数量。
static int test_manager_prefix_snapshot(void) {
    unsigned char manager[XE_VME_MANAGER_PREFIX_BYTES];
    unsigned char original[XE_VME_MANAGER_PREFIX_BYTES];
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    memset(manager, 0x5a, sizeof(manager));
    uint32_t classes = 1;
    memcpy(manager + 0x58, &classes, sizeof(classes));
    memcpy(original, manager, sizeof(manager));
    if (xe_vme_capture_manager_snapshot(manager, 0x1234, 0x5678, 0x9abc,
                                        mock_copy, &snapshot, storage,
                                        sizeof(storage), error, sizeof(error)) != 0) return 70;
    if (snapshot.manager != (uintptr_t)manager ||
        snapshot.captured_bytes != sizeof(manager) || snapshot.cached_class_count != 1)
        return 71;
    if (snapshot.request != 0x1234 || snapshot.flags != 0x5678 ||
        snapshot.fault != 0x9abc) return 72;
    if (memcmp(storage, manager, sizeof(manager)) != 0 ||
        memcmp(original, manager, sizeof(manager)) != 0) return 73;
    partial_copy = 1;
    int result = xe_vme_capture_manager_snapshot(manager, 0, 0, 0, mock_copy,
        &snapshot, storage, sizeof(storage), error, sizeof(error));
    partial_copy = 0;
    if (result == 0) return 74;
    return 0;
}

// 验证零缓存 class 数量有效，且 +0x80 只作为原始前缀字节复制。
static int test_zero_cached_count_keeps_opaque_pointer(void) {
    unsigned char manager[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t opaque_pointer = 1;
    memcpy(manager + 0x80, &opaque_pointer, sizeof(opaque_pointer));
    if (xe_vme_capture_manager_snapshot(manager, 0, 0, 0, mock_copy,
        &snapshot, storage, sizeof(storage), error, sizeof(error)) != 0) return 76;
    if (snapshot.cached_class_count != 0 ||
        snapshot.captured_bytes != XE_VME_MANAGER_PREFIX_BYTES) return 77;
    if (memcmp(storage, manager, sizeof(manager)) != 0) return 78;
    return 0;
}

// 缓存计数只按原始字段记录，静态证据没有给出可用于拒绝的上限。
static int test_cached_class_count_is_not_range_limited(void) {
    unsigned char manager[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uint32_t classes = UINT32_MAX;
    memcpy(manager + 0x58, &classes, sizeof(classes));
    if (xe_vme_capture_manager_snapshot(manager, 0, 0, 0, mock_copy,
        &snapshot, storage, sizeof(storage), error, sizeof(error)) != 0) return 79;
    if (snapshot.cached_class_count != UINT32_MAX) return 80;
    return 0;
}

// 验证 helper、目标和调用点策略均未知时拒绝，并保留非目标转发。
static int test_unknown_manager_and_callers_rejected(void) {
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    if (xe_vme_capture_manager_snapshot(NULL, 0, 0, 0, mock_copy,
        &snapshot, storage, sizeof(storage), error, sizeof(error)) == 0) return 80;
    if (xe_vme_capture_manager_snapshot(storage, 0, 0, 0, mock_copy,
        &snapshot, NULL, sizeof(storage), error, sizeof(error)) == 0) return 81;
    if (xe_vme_capture_manager_snapshot(storage, 0, 0, 0, mock_copy,
        &snapshot, storage, sizeof(storage) - 1, error, sizeof(error)) == 0) return 82;
    if (xe_vme_submit_action(1, 1, 1, 0, 1) != XE_VME_ACTION_REJECT) return 83;
    if (xe_vme_submit_action(1, 1, 0, 0, 1) != XE_VME_ACTION_FORWARD) return 84;
    return 0;
}

// 运行所有核心断言并返回第一个失败编号。
int main(void) {
    int result = test_submit_intercept_policy();
    if (result) return result;
    result = test_target_callsite_guard();
    if (result) return result;
    result = test_manager_prefix_snapshot();
    if (result) return result;
    result = test_zero_cached_count_keeps_opaque_pointer();
    if (result) return result;
    result = test_cached_class_count_is_not_range_limited();
    if (result) return result;
    return test_unknown_manager_and_callers_rejected();
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
