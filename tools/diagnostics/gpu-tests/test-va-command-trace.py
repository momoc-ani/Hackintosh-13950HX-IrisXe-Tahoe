#!/usr/bin/env python3
"""用普通内存验证 VA 帧参数采集在原调度入口之前截停。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("xe-va-command-trace.c")


class CommandTraceTests(unittest.TestCase):
    """运行真实采集函数和 CPU 调度桩，不创建或连接 GPU。"""

    def build_harness(self, directory):
        """编译含双节点及不可解引用字段值的夹具，检查采集器只读取字段本身。"""
        self.assertTrue(SOURCE.exists(), "尚未实现 VA 原调度前截停采集")
        source = directory / "harness.c"
        source.write_text('#include "' + str(SOURCE) + '"\n' + r'''
static int original_calls;

// 返回一个固定状态，供默认模式确认原入口的调用次数及请求原样传递。
static int original(void *accelerator, uint32_t *request, void *output) {
    (void)accelerator; (void)request; (void)output;
    original_calls++;
    return 17;
}

// 使用已证实的命令字段位置构造请求；帧字段值仅可被记录，不允许解引用。
int main(void) {
    unsigned char header[16] = {0}, first[0x38] = {0}, second[0x38] = {0};
    unsigned char frame[0x4a8] = {0}, frame_before[sizeof(frame)];
    unsigned char *commands[] = {first};
    uint32_t count = 1, kind = 0x20, other_kind = 2;
    uint64_t cap = 0x400, other_cap = 0x800, sentinel = 0x1020304050607080ULL;
    void *array = commands, *parameters = frame, *next = second;
    memcpy(header, &count, 4); memcpy(header + 8, &array, 8);
    memcpy(first, &kind, 4); memcpy(first + 8, &cap, 8);
    memcpy(first + 0x20, &parameters, 8); memcpy(first + 0x30, &next, 8);
    memcpy(second, &other_kind, 4); memcpy(second + 8, &other_cap, 8);
    memcpy(frame + 0x8, &sentinel, 8); memcpy(frame + 0x440, &sentinel, 8);
    memcpy(frame_before, frame, sizeof(frame));
    int result = xe_va_trace_schedule(NULL, (uint32_t *)header, NULL, original);
    if (result != 17 || original_calls != 1 || memcmp(frame, frame_before, sizeof(frame))) return 91;
    puts("original-called-once status=17");
    return 0;
}
''')
        executable = directory / "harness"
        built = subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                str(source), "-o", str(executable)], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        return executable

    def test_stop_records_frame_fields_without_calling_original(self):
        """开关为 1 时输出两个节点及帧字段，以 77 退出且不调用原调度入口。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_harness(Path(temporary))
            environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1"}
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertNotIn("original-called", result.stdout)
            events = [json.loads(line.removeprefix("VA-COMMAND "))
                      for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
            nodes = [event for event in events if event["event"] == "node"]
            self.assertEqual([node["kind"] for node in nodes], [0x20, 2])
            fields = {event["offset"]: event["value"] for event in events
                      if event["event"] == "frame-field"}
            self.assertEqual(fields["0x440"], "0x1020304050607080")
            self.assertEqual(fields["0x448"], "0x0")
            self.assertEqual(events[-1]["event"], "stop-before-original")

    def test_default_and_other_switch_values_preserve_original(self):
        """未启用或开关不为 1 时，保留原返回值且不采集帧字段。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_harness(Path(temporary))
            environment = os.environ.copy()
            environment.pop("XE_VA_STOP_BEFORE_SCHEDULE", None)
            environment.pop("XE_USAGE0_REMAP", None)
            for current in (environment, {**environment, "XE_VA_STOP_BEFORE_SCHEDULE": "0"}):
                result = subprocess.run([str(executable)], env=current, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("original-called-once status=17", result.stdout)
                self.assertNotIn("VA-COMMAND", result.stderr)

    def test_known_incompatible_remap_always_stops_before_original(self):
        """已确认不兼容的映射即使漏设截停开关，也不能执行原驱动命令。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_harness(Path(temporary))
            environment = {**os.environ, "XE_USAGE0_REMAP": "1"}
            environment.pop("XE_VA_STOP_BEFORE_SCHEDULE", None)
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertNotIn("original-called", result.stdout)
            self.assertIn('"event":"stop-before-original"', result.stderr)

    def build_statistics_harness(self, directory):
        """按统计任务的实际大小分配帧数据，检查采集不套用 VDENC 帧布局。"""
        source = directory / "statistics.c"
        source.write_text('#include "' + str(SOURCE) + '"\n' + r'''
#include <sys/mman.h>

// 将对象尾紧贴不可访问页，字段宽度错误或误套 VDENC 大结构会直接使测试失败。
static unsigned char *guarded_frame(size_t bytes) {
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    size_t accessible = (bytes + page - 1) / page * page;
    unsigned char *memory = mmap(NULL, accessible + page, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANON, -1, 0);
    if (memory == MAP_FAILED || mprotect(memory + accessible, page, PROT_NONE)) exit(93);
    return memory + accessible - bytes;
}

// 一旦调用原调度便返回失败标记，统计采集必须先于这个入口退出。
static int original(void *accelerator, uint32_t *request, void *output) {
    (void)accelerator; (void)request; (void)output;
    fputs(getenv("XE_TEST_EXPECT_ORIGINAL") ? "original-called\n" :
          "unexpected-original-call\n", stderr);
    return 91;
}

// 构造 FrameStatisticsContext 及其关联输出；表面值故意不指向可访问内存。
int main(void) {
    unsigned char header[16] = {0}, first[0x38] = {0}, second[0x38] = {0};
    unsigned char *frame = guarded_frame(0x18), *output = guarded_frame(0x10);
    unsigned char pak_frame[0x4a8] = {0};
    unsigned char *context = calloc(1, 0x4730), *commands[] = {first};
    unsigned char *accelerator = guarded_frame(0xa86c);
    uint32_t hardware_threads = 112;
    if (getenv("XE_TEST_ZERO_THREADS")) hardware_threads = 0;
    memcpy(accelerator + 0xa868, &hardware_threads, 4);
    if (getenv("XE_TEST_NO_ACCELERATOR")) accelerator = NULL;
    uint32_t count = 1, kind = 2, output_kind = 16;
    uint64_t capability = 0x1000, output_capability = 0x20;
    uint64_t surfaces[] = {0x1020304050607080ULL, 0x8877665544332211ULL};
    uint64_t output_surface = 0x1122334455667788ULL;
    uint32_t slot = 3, blocks = 0x87654321;
    uint32_t width = 1280, height = 720, block_width = 16, block_height = 16;
    uint32_t curbe_bytes = 32, kernel_count = 1, kernel_bytes = 0x2050;
    uint32_t kernel_storage_capacity = 0x4000, kernel_storage_used = 0x2080;
    uint64_t kernel_storage_base = 0x12340000, kernel_copy = 0x12342000;
    uint32_t vfe_state[9] = {0x70000007, 0, 0, 0x12341000, 0, 0, 0, 0, 0};
    uint32_t curbe_load[4] = {0x70010002, 0, 32, 0x400};
    uint32_t descriptor_load[4] = {0x70020002, 0, 64, 0};
    if (getenv("XE_TEST_NO_CONTEXT")) context = NULL;
    if (getenv("XE_TEST_OTHER_KIND")) {
        kind = 4;
        context = (void *)(uintptr_t)1;
    }
    void *array = commands;
    memcpy(header, &count, 4); memcpy(header + 8, &array, 8);
    memcpy(first, &kind, 4); memcpy(first + 8, &capability, 8);
    memcpy(first + 0x18, &context, 8); memcpy(first + 0x20, &frame, 8);
    unsigned char *next = second;
    memcpy(first + 0x30, &next, 8);
    memcpy(second, &output_kind, 4); memcpy(second + 8, &output_capability, 8);
    memcpy(second + 0x20, &output, 8);
    memcpy(frame, surfaces, sizeof(surfaces));
    memcpy(frame + 0x10, &slot, 4); memcpy(frame + 0x14, &blocks, 4);
    memcpy(output + 8, &output_surface, 8);
    if (context && !getenv("XE_TEST_OTHER_KIND")) {
        memcpy(context + 8, &accelerator, 8);
        memcpy(context + 0x138, &width, 4); memcpy(context + 0x13c, &height, 4);
        memcpy(context + 0x150, &block_width, 4); memcpy(context + 0x158, &block_height, 4);
        memcpy(context + 0x160, &curbe_bytes, 4); memcpy(context + 0x168, &kernel_count, 4);
        memcpy(context + 0x16c, &kernel_storage_used, 4);
        memcpy(context + 0x190, &kernel_storage_capacity, 4);
        memcpy(context + 0x198, &kernel_storage_base, 8);
        memcpy(context + 0x4390, &kernel_copy, 8);
        memcpy(context + 0x4590, &kernel_bytes, 4);
        memcpy(context + 0x427c, vfe_state, sizeof(vfe_state));
        memcpy(context + 0x42c4, curbe_load, sizeof(curbe_load));
        memcpy(context + 0x42e4, descriptor_load, sizeof(descriptor_load));
    }
    if (getenv("XE_TEST_ALLOW_STATS")) {
        int first_result = xe_va_trace_schedule(NULL, (uint32_t *)header, NULL, original);
        if (first_result != 91) return 92;
        if (getenv("XE_TEST_PAK_AFTER_STATS")) {
            kind = 0x20;
            capability = 0x400;
            next = NULL;
            memcpy(first, &kind, 4);
            memcpy(first + 8, &capability, 8);
            unsigned char *pak_frame_pointer = pak_frame;
            memcpy(first + 0x20, &pak_frame_pointer, 8);
            memcpy(first + 0x30, &next, 8);
        }
    }
    return xe_va_trace_schedule(NULL, (uint32_t *)header, NULL, original);
}
''')
        executable = directory / "statistics"
        built = subprocess.run([
            "clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
            str(source), "-o", str(executable),
        ], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        return executable

    def test_statistics_fields_use_their_own_layout_and_integer_widths(self):
        """统计任务只读 24 字节帧，槽号与输出块数分别按 32 位记录。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1"}
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            events = [json.loads(line.removeprefix("VA-COMMAND "))
                      for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
            statistics = [event for event in events if event["event"] == "frame-statistics"]
            self.assertEqual(len(statistics), 1, result.stderr)
            self.assertEqual(statistics[0]["current_surface"], "0x1020304050607080")
            self.assertEqual(statistics[0]["previous_surface"], "0x8877665544332211")
            self.assertEqual(statistics[0]["statistics_slot"], 3)
            self.assertEqual(statistics[0]["blocks_before_schedule"], 0x87654321)
            contexts = [event for event in events if event["event"] == "frame-statistics-context"]
            self.assertEqual(len(contexts), 1, result.stderr)
            for name, expected in {"width": 1280, "height": 720, "block_width": 16,
                                   "block_height": 16, "curbe_bytes": 32, "kernel_count": 1,
                                   "kernel_bytes": 0x2050}.items():
                self.assertEqual(contexts[0][name], expected)
            storage = [event for event in events if event["event"] == "frame-statistics-storage"]
            self.assertEqual(len(storage), 1, result.stderr)
            for name, expected in {"capacity": 0x4000, "used_bytes": 0x2080,
                                   "storage_base": "0x12340000",
                                   "kernel_copy": "0x12342000", "kernel_bytes": 0x2050}.items():
                self.assertEqual(storage[0][name], expected)
            outputs = [event for event in events if event["event"] == "frame-statistics-output"]
            self.assertEqual(len(outputs), 1, result.stderr)
            self.assertEqual(outputs[0]["output_surface"], "0x1122334455667788")
            self.assertFalse(any(event["event"] == "frame-field" for event in events))
            self.assertNotIn("unexpected-original-call", result.stderr)

    def test_statistics_threads_follow_context_accelerator_and_read_only_32_bits(self):
        """从 context+8 对象读取 +0xa868 的 32 位字段，不能使用调度入口参数或越界。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1"}
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            events = [json.loads(line.removeprefix("VA-COMMAND "))
                      for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
            threads = [event for event in events if event["event"] == "frame-statistics-threads"]
            self.assertEqual(len(threads), 1, result.stderr)
            self.assertNotEqual(threads[0]["accelerator"], "0x0")
            self.assertEqual(threads[0]["hardware_threads"], 112)
            self.assertEqual(threads[0]["field_offset"], "0xa868")
            self.assertEqual(events[-1]["event"], "stop-before-original")
            self.assertNotIn("unexpected-original-call", result.stderr)

    def test_statistics_command_state_reports_only_bounded_context_words(self):
        """VFE、CURBE-load 与 descriptor-load 只读各自的已核对 DWORD 范围。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1"}
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            events = [json.loads(line.removeprefix("VA-COMMAND "))
                      for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
            states = [event for event in events if event["event"] == "frame-statistics-command-state"]
            self.assertEqual(len(states), 1, result.stderr)
            self.assertEqual(states[0]["capture_phase"], "before-original-schedule")
            self.assertEqual(states[0]["vfe_dwords"], [
                0x70000007, 0, 0, 0x12341000, 0, 0, 0, 0, 0,
            ])
            self.assertEqual(states[0]["curbe_load_dwords"], [0x70010002, 0, 32, 0x400])
            self.assertEqual(states[0]["descriptor_load_dwords"], [0x70020002, 0, 64, 0])
            self.assertEqual(events[-1]["event"], "stop-before-original")
            self.assertNotIn("unexpected-original-call", result.stderr)

    def test_missing_accelerator_is_unknown_but_zero_threads_is_a_read_value(self):
        """缺失对象记录 null，实测零保留零，不把两种状态混为一谈。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            for flag, expected in (("XE_TEST_NO_ACCELERATOR", None), ("XE_TEST_ZERO_THREADS", 0)):
                environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1", flag: "1"}
                result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 77, result.stderr)
                events = [json.loads(line.removeprefix("VA-COMMAND "))
                          for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
                threads = [event for event in events if event["event"] == "frame-statistics-threads"]
                self.assertEqual(len(threads), 1, result.stderr)
                self.assertEqual(threads[0]["hardware_threads"], expected)
                self.assertEqual(threads[0]["accelerator"] == "0x0", expected is None)
                self.assertEqual(events[-1]["event"], "stop-before-original")
                self.assertNotIn("unexpected-original-call", result.stderr)

    def test_statistics_context_is_optional_and_unknown_kinds_are_not_read(self):
        """空 context 仍可采集帧；其他 kind 不解引用其不可访问的 context。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            for flag in ("XE_TEST_NO_CONTEXT", "XE_TEST_OTHER_KIND"):
                environment = {**os.environ, "XE_VA_STOP_BEFORE_SCHEDULE": "1", flag: "1"}
                result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertNotIn('"event":"frame-statistics-context"', result.stderr)
                self.assertNotIn('"event":"frame-statistics-threads"', result.stderr)
                self.assertNotIn('"event":"frame-statistics-command-state"', result.stderr)
                self.assertNotIn("unexpected-original-call", result.stderr)
                if flag == "XE_TEST_OTHER_KIND":
                    self.assertNotIn('"event":"frame-statistics"', result.stderr)

    def test_legacy_allow_statistics_switch_cannot_bypass_remap_stop(self):
        """已废弃的放行变量不能让统计任务调用存在 RCS reset 的原调度。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ, "XE_USAGE0_REMAP": "1",
                           "XE_VA_ALLOW_STATISTICS_ONLY": "1",
                           "XE_VA_ALLOW_FIRST_SCHEDULE": "1",
                           "XE_TEST_ALLOW_STATS": "1"}
            environment.pop("XE_VA_STOP_BEFORE_SCHEDULE", None)
            result = subprocess.run([str(executable)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertNotIn("unexpected-original-call", result.stderr)
            self.assertNotIn('"event":"allow-statistics-only"', result.stderr)
            self.assertEqual(result.stderr.count('"event":"stop-before-original"'), 1, result.stderr)

    def test_one_statistics_schedule_is_allowed_then_pak_is_stopped(self):
        """仅精确统计请求可放行一次；随后的 PAK 请求必须在原调度前退出。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ,
                           "XE_USAGE0_REMAP": "1",
                           "XE_VA_ALLOW_ONE_FRAME_STATISTICS": "1",
                           "XE_TEST_ALLOW_STATS": "1",
                           "XE_TEST_PAK_AFTER_STATS": "1",
                           "XE_TEST_EXPECT_ORIGINAL": "1"}
            environment.pop("XE_VA_STOP_BEFORE_SCHEDULE", None)
            result = subprocess.run([str(executable)], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertEqual(result.stderr.count("original-called"), 1, result.stderr)
            self.assertIn('"event":"allow-one-statistics-only"', result.stderr)
            self.assertIn('"event":"statistics-only-result","status":91', result.stderr)
            events = [json.loads(line.removeprefix("VA-COMMAND "))
                      for line in result.stderr.splitlines() if line.startswith("VA-COMMAND ")]
            pak_nodes = [event for event in events
                         if event["event"] == "node" and event["depth"] == 0]
            self.assertEqual(len(pak_nodes), 2, result.stderr)
            self.assertEqual(pak_nodes[1]["kind"], 0x20)
            self.assertEqual(pak_nodes[1]["capability"], "0x400")
            self.assertEqual(events[-1]["event"], "stop-before-original")

    def test_one_statistics_gate_rejects_unexpected_first_schedule(self):
        """一次性放行门遇到非统计请求时立即截停，不调用原调度。"""
        with tempfile.TemporaryDirectory() as temporary:
            executable = self.build_statistics_harness(Path(temporary))
            environment = {**os.environ,
                           "XE_USAGE0_REMAP": "1",
                           "XE_VA_ALLOW_ONE_FRAME_STATISTICS": "1",
                           "XE_TEST_OTHER_KIND": "1",
                           "XE_TEST_EXPECT_ORIGINAL": "1"}
            environment.pop("XE_VA_STOP_BEFORE_SCHEDULE", None)
            result = subprocess.run([str(executable)], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertNotIn("original-called", result.stderr)
            self.assertNotIn('"event":"allow-one-statistics-only"', result.stderr)
            self.assertEqual(result.stderr.count('"event":"stop-before-original"'), 1,
                             result.stderr)


if __name__ == "__main__":
    unittest.main()
