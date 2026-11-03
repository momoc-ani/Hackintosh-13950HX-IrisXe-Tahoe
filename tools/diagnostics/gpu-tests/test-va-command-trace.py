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


if __name__ == "__main__":
    unittest.main()
