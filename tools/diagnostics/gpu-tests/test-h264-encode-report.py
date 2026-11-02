#!/usr/bin/env python3
"""验证编码验收不会把空回调、丢帧、帧间模式或坏画面当作通过。"""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("encode_validation", Path(__file__).with_name("verify-h264-encode.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EncodeEvidenceTests(unittest.TestCase):
    """覆盖本轮真实出现的误判条件，不提交任何 GPU 命令。"""

    def report(self):
        """返回最小的两帧硬件编码报告，用于逐项破坏证据。"""
        return {
            "width": 16, "height": 16, "requested_frames": 2, "callbacks": 2,
            "callback_errors": 0, "dropped": 0, "submit_errors": 0,
            "complete_status": 0, "intra_only": True,
            "UsingHardwareAcceleratedVideoEncoder": True,
            "EncoderID": "local.test.intel.h264", "target_gpu_registry_id": 123,
            "frames": [
                {"bytes": 100, "pts_value": 0, "pts_timescale": 30, "keyframe": True},
                {"bytes": 100, "pts_value": 1, "pts_timescale": 30, "keyframe": True},
            ],
        }

    def test_valid_hardware_report(self):
        """完整的本地 Intel 全 I 帧报告可以进入像素验收。"""
        self.assertEqual(module.verify_manifest(self.report()), (16, 16, 2))

    def test_callback_success_without_frames_fails(self):
        """回调计数与真实样本数必须一致。"""
        report = self.report()
        report["frames"] = []
        with self.assertRaises(ValueError):
            module.verify_manifest(report)

    def test_dropped_or_software_or_inter_frames_fail(self):
        """分别拒绝丢帧、软件回退和超出探针范围的帧间模式。"""
        for key, value in [("dropped", 1), ("UsingHardwareAcceleratedVideoEncoder", False), ("intra_only", False)]:
            with self.subTest(key=key):
                report = self.report()
                report[key] = value
                with self.assertRaises(ValueError):
                    module.verify_manifest(report)

    def test_duplicate_pts_fails(self):
        """重复上一帧不能作为新一帧的编码结果。"""
        report = self.report()
        report["frames"][1]["pts_value"] = 0
        with self.assertRaises(ValueError):
            module.verify_manifest(report)

    def test_bad_pixels_fail_despite_successful_decode(self):
        """复现返回码流但内容错误的低 PSNR 结果。"""
        with self.assertRaises(ValueError):
            module.verify_quality({"samples": 768, "psnr_db": 6.29, "pixel_exact": False}, 35.0)

    def test_good_lossy_or_exact_pixels_pass(self):
        """允许正常有损编码及相同像素，后者的 PSNR 为无穷大。"""
        module.verify_quality({"samples": 768, "psnr_db": 52.0, "pixel_exact": False}, 35.0)
        module.verify_quality({"samples": 768, "psnr_db": None, "pixel_exact": True}, 35.0)


if __name__ == "__main__":
    unittest.main()
