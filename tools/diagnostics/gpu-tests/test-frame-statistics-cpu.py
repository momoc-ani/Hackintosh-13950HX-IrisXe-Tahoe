#!/usr/bin/env python3
"""用独立 VA 的真实 CPU 构建器核对统计任务的媒体命令，完全不连接 GPU。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(__file__).with_name("inspect-frame-statistics-cpu.py")
IMAGE = ROOT / "build/validation-encode-and-kext-icons-20261102/va-gen12-native/AppleIntelTGLGraphicsVADriver"
PATCH_SOURCE = ROOT / "macos/vadriver/encode-experimental/patch_vfelegacy.py"


class FrameStatisticsCPUTests(unittest.TestCase):
    """执行实际构建函数，检查分辨率、硬件线程夹具与命令位置的关系。"""

    def audit(self, width, height, threads=224):
        """加载诊断工具并将所有驱动对象替换为普通 CPU 内存。"""
        self.assertTrue(SOURCE.exists(), "尚未实现统计任务的独立 CPU 构建核对")
        spec = importlib.util.spec_from_file_location("statistics_cpu", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.audit_builder(IMAGE, width, height, threads)

    def test_real_builder_uses_gen12_17_dword_media_walker(self):
        """720p 首个统计 walker 为 17 DWORD，前后偏移与三种媒体状态长度一致。"""
        result = self.audit(1280, 720)
        self.assertFalse(result["gpu_commands_executed"])
        self.assertEqual(result["block_dimensions"], [80, 45])
        self.assertEqual(result["walker_dwords"], [
            0x7103000f, 0, 0, 0, 0, 0, 0, 0x07ff07ff,
            0x002d0050, 0, 0, 0x10000, 1, 0x002d0050, 0, 80, 0x002d0000,
        ])
        self.assertEqual(result["vfe_dwords"][0], 0x70000007)
        self.assertEqual(result["vfe_dwords"][3] >> 16, 223)
        self.assertEqual(result["vfe_dwords"][6:], [0, 0, 0])
        self.assertEqual(result["curbe_load_dwords"], [0x70010002, 0, 32, 0x400])
        self.assertEqual(result["descriptor_load_dwords"], [0x70020002, 0, 64, 0])
        self.assertEqual(result["walker_offset_bytes"], 0xac)
        self.assertEqual(result["walker_end_bytes"], 0xf0)

    def test_dimensions_and_threads_come_from_separate_context_fields(self):
        """1080p 向上取整成 120×68 块，线程夹具只改变 VFE 线程字段。"""
        result = self.audit(1920, 1080, 112)
        self.assertEqual(result["block_dimensions"], [120, 68])
        self.assertEqual(result["walker_dwords"][8], 0x00440078)
        self.assertEqual(result["walker_dwords"][13], 0x00440078)
        self.assertEqual(result["walker_dwords"][15:], [120, 0x00440000])
        self.assertEqual(result["vfe_dwords"][3] >> 16, 111)
        self.assertEqual(result["walker_end_bytes"] - result["walker_offset_bytes"], 68)

    def patch_module(self):
        """加载仅用于独立 CPU 验证副本的 Gen12 VFE 分派修补生成器。"""
        self.assertTrue(PATCH_SOURCE.exists(), "尚未实现 VFE legacy 分派验证副本")
        spec = importlib.util.spec_from_file_location("vfe_legacy", PATCH_SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_legacy_candidate_changes_only_the_vfe_dispatch_bit(self):
        """实际生成的 batch 仅 DW3 bit 6 变化，walker、表面引用及后续指令保留。"""
        patch = self.patch_module()
        spec = importlib.util.spec_from_file_location("statistics_cpu", SOURCE)
        inspector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inspector)
        original = IMAGE.read_bytes()
        candidate = patch.apply_patch(original)
        changes = [offset for offset, (before, after) in enumerate(zip(original, candidate)) if before != after]
        self.assertEqual(len(original), len(candidate))
        self.assertEqual(changes, [0x2c9b0])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vfe-legacy"
            path.write_bytes(candidate)
            for width, height, threads in ((1280, 720, 224), (1920, 1080, 112)):
                baseline = inspector.audit_builder(IMAGE, width, height, threads)
                patched = inspector.audit_builder(path, width, height, threads)
                expected_batch = baseline["batch_dwords"].copy()
                self.assertEqual(expected_batch[0x68 // 4], 0x70000007)
                expected_batch[0x74 // 4] |= 0x40
                self.assertEqual(patched["batch_dwords"], expected_batch)
                self.assertEqual(patched["vfe_dwords"][3], baseline["vfe_dwords"][3] | 0x40)
                for field in ("walker_dwords", "walker_offset_bytes", "walker_end_bytes",
                              "curbe_load_dwords", "descriptor_load_dwords", "batch_bytes"):
                    self.assertEqual(patched[field], baseline[field])

    def test_patch_is_idempotent_and_rejects_a_different_instruction(self):
        """已生成副本保持不变；指令边界或原值不匹配时拒绝写入。"""
        patch = self.patch_module()
        original = bytearray(IMAGE.read_bytes())
        candidate = patch.apply_patch(original)
        self.assertEqual(patch.apply_patch(candidate), candidate)
        original[0x2c9ae] = 0x90
        with self.assertRaises(ValueError):
            patch.apply_patch(original)


if __name__ == "__main__":
    unittest.main()
