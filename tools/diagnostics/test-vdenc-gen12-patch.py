#!/usr/bin/env python3
"""执行独立 VA 候选的真实机器码，检查 walker、参考映射及已有补丁保留。"""
import ctypes
import importlib.util
import mmap
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "macos/vadriver/encode-experimental/patch_vdencgen12.py"
CAPTURE = ROOT / "docs/evidence/20261102-kext-icons-and-encode/vdenc-binding"


def write_pointer(buffer, offset, address):
    """为模拟对象填写真实进程指针，不连接 IOAccelerator。"""
    ctypes.c_void_p.from_buffer(buffer, offset).value = address


class NativeVDENCTests(unittest.TestCase):
    """候选在匿名可执行内存中运行，所有 GPU 对象均为普通内存。"""

    @classmethod
    def setUpClass(cls):
        """加载补丁生成器；缺失实现时明确失败。"""
        if not SOURCE.exists():
            raise AssertionError("尚未实现不依赖注入库的独立 Gen12 VA 候选")
        spec = importlib.util.spec_from_file_location("vdenc_gen12", SOURCE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def image(self):
        """构造含已确认入口字节和其他补丁标记的最小目标映像。"""
        image = bytearray(0x23e400)
        for offset, original, _ in self.module.sites():
            image[offset:offset + len(original)] = original
        for offset in (0xb00, 0xd20, 0xe00, 0xe60, 0xf00, 0xf40, 0x1100):
            image[offset:offset + 16] = bytes([0xcc]) * 16
        return image

    def mapped(self):
        """映射补丁副本，用一个记录调用的桩替代 fflush。"""
        image = self.module.apply_patch(self.image())
        # 原调用仍必须抵达 fflush，记录 FILE 参数并返回 37。
        image[0x23e39e:0x23e3ab] = bytes.fromhex("c70701000000b825000000c3").ljust(13, b"\x90")
        # 保存 RBX，以第二参数传入完整 encoder，模拟原函数的保存寄存器布局。
        stub = bytes.fromhex("534889f3") + b"\xe8" + struct.pack("<i", self.module.MAP - 0x809) + bytes.fromhex("5bc3")
        image[0x800:0x800 + len(stub)] = stub
        memory = mmap.mmap(-1, len(image), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        memory.write(image)
        return memory, ctypes.addressof(ctypes.c_char.from_buffer(memory))

    def test_walker_machine_code_preserves_fields_and_uses_source_width(self):
        """原 4 DWORD 扩为官方 27 DWORD，更新尾指针并保留相邻模板。"""
        memory, address = self.mapped()
        function = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            address + self.module.WALKER)
        try:
            for index in (0, 1):
                batch = (CAPTURE / f"frame-{index}-before-batch.bin").read_bytes()
                token = ctypes.create_string_buffer((CAPTURE / f"frame-{index}-before-token.bin").read_bytes())
                source = ctypes.create_string_buffer(batch)
                encoder, context = ctypes.create_string_buffer(0x6b00), ctypes.create_string_buffer(0x78)
                template = ctypes.create_string_buffer(bytes([0x5a]) * 0x300, 0x300)
                write_pointer(encoder, 0xa08, ctypes.addressof(template))
                write_pointer(encoder, 0x6aa0, ctypes.addressof(token))
                write_pointer(context, 0x10, ctypes.addressof(source))
                for offset in range(0, len(batch) - 16, 4):
                    if batch[offset:offset + 4] != bytes.fromhex("02008770"):
                        continue
                    ctypes.memmove(ctypes.addressof(template) + 0x204, batch[offset:offset + 16], 16)
                    output = ctypes.create_string_buffer(bytes([0xaa]) * 124, 124)
                    tail = ctypes.c_void_p(ctypes.addressof(output))
                    returned = function(encoder, context, ctypes.byref(tail))
                    expected = [0] * 27
                    expected[:4] = struct.unpack_from("<4I", batch, offset)
                    expected[0], expected[5] = 0x70870019, 1279
                    expected[14], expected[16] = 0x3f400000, 0x003f3f3f
                    self.assertEqual(bytes(output)[:108], struct.pack("<27I", *expected))
                    self.assertEqual(bytes(output)[108:], bytes([0xaa]) * 16)
                    self.assertEqual(bytes(template)[0x214:], bytes([0x5a]) * (0x300 - 0x214))
                    self.assertEqual(tail.value, ctypes.addressof(output) + 108)
                    self.assertEqual(returned, ctypes.addressof(output) + 92)
        finally:
            del function
            memory.close()

    def remap(self, batch, token):
        """通过真实 RBX 调用约定运行最终提交前映射，返回 batch/token/fflush 证据。"""
        memory, address = self.mapped()
        data, resource = ctypes.create_string_buffer(bytes(batch)), ctypes.create_string_buffer(bytes(token))
        encoder, video = ctypes.create_string_buffer(0x6c20), ctypes.create_string_buffer(0xd0)
        write_pointer(encoder, 0x10, ctypes.addressof(video))
        write_pointer(encoder, 0x6bc8, ctypes.addressof(resource))
        write_pointer(video, 0x58, ctypes.addressof(data))
        write_pointer(video, 0xb8, ctypes.addressof(data) + len(batch))
        stream = ctypes.c_int(0)
        function = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(address + 0x800)
        try:
            result = function(ctypes.byref(stream), encoder)
            return bytes(data)[:len(batch)], bytes(resource)[:len(token)], result, stream.value
        finally:
            del function
            memory.close()

    def test_reference_machine_code_maps_pool_slots_and_retains_flags(self):
        """四个已观察池槽都映射到槽零，保留长短期等 flag 与 resource 标志。"""
        original = (CAPTURE / "frame-1-before-batch.bin").read_bytes()
        token = (CAPTURE / "frame-1-before-token.bin").read_bytes()
        for slot in range(4):
            batch, resource = bytearray(original), bytearray(token)
            for offset in range(0, len(batch) - 40, 4):
                if batch[offset:offset + 4] == bytes.fromhex("08000471"):
                    batch[offset + 8] = slot * 2 | 0x40
            resource[0xc78:0xcb8] = bytes(64)
            struct.pack_into("<I", resource, 0xc78 + slot * 4, 0x1007)
            struct.pack_into("<I", resource, 0xcb8, 7)
            after, mapped, result, flushed = self.remap(batch, resource)
            expected = bytearray(batch)
            for offset in range(0, len(expected) - 40, 4):
                if expected[offset:offset + 4] == bytes.fromhex("08000471"):
                    expected[offset + 8] = 0x40
            expected_resource = bytearray(resource)
            expected_resource[0xc78:0xcb8] = bytes(64)
            struct.pack_into("<I", expected_resource, 0xc78, 0x1007)
            self.assertEqual(after, expected)
            self.assertEqual(mapped, expected_resource)
            self.assertEqual((result, flushed), (37, 1))

    def test_intra_and_multiple_active_references_are_unchanged(self):
        """I 帧及未验收的双参考保持原数据，不进行部分映射。"""
        for index in (0, 1):
            batch = bytearray((CAPTURE / f"frame-{index}-before-batch.bin").read_bytes())
            token = (CAPTURE / f"frame-{index}-before-token.bin").read_bytes()
            if index:
                for offset in range(0, len(batch) - 44, 4):
                    if batch[offset:offset + 4] == bytes.fromhex("09000371"):
                        struct.pack_into("<I", batch, offset + 8, 2 << 16)
            after, mapped, result, flushed = self.remap(batch, token)
            self.assertEqual(after, batch)
            self.assertEqual(mapped, token)
            self.assertEqual((result, flushed), (37, 1))

    def test_patch_preserves_other_fixes_and_is_idempotent(self):
        """只改变明确列出的入口/空闲区域，重复运行不改写其他补丁。"""
        original = self.image()
        after = self.module.apply_patch(original)
        allowed = set()
        for offset, before, _ in self.module.sites():
            allowed.update(range(offset, offset + len(before)))
        for offset, code in self.module.caves().items():
            allowed.update(range(offset, offset + len(code)))
        self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, after))))
        self.assertEqual(len(after), len(original))
        self.assertEqual(after, self.module.apply_patch(after))

    def test_wrong_version_or_occupied_cave_is_rejected(self):
        """入口字节不同或候选代码区已占用时拒绝生成。"""
        image = self.image()
        image[0x268ea] = 0x90
        with self.assertRaises(ValueError):
            self.module.apply_patch(image)
        image = self.image()
        image[self.module.MAP] = 0xcc
        with self.assertRaises(ValueError):
            self.module.apply_patch(image)


if __name__ == "__main__":
    unittest.main()
