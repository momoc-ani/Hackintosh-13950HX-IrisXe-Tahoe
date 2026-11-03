#!/usr/bin/env python3
"""验证 Usage=0 的 kind=0x20 请求只做 capability 映射，不篡改其他节点。"""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tools/diagnostics/gpu-tests/xe-usage0-remap.c"


class Usage0RemapTests(unittest.TestCase):
    """在普通内存中运行真实 C 映射函数，不加载 VA 或连接 GPU。"""

    def test_only_picture_encode_ordinary_capability_is_remapped(self):
        """kind=0x20/cap=0x4000 映射为 0x400，其他类型和链保持不变。"""
        self.assertTrue(SOURCE.exists(), "尚未实现 Usage=0 最小映射诊断")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "harness.c"
            harness.write_text('#define XE_USAGE0_REMAP_TEST\n#include "' + str(SOURCE) + '"\n' + r'''
// 构造三节点请求链，验证精确映射及其余字节保留。
int main(void) {
    unsigned char first[0x438] = {0}, second[0x438] = {0}, third[0x438] = {0};
    uint32_t kind = 0x20, other_kind = 2;
    uint64_t ordinary = 0x4000, vdenc = 0x400;
    memcpy(first, &kind, 4); memcpy(first + 8, &ordinary, 8); memcpy(first + 0x10, &(uint32_t){1001}, 4);
    memcpy(second, &other_kind, 4); memcpy(second + 8, &ordinary, 8); memcpy(second + 0x10, &(uint32_t){1002}, 4);
    memcpy(third, &kind, 4); memcpy(third + 8, &vdenc, 8); memcpy(third + 0x10, &(uint32_t){1003}, 4);
    *(void **)(first + 0x38) = second; *(void **)(second + 0x38) = third;
    unsigned char first_before[sizeof(first)], second_before[sizeof(second)], third_before[sizeof(third)];
    memcpy(first_before, first, sizeof(first));
    memcpy(second_before, second, sizeof(second));
    memcpy(third_before, third, sizeof(third));
    if (xe_usage0_remap_chain(first) != 1) return 10;
    if (*(uint64_t *)(first + 8) != vdenc) return 11;
    if (*(uint64_t *)(second + 8) != ordinary || *(uint64_t *)(third + 8) != vdenc) return 12;
    if (*(void **)(first + 0x38) != second || *(void **)(second + 0x38) != third) return 13;
    if (memcmp(first_before, first, 8) || memcmp(first_before + 16, first + 16, sizeof(first) - 16)) return 15;
    if (memcmp(second_before, second, sizeof(second)) || memcmp(third_before, third, sizeof(third))) return 16;
    if (xe_usage0_remap_chain(NULL) != 0) return 14;
    return 0;
}
''')
            executable = root / "harness"
            built = subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                    str(harness), "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_matching_parameter_entries_are_remapped(self):
        """参数数组只改写 kind=0x20/cap=0x4000 的 capability 字段。"""
        self.assertTrue(SOURCE.exists(), "尚未实现参数数组映射")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "harness.c"
            harness.write_text('#include "' + str(SOURCE) + '"\n' + r'''
// 构造参数数组，验证映射只修改目标项的 capability 字段。
int main(void) {
    unsigned char entries[3 * 0x30];
    unsigned char before[sizeof(entries)];
    uint32_t kind = 0x20, other_kind = 2;
    uint64_t ordinary = 0x4000, vdenc = 0x400;
    memset(entries, 0x5a, sizeof(entries));
    memcpy(entries, &kind, 4); memcpy(entries + 8, &ordinary, 8);
    memcpy(entries + 0x30, &other_kind, 4); memcpy(entries + 0x38, &ordinary, 8);
    memcpy(entries + 0x60, &kind, 4); memcpy(entries + 0x68, &vdenc, 8);
    memcpy(before, entries, sizeof(entries));
    if (xe_usage0_remap_parameter_entries(entries, 3) != 1) return 20;
    if (*(uint64_t *)(entries + 8) != vdenc) return 21;
    if (memcmp(before, entries, 8) || memcmp(before + 16, entries + 16, 0x20)) return 22;
    if (memcmp(before + 0x30, entries + 0x30, 0x30)) return 23;
    if (memcmp(before + 0x60, entries + 0x60, 0x30)) return 24;
    return 0;
}
''')
            executable = root / "harness"
            built = subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                    str(harness), "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
