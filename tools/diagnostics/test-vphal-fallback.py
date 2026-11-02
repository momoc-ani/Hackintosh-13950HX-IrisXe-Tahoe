#!/usr/bin/env python3
"""在 x86_64 内存中执行真实补丁字节，验证普通 VPHAL 回退与编码器请求分流。"""
import ctypes
import importlib.util
import mmap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("vphal_patch", ROOT / "macos/vadriver/encode-experimental/patch_vphalfallback.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class VPHALFallbackTests(unittest.TestCase):
    """用小型真实请求结构执行补丁入口，不连接 GPU 或系统驱动。"""

    def execute(self, chains, patched=True):
        """把候选机器码映射为函数，用原始入口的返回值 0 检查是否被正确放行。"""
        image = bytearray(patcher.SITE + 32)
        image[patcher.SITE:patcher.SITE + 6] = patcher.ORIGINAL
        image[patcher.SITE + 6:patcher.SITE + 12] = bytes.fromhex("31c0415f5dc3")
        if patched:
            image = patcher.apply_patch(image)
        memory = mmap.mmap(-1, len(image), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        memory.write(image)
        address = ctypes.addressof(ctypes.c_char.from_buffer(memory))
        buffers = []
        pointers = []
        for chain in chains:
            following = 0
            for kind, capability, usage in reversed(chain):
                buffer = ctypes.create_string_buffer(64)
                ctypes.c_uint32.from_buffer(buffer, 0).value = kind
                ctypes.c_uint64.from_buffer(buffer, 8).value = capability
                ctypes.c_uint32.from_buffer(buffer, 0x14).value = usage
                ctypes.c_void_p.from_buffer(buffer, 0x38).value = following
                buffers.append(buffer)
                following = ctypes.addressof(buffer)
            pointers.append(following)
        nodes = (ctypes.c_void_p * len(pointers))(*pointers)
        header = ctypes.create_string_buffer(16)
        ctypes.c_uint32.from_buffer(header, 0).value = len(pointers)
        ctypes.c_void_p.from_buffer(header, 8).value = ctypes.addressof(nodes)
        request = ctypes.c_void_p(ctypes.addressof(header))
        function = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(address + patcher.SITE)
        result = function(None, ctypes.byref(request))
        del function
        memory.close()
        return result

    def test_unpatched_baseline_enters_original(self):
        """证明普通转换在原始入口中确实会继续进入出错的初始化。"""
        self.assertEqual(self.execute([[(2, 0x800, 0)]], False), 0)

    def test_generic_vphal_declined(self):
        """普通缩放与颜色转换必须在提交前报告不支持。"""
        for capability in [0x800, 0x200]:
            with self.subTest(capability=capability):
                self.assertEqual(self.execute([[(2, capability, 0)]]), 1)

    def test_encoder_scaler_kept(self):
        """用途 1 的编码器内部 scaler 继续执行原始初始化。"""
        self.assertEqual(self.execute([[(2, 0x800, 1)]]), 0)

    def test_decoder_and_other_functions_kept(self):
        """不误伤解码能力与编码器的其他输入处理节点。"""
        self.assertEqual(self.execute([[(1, 0x800, 0)], [(2, 0x40000, 0)]]), 0)

    def test_linked_or_later_conversion_declined(self):
        """覆盖节点链及多个入口，普通转换不能藏在后续节点中通过。"""
        self.assertEqual(self.execute([[(2, 0x800, 1), (2, 0x200, 0)]]), 1)
        self.assertEqual(self.execute([[(2, 0x800, 1)], [(2, 0x800, 0)]]), 1)

    def test_empty_request_kept(self):
        """空链表保留原始函数的处理语义。"""
        self.assertEqual(self.execute([]), 0)

    def test_unsupported_or_occupied_image_is_rejected(self):
        """版本不匹配或空闲区已有其他代码时必须拒绝生成候选。"""
        image = bytearray(patcher.SITE + 32)
        with self.assertRaises(ValueError):
            patcher.apply_patch(image)
        image[patcher.SITE:patcher.SITE + 6] = patcher.ORIGINAL
        image[patcher.CAVE] = 0x90
        with self.assertRaises(ValueError):
            patcher.apply_patch(image)

    def test_patching_is_idempotent(self):
        """重复处理已修补的相同候选不会继续改写文件。"""
        image = bytearray(patcher.SITE + 32)
        image[patcher.SITE:patcher.SITE + 6] = patcher.ORIGINAL
        first = patcher.apply_patch(image)
        self.assertEqual(first, patcher.apply_patch(first))


if __name__ == "__main__":
    unittest.main()
