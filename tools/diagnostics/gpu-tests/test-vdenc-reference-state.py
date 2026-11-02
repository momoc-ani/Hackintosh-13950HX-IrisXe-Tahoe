#!/usr/bin/env python3
"""以实际 I/P 命令验证每个 slice 的默认参考权重与状态偏移。"""
from pathlib import Path
import ctypes
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
HEADER = Path(__file__).with_name('xe-vdenc-reference-state.h')
CAPTURE = ROOT / 'docs/evidence/20261102-kext-icons-and-encode/vdenc-binding'


def commands(batch):
    """按 MI 与媒体命令各自的长度字段遍历完整批次。"""
    offset = 0
    while offset < len(batch):
        header = struct.unpack_from('<I', batch, offset)[0]
        if header >> 29 == 0:
            opcode = (header >> 23) & 63
            length = 1 if opcode in (0, 5, 10) else (header & 63) + 2
        else:
            length = (header & 4095) + 2
        if offset + length * 4 > len(batch):
            raise ValueError('命令长度超过已采集批次')
        yield offset, header, length
        offset += length * 4


class ReferenceStateTests(unittest.TestCase):
    """验证实际生成命令，不运行编码器或连接 GPU。"""

    def test_default_weight_per_slice_preserves_all_original_commands(self):
        """每个 walker 前加入默认权重，保留原命令及所有资源索引。"""
        self.assertTrue(HEADER.exists(), '尚未实现独立的默认权重状态实验')
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'wrapper.c'
            source.write_text('#include "' + str(HEADER) + '"\n' + r'''
// 暴露与进程内实验相同的生成函数，接受实际采集命令。
int transform(unsigned char *batch, size_t capacity, size_t *bytes, unsigned char *token) {
    return xe_vdenc_add_default_weights(batch, capacity, bytes, token, 0xce8);
}
''')
            library = Path(temporary) / 'reference.dylib'
            subprocess.run(['clang', '-dynamiclib', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(library)], check=True, capture_output=True)
            transform = ctypes.CDLL(str(library)).transform
            transform.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p]
            transform.restype = ctypes.c_int
            for frame in [0, 1]:
                with self.subTest(frame=frame):
                    before = (CAPTURE / f'frame-{frame}-before-batch.bin').read_bytes()
                    original_token = (CAPTURE / f'frame-{frame}-before-token.bin').read_bytes()
                    original_cmds = list(commands(before))
                    walkers = [offset for offset, header, _ in original_cmds if header >> 16 == 0x7087]
                    self.assertEqual(len(walkers), 4)
                    buffer = ctypes.create_string_buffer(before, len(before) + 48)
                    token = ctypes.create_string_buffer(original_token, len(original_token))
                    size = ctypes.c_size_t(len(before))
                    self.assertEqual(transform(buffer, len(buffer), ctypes.byref(size), token), 0)
                    self.assertEqual(size.value, len(before) + 48)
                    after = bytes(buffer)
                    rebuilt = bytearray()
                    count = 0
                    previous_header = 0
                    for offset, header, length in commands(after):
                        if header >> 16 == 0x7088:
                            self.assertEqual(after[offset:offset + 12],
                                             struct.pack('<3I', 0x70880001, 0x00010001, 1))
                            count += 1
                        else:
                            if header >> 16 == 0x7087:
                                self.assertEqual(previous_header >> 16, 0x7088)
                            rebuilt.extend(after[offset:offset + length * 4])
                        previous_header = header
                    self.assertEqual(count, 4)
                    self.assertEqual(rebuilt, before)
                    expected = bytearray(original_token)
                    for offset in [0xc24, 0xce0]:
                        struct.pack_into('<I', expected, offset,
                                         struct.unpack_from('<I', expected, offset)[0] + 12)
                    self.assertEqual(bytes(token), expected)
                    # 容量不足时停止，命令与 token 必须保持原样。
                    small = ctypes.create_string_buffer(before, len(before))
                    small_token = ctypes.create_string_buffer(original_token, len(original_token))
                    small_size = ctypes.c_size_t(len(before))
                    self.assertNotEqual(transform(small, len(small), ctypes.byref(small_size), small_token), 0)
                    self.assertEqual(bytes(small), before)
                    self.assertEqual(bytes(small_token), original_token)


    def test_full_gen12_walker_preserves_slice_commands_and_reference_state(self):
        """补齐真实 walker 并设置官方 AVC 宽度与构造默认值，其他命令保留。"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'walker.c'
            source.write_text('#include "' + str(HEADER) + '"\n' + r'''
// 暴露单一 walker 转换，不叠加默认权重或地址扩展。
int transform(unsigned char *batch, size_t capacity, size_t *bytes, unsigned char *token) {
    return xe_vdenc_expand_walker(batch, capacity, bytes, token, 0xce8);
}
''')
            library = Path(temporary) / 'walker.dylib'
            built = subprocess.run(['clang', '-dynamiclib', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(source), '-o', str(library)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            transform = ctypes.CDLL(str(library)).transform
            transform.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p]
            transform.restype = ctypes.c_int
            for frame in [0, 1]:
                with self.subTest(frame=frame):
                    before = (CAPTURE / f'frame-{frame}-before-batch.bin').read_bytes()
                    original_token = (CAPTURE / f'frame-{frame}-before-token.bin').read_bytes()
                    original_cmds = list(commands(before))
                    old_walkers = [before[o:o + n * 4] for o, h, n in original_cmds if h >> 16 == 0x7087]
                    buffer = ctypes.create_string_buffer(before, len(before) + 368)
                    token = ctypes.create_string_buffer(original_token, len(original_token))
                    size = ctypes.c_size_t(len(before))
                    self.assertEqual(transform(buffer, len(buffer), ctypes.byref(size), token), 0)
                    self.assertEqual(size.value, len(before) + 368)
                    after = bytes(buffer)
                    rebuilt = bytearray()
                    index = 0
                    for offset, header, length in commands(after):
                        if header >> 16 == 0x7087:
                            self.assertEqual(length, 27)
                            expected = [0] * 27
                            expected[:4] = struct.unpack('<4I', old_walkers[index])
                            expected[0] = 0x70870019
                            expected[5] = 1279
                            expected[14] = 0x3f400000
                            expected[16] = 0x003f3f3f
                            self.assertEqual(after[offset:offset + 108], struct.pack('<27I', *expected))
                            rebuilt.extend(old_walkers[index])
                            index += 1
                        else:
                            rebuilt.extend(after[offset:offset + length * 4])
                    self.assertEqual(index, 4)
                    self.assertEqual(rebuilt, before)
                    expected_token = bytearray(original_token)
                    for offset in [0xc24, 0xce0]:
                        struct.pack_into('<I', expected_token, offset,
                            struct.unpack_from('<I', expected_token, offset)[0] + 92)
                    self.assertEqual(bytes(token), expected_token)
                    small = ctypes.create_string_buffer(before, len(before))
                    small_token = ctypes.create_string_buffer(original_token, len(original_token))
                    small_size = ctypes.c_size_t(len(before))
                    self.assertNotEqual(transform(small, len(small), ctypes.byref(small_size), small_token), 0)
                    self.assertEqual(bytes(small), before)
                    self.assertEqual(bytes(small_token), original_token)

    def test_single_active_reference_maps_to_store_zero_without_changing_other_fields(self):
        """活动参考位于池槽 0..3 时均映射到 Gen12 槽零，其他字段保持。"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'reference-map.c'
            source.write_text('#include "' + str(HEADER) + '"\n' + r'''
// 对预提交数据生成一一映射，最终地址仍由内核绑定。
int transform(unsigned char *batch, size_t bytes, unsigned char *token) {
    return xe_vdenc_map_single_reference(batch, bytes, token, 0xce8);
}
''')
            library = Path(temporary) / 'reference-map.dylib'
            built = subprocess.run(['clang', '-dynamiclib', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(source), '-o', str(library)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            transform = ctypes.CDLL(str(library)).transform
            transform.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
            transform.restype = ctypes.c_int
            original = (CAPTURE / 'frame-1-before-batch.bin').read_bytes()
            original_token = (CAPTURE / 'frame-1-before-token.bin').read_bytes()
            ref_index = struct.unpack_from('<I', original_token, 0xcb8)[0]
            for slot in range(4):
                with self.subTest(slot=slot):
                    batch = bytearray(original)
                    token = bytearray(original_token)
                    for entry in range(16):
                        struct.pack_into('<I', token, 0xc78 + entry * 4, 0)
                    struct.pack_into('<I', token, 0xc78 + slot * 4, ref_index | 0x9000)
                    for offset, header, _ in commands(batch):
                        if header >> 16 == 0x7104:
                            batch[offset + 8] = slot * 2
                    expected = bytearray(batch)
                    for offset, header, _ in commands(expected):
                        if header >> 16 == 0x7104:
                            expected[offset + 8] = 0
                    expected_token = bytearray(token)
                    expected_token[0xc78:0xcb8] = bytes(64)
                    struct.pack_into('<I', expected_token, 0xc78, ref_index | 0x9000)
                    batch_buffer = ctypes.create_string_buffer(bytes(batch), len(batch))
                    token_buffer = ctypes.create_string_buffer(bytes(token), len(token))
                    self.assertEqual(transform(batch_buffer, len(batch), token_buffer), 0)
                    self.assertEqual(bytes(batch_buffer), expected)
                    self.assertEqual(bytes(token_buffer), expected_token)


if __name__ == '__main__':
    unittest.main()
