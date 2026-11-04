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
            '#include "' + str(SOURCE.with_name("xe-vme-submit-capture.h")) + '"\n'
            r'''
#include <string.h>
#include <dlfcn.h>

typedef int (*XeVMEEmptyFlushCheck)(const XeVMEManagerSnapshot *,
    const unsigned char *, size_t);
typedef int (*XeVMEForwardDecision)(int, int, uint32_t);
typedef int (*XeVMEReadSharedValues)(const void *, uint32_t *, uint8_t *);

// 动态查找新判据，使其尚未实现时由行为断言失败而非编译失败。
static XeVMEEmptyFlushCheck empty_flush_checker(void) {
    return (XeVMEEmptyFlushCheck)dlsym(RTLD_DEFAULT,
                                      "xe_vme_is_empty_control_flush");
}

// 新策略暂以行为查找，缺失实现应由断言失败而不是编译失败指出。
static XeVMEForwardDecision forward_decision(void) {
    return (XeVMEForwardDecision)dlsym(RTLD_DEFAULT,
                                      "xe_vme_forward_nonempty_action");
}

// 按已核对偏移读取共享元数据，不把它解释为 GPU 虚拟地址。
static XeVMEReadSharedValues shared_values_reader(void) {
    return (XeVMEReadSharedValues)dlsym(RTLD_DEFAULT,
                                       "xe_vme_read_client_shared_values");
}

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

// 转发模式只捕获并放行首个非空 submit。
static int test_forward_nonempty_one_shot_policy(void) {
    XeVMEForwardDecision decide = forward_decision();
    if (!decide) return 125;
    if (decide(0, 1, 0) != XE_VME_ACTION_FORWARD) return 126;
    if (decide(1, 0, 0) != XE_VME_ACTION_FORWARD) return 127;
    if (decide(1, 1, 0) != 3) return 128;
    if (decide(1, 1, 1) != 4) return 129;
    if (decide(1, 1, 7) != 4) return 130;
    return 0;
}

// Test the exact client-shared fields emitted by the post-submit resource logger.
static int test_client_shared_fields_are_read_at_known_offsets(void) {
    XeVMEReadSharedValues read_values = shared_values_reader();
    if (!read_values) return 131;
    unsigned char shared[0x104] = {0};
    uint32_t expected_word = 0x12345678, actual_word = 0;
    uint8_t expected_flag = 0x5a, actual_flag = 0;
    memcpy(shared + 0x100, &expected_word, sizeof(expected_word));
    memcpy(shared + 0x1c, &expected_flag, sizeof(expected_flag));
    if (!read_values(shared, &actual_word, &actual_flag)) return 132;
    if (actual_word != expected_word || actual_flag != expected_flag) return 133;
    actual_word = 1;
    actual_flag = 1;
    if (read_values(NULL, &actual_word, &actual_flag)) return 134;
    if (actual_word != 0 || actual_flag != 0) return 135;
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

// 验证 VA 传入的命令区和 sideband 区按当前游标分别截取。
static int test_submit_buffer_ranges_are_captured(void) {
    static const unsigned char command[] = {0x10, 0x20, 0x30, 0x40, 0x50};
    static const unsigned char sideband[] = {0xa1, 0xb2, 0xc3, 0xd4};
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES + sizeof(command) + 2] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t command_start = (uintptr_t)command;
    uintptr_t command_current = command_start + sizeof(command);
    uintptr_t command_end = command_current + 1;
    uintptr_t sideband_start = (uintptr_t)sideband;
    uintptr_t sideband_current = sideband_start + 2;
    uintptr_t sideband_end = sideband_start + sizeof(sideband);
    memcpy(prefix + 0x10, &command_start, sizeof(command_start));
    memcpy(prefix + 0x18, &command_end, sizeof(command_end));
    memcpy(prefix + 0x20, &command_current, sizeof(command_current));
    memcpy(prefix + 0x60, &sideband_current, sizeof(sideband_current));
    memcpy(prefix + 0x68, &sideband_end, sizeof(sideband_end));
    memcpy(prefix + 0x80, &sideband_start, sizeof(sideband_start));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 9, 1, 0,
        &snapshot, error, sizeof(error)) != 0) return 85;
    if (snapshot.command_buffer.address != command_start ||
        snapshot.command_buffer.size != sizeof(command) ||
        snapshot.sideband_buffer.address != sideband_start ||
        snapshot.sideband_buffer.size != 2 ||
        snapshot.sideband_buffer.storage_offset !=
            XE_VME_MANAGER_PREFIX_BYTES + sizeof(command) ||
        snapshot.captured_bytes != XE_VME_MANAGER_PREFIX_BYTES +
            sizeof(command) + 2) return 86;
    if (!xe_vme_submit_has_data(&snapshot)) return 89;
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 87;
    if (memcmp(storage, prefix, sizeof(prefix)) != 0 ||
        memcmp(storage + snapshot.command_buffer.storage_offset, command,
               sizeof(command)) != 0 ||
        memcmp(storage + snapshot.sideband_buffer.storage_offset, sideband, 2) != 0)
        return 88;
    return 0;
}

// 验证初始化时只有 sideband 基址、没有游标的空提交可被识别。
static int test_empty_submit_buffer_ranges(void) {
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t sideband_start = 0x12340000;
    memcpy(prefix + 0x80, &sideband_start, sizeof(sideband_start));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 89;
    if (snapshot.cached_class_count != 0 || snapshot.command_buffer.size != 0 ||
        snapshot.sideband_buffer.size != 0 ||
        snapshot.captured_bytes != XE_VME_MANAGER_PREFIX_BYTES) return 90;
    if (xe_vme_submit_has_data(&snapshot)) return 95;
    return 0;
}

// 未识别的 sideband 即使没有 command 字节也必须触发捕获。
static int test_unclassified_sideband_is_data(void) {
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t sideband = 0x2000;
    uintptr_t sideband_current = sideband + 8;
    uintptr_t sideband_end = sideband + 64;
    memcpy(prefix + 0x60, &sideband_current, sizeof(sideband_current));
    memcpy(prefix + 0x68, &sideband_end, sizeof(sideband_end));
    memcpy(prefix + 0x80, &sideband, sizeof(sideband));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 2, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 96;
    if (snapshot.command_buffer.size != 0 || snapshot.sideband_buffer.size != 8 ||
        !xe_vme_submit_has_data(&snapshot)) return 97;
    return 0;
}

// 验证任何非零 submit 控制字段都不会被当作空初始化提交放行。
static int test_submit_control_fields_are_data(void) {
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    if (xe_vme_measure_submit_buffers(prefix, prefix, 1, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 119;
    if (!xe_vme_submit_has_data(&snapshot)) return 120;
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 1, 0,
        &snapshot, error, sizeof(error)) != 0) return 121;
    if (!xe_vme_submit_has_data(&snapshot)) return 122;
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 1,
        &snapshot, error, sizeof(error)) != 0) return 123;
    if (!xe_vme_submit_has_data(&snapshot)) return 124;
    return 0;
}

// 仅精确匹配的零尾标和单条 type-2 记录可视为空 flush。
static int test_empty_control_flush_signature(void) {
    _Alignas(8) unsigned char command_bytes[32] = {0};
    _Alignas(8) unsigned char sideband_bytes[64] = {0};
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES + 12 + 32] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t command = (uintptr_t)(command_bytes + 4);
    uintptr_t command_current = command + 12;
    uintptr_t command_end = command_current;
    uintptr_t sideband = (uintptr_t)sideband_bytes;
    uintptr_t sideband_current = sideband + 32;
    uintptr_t sideband_end = sideband + 64;
    uint32_t sideband_used_dwords = 4;
    uint64_t sideband_header = 0x20100;
    uint16_t record_type = 0x200;
    uint16_t record_flags = 0;
    uint32_t command_dwords = 3;
    XeVMEEmptyFlushCheck is_empty_flush = empty_flush_checker();
    memcpy(sideband_bytes + 8, &sideband_used_dwords, sizeof(sideband_used_dwords));
    memcpy(sideband_bytes + 16, &sideband_header, sizeof(sideband_header));
    memcpy(sideband_bytes + 24, &record_type, sizeof(record_type));
    memcpy(sideband_bytes + 26, &record_flags, sizeof(record_flags));
    memcpy(sideband_bytes + 28, &command_dwords, sizeof(command_dwords));

    uintptr_t aligned_command = (uintptr_t)command_bytes;
    uintptr_t aligned_current = aligned_command + 8;
    uintptr_t aligned_end = aligned_current;
    uint32_t aligned_command_dwords = 2;
    memcpy(sideband_bytes + 28, &aligned_command_dwords,
           sizeof(aligned_command_dwords));
    memcpy(prefix + 0x10, &aligned_command, sizeof(aligned_command));
    memcpy(prefix + 0x18, &aligned_end, sizeof(aligned_end));
    memcpy(prefix + 0x20, &aligned_current, sizeof(aligned_current));
    memcpy(prefix + 0x60, &sideband_current, sizeof(sideband_current));
    memcpy(prefix + 0x68, &sideband_end, sizeof(sideband_end));
    memcpy(prefix + 0x80, &sideband, sizeof(sideband));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 115;
    if (snapshot.command_buffer.size != 8) return 116;
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 117;
    if (!is_empty_flush || !is_empty_flush(&snapshot, storage,
        (size_t)snapshot.captured_bytes)) return 118;

    memcpy(sideband_bytes + 28, &command_dwords, sizeof(command_dwords));
    memcpy(prefix + 0x10, &command, sizeof(command));
    memcpy(prefix + 0x18, &command_end, sizeof(command_end));
    memcpy(prefix + 0x20, &command_current, sizeof(command_current));
    memcpy(prefix + 0x60, &sideband_current, sizeof(sideband_current));
    memcpy(prefix + 0x68, &sideband_end, sizeof(sideband_end));
    memcpy(prefix + 0x80, &sideband, sizeof(sideband));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 102;
    if (!xe_vme_submit_has_data(&snapshot)) return 103;
    if (!is_empty_flush) return 104;
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 105;
    if (!is_empty_flush(&snapshot, storage,
        (size_t)snapshot.captured_bytes)) return 106;

    uint32_t classes = 1;
    memcpy(prefix + 0x58, &classes, sizeof(classes));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 2, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 107;
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 108;
    if (is_empty_flush(&snapshot, storage,
        (size_t)snapshot.captured_bytes)) return 109;

    classes = 0;
    memcpy(prefix + 0x58, &classes, sizeof(classes));
    command_bytes[4] = 1;
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 110;
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 111;
    if (is_empty_flush(&snapshot, storage,
        (size_t)snapshot.captured_bytes)) return 112;

    command_bytes[4] = 0;
    record_type = 0x300;
    memcpy(sideband_bytes + 24, &record_type, sizeof(record_type));
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy, storage,
        sizeof(storage), error, sizeof(error)) != 0) return 113;
    if (is_empty_flush(&snapshot, storage,
        (size_t)snapshot.captured_bytes)) return 114;
    return 0;
}

// 验证反向游标和超过总预算的命令范围都会被拒绝。
static int test_invalid_submit_buffer_ranges_rejected(void) {
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t base = 0x1000;
    uintptr_t current = base + 8;
    uintptr_t end = base + 4;
    memcpy(prefix + 0x10, &base, sizeof(base));
    memcpy(prefix + 0x18, &end, sizeof(end));
    memcpy(prefix + 0x20, &current, sizeof(current));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) == 0) return 91;

    current = base + XE_VME_CAPTURE_MAX_TOTAL_BYTES + 1;
    end = current;
    memcpy(prefix + 0x18, &end, sizeof(end));
    memcpy(prefix + 0x20, &current, sizeof(current));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) == 0) return 92;
    return 0;
}

// 验证 command 或 sideband 的部分复制不会被当作完整快照接受。
static int test_submit_buffer_partial_copy_rejected(void) {
    static const unsigned char command[] = {1, 2, 3, 4};
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES] = {0};
    unsigned char storage[XE_VME_MANAGER_PREFIX_BYTES + sizeof(command)] = {0};
    XeVMEManagerSnapshot snapshot = {0};
    char error[128] = {0};
    uintptr_t base = (uintptr_t)command;
    uintptr_t current = base + sizeof(command);
    uintptr_t end = current;
    memcpy(prefix + 0x10, &base, sizeof(base));
    memcpy(prefix + 0x18, &end, sizeof(end));
    memcpy(prefix + 0x20, &current, sizeof(current));
    if (xe_vme_measure_submit_buffers(prefix, prefix, 0, 0, 0,
        &snapshot, error, sizeof(error)) != 0) return 93;
    partial_copy = 1;
    int result = xe_vme_copy_submit_buffers(prefix, &snapshot, mock_copy,
        storage, sizeof(storage), error, sizeof(error));
    partial_copy = 0;
    if (result == 0) return 94;
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
    result = test_forward_nonempty_one_shot_policy();
    if (result) return result;
    result = test_client_shared_fields_are_read_at_known_offsets();
    if (result) return result;
    result = test_target_callsite_guard();
    if (result) return result;
    result = test_manager_prefix_snapshot();
    if (result) return result;
    result = test_zero_cached_count_keeps_opaque_pointer();
    if (result) return result;
    result = test_cached_class_count_is_not_range_limited();
    if (result) return result;
    result = test_submit_buffer_ranges_are_captured();
    if (result) return result;
    result = test_empty_submit_buffer_ranges();
    if (result) return result;
    result = test_unclassified_sideband_is_data();
    if (result) return result;
    result = test_submit_control_fields_are_data();
    if (result) return result;
    result = test_empty_control_flush_signature();
    if (result) return result;
    result = test_invalid_submit_buffer_ranges_rejected();
    if (result) return result;
    result = test_submit_buffer_partial_copy_rejected();
    if (result) return result;
    return test_unknown_manager_and_callers_rejected();
}
''',
            encoding="utf-8",
        )
        executable = root / "harness"
        result = subprocess.run(
            ["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
             "-DXE_VME_CAPTURE_TEST", "-Wl,-export_dynamic", str(source),
             str(SOURCE), "-o", str(executable)],
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
            environment.pop("XE_VME_CAPTURE_NONEMPTY_SUBMIT", None)
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

            forward_missing_directory = environment.copy()
            forward_missing_directory["XE_VME_FORWARD_FIRST_NONEMPTY"] = "1"
            result = subprocess.run([str(app)], env=forward_missing_directory,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("capture directory is required", result.stderr)

            conflicting_modes = environment.copy()
            conflicting_modes["XE_VME_CAPTURE_DIR"] = str(root)
            conflicting_modes["XE_VME_STOP_BEFORE_SUBMIT"] = "1"
            conflicting_modes["XE_VME_CAPTURE_NONEMPTY_SUBMIT"] = "1"
            result = subprocess.run([str(app)], env=conflicting_modes,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("exactly one capture mode", result.stderr)

            conflicting_forward_mode = environment.copy()
            conflicting_forward_mode["XE_VME_CAPTURE_DIR"] = str(root)
            conflicting_forward_mode["XE_VME_STOP_BEFORE_SUBMIT"] = "1"
            conflicting_forward_mode["XE_VME_FORWARD_FIRST_NONEMPTY"] = "1"
            result = subprocess.run([str(app)], env=conflicting_forward_mode,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("exactly one capture mode", result.stderr)

            missing_mode = environment.copy()
            missing_mode["XE_VME_CAPTURE_DIR"] = str(root)
            result = subprocess.run([str(app)], env=missing_mode,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 78, result.stderr)
            self.assertIn("exactly one capture mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
