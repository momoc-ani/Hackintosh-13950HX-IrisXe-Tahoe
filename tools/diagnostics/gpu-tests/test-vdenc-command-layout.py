#!/usr/bin/env python3
"""在真实采集数据上验证 Gen12 地址命令扩展，不接触 GPU。"""
from pathlib import Path
import ctypes
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
HEADER = Path(__file__).with_name('xe-vdenc-command-layout.h')
CAPTURE = ROOT / 'docs/evidence/20261102-kext-icons-and-encode/vdenc-binding'


class CommandLayoutTests(unittest.TestCase):
    """比较完整命令前后字节及 token 偏移，发现长度或移动范围错误。"""

    def test_expand_address_preserves_following_slice_and_resources(self):
        """I/P 均保留前缀及后续命令，仅增加 34 个未使用的 DWORD。"""
        self.assertTrue(HEADER.exists(), '尚未实现 Gen12 地址命令布局转换')
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'wrapper.c'
            source.write_text('#include "' + str(HEADER) + '"\n' + r'''
// 暴露实际转换函数给 CPU 验证，输入仍是原始采集字节。
int transform(unsigned char *batch, size_t capacity, size_t *bytes, unsigned char *token) {
    return xe_vdenc_expand_address(batch, capacity, bytes, token, 0xce8);
}
''')
            library = Path(temporary) / 'layout.dylib'
            subprocess.run(['clang', '-dynamiclib', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(library)], check=True, capture_output=True)
            transform = ctypes.CDLL(str(library)).transform
            transform.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p]
            transform.restype = ctypes.c_int
            for frame in [0, 1]:
                with self.subTest(frame=frame):
                    original = (CAPTURE / f'frame-{frame}-before-batch.bin').read_bytes()
                    original_token = (CAPTURE / f'frame-{frame}-before-token.bin').read_bytes()
                    buffer = ctypes.create_string_buffer(original, len(original) + 136)
                    token = ctypes.create_string_buffer(original_token, len(original_token))
                    size = ctypes.c_size_t(len(original))
                    self.assertEqual(transform(buffer, len(buffer), ctypes.byref(size), token), 0)
                    self.assertEqual(size.value, len(original) + 136)
                    converted = bytes(buffer)
                    self.assertEqual(converted[:0x274], original[:0x274])
                    self.assertEqual(struct.unpack_from('<I', converted, 0x274)[0], 0x70840045)
                    self.assertEqual(converted[0x278:0x308], original[0x278:0x308])
                    self.assertEqual(converted[0x308:0x390], bytes(136))
                    self.assertEqual(converted[0x390:], original[0x308:])
                    expected_token = bytearray(original_token)
                    for offset in [0xc20, 0xce0]:
                        value = struct.unpack_from('<I', expected_token, offset)[0]
                        struct.pack_into('<I', expected_token, offset, value + 34)
                    self.assertEqual(bytes(token), expected_token)
                    self.assertEqual(struct.unpack_from('<I', converted,
                        struct.unpack_from('<I', token, 0xc20)[0] * 4)[0],
                        struct.unpack_from('<I', original,
                        struct.unpack_from('<I', original_token, 0xc20)[0] * 4)[0])
                    # 原 VA 的长度预估可能比已构建命令多一 DWORD，扩展保留这个差值。
                    padded = bytearray(original_token)
                    struct.pack_into('<I', padded, 0xce0, len(original) // 4 + 1)
                    padded_token = ctypes.create_string_buffer(bytes(padded), len(padded))
                    padded_buffer = ctypes.create_string_buffer(original, len(original) + 136)
                    padded_size = ctypes.c_size_t(len(original))
                    self.assertEqual(transform(padded_buffer, len(padded_buffer),
                        ctypes.byref(padded_size), padded_token), 0)
                    self.assertEqual(struct.unpack_from('<I', padded_token, 0xce0)[0],
                                     len(original) // 4 + 35)
                    # 预留空间不足时不得部分改写命令或 token。
                    small = ctypes.create_string_buffer(original, len(original))
                    untouched = ctypes.create_string_buffer(original_token, len(original_token))
                    count = ctypes.c_size_t(len(original))
                    self.assertNotEqual(transform(small, len(small), ctypes.byref(count), untouched), 0)
                    self.assertEqual(bytes(small), original)
                    self.assertEqual(bytes(untouched), original_token)


if __name__ == '__main__':
    unittest.main()
