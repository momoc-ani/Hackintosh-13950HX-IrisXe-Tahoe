#!/usr/bin/env python3
"""验证编码验收不会把空回调、丢帧、帧间模式或坏画面当作通过。"""
import importlib.util
import tempfile
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

    def test_inter_manifest_requires_actual_non_key_samples(self):
        """帧间验收必须显式启用，并包含完整的非关键帧输出。"""
        report = self.report()
        report["intra_only"] = False
        report["frames"][1]["keyframe"] = False
        self.assertEqual(module.verify_manifest(report, inter=True), (16, 16, 2))
        report["intra_only"] = 0
        self.assertEqual(module.verify_manifest(report, inter=True), (16, 16, 2))
        report["frames"][1]["keyframe"] = True
        with self.assertRaises(ValueError):
            module.verify_manifest(report, inter=True)

    def test_inter_manifest_still_rejects_dropped_or_out_of_order_frames(self):
        """放开 P 帧不会放开回调错误、软件回退和重复时间戳。"""
        for field, value in [("callback_errors", 1), ("UsingHardwareAcceleratedVideoEncoder", False)]:
            with self.subTest(field=field):
                report = self.report()
                report["intra_only"] = False
                report["frames"][1]["keyframe"] = False
                report[field] = value
                with self.assertRaises(ValueError):
                    module.verify_manifest(report, inter=True)

    def test_one_bad_frame_cannot_hide_in_sequence_average(self):
        """64 帧中一帧误差 5 的整体画质仍很好，但该帧必须使验收失败。"""
        with tempfile.TemporaryDirectory() as temporary:
            reference, decoded = Path(temporary) / "reference", Path(temporary) / "decoded"
            reference.write_bytes(bytes([80]) * 384 * 64)
            decoded.write_bytes(bytes([80]) * 384 * 63 + bytes([85]) * 384)
            module.verify_quality(module.pixels.compare_pixels(decoded, reference, False), 35.0)
            with self.assertRaisesRegex(ValueError, "63"):
                module.verify_frame_quality(decoded, reference, 16, 16, 64, 35.0)

    def test_frame_quality_records_worst_frame_and_exact_frames(self):
        """保存每帧指标，零误差帧不应掩盖真正最差的一帧。"""
        with tempfile.TemporaryDirectory() as temporary:
            reference, decoded = Path(temporary) / "reference", Path(temporary) / "decoded"
            reference.write_bytes(bytes([80]) * 384 * 3)
            decoded.write_bytes(bytes([80]) * 384 + bytes([81]) * 384 + bytes([82]) * 384)
            result = module.verify_frame_quality(decoded, reference, 16, 16, 3, 35.0)
            self.assertEqual(result["worst_frame_index"], 2)
            self.assertEqual(result["frame_psnr_db"][0], None)
            self.assertAlmostEqual(result["minimum_frame_psnr_db"], 42.1102036954)
            self.assertEqual(len(result["frame_psnr_db"]), 3)

    def test_truncated_frame_is_rejected_before_quality_comparison(self):
        """缺少一字节的帧不能用其余完整像素计算出合格画质。"""
        with tempfile.TemporaryDirectory() as temporary:
            reference, decoded = Path(temporary) / "reference", Path(temporary) / "decoded"
            reference.write_bytes(bytes([80]) * 384)
            decoded.write_bytes(bytes([80]) * 383)
            with self.assertRaises(ValueError):
                module.verify_frame_quality(decoded, reference, 16, 16, 1, 35.0)

    def test_create_only_is_never_an_encoded_frame_pass(self):
        """即使保留了旧样本字段，仅创建会话的报告也必须被拒绝。"""
        report = self.report()
        report["phase"] = "created-only"
        with self.assertRaises(ValueError):
            module.verify_manifest(report)

    def test_requested_usage_must_match_the_tested_mode(self):
        """固定 Usage=1 的旧结果不能贴上普通导出模式的标签。"""
        report = {"requested_usage": 1, "requested_bitrate_bps": 2000000,
                  "configured_average_bitrate_bps": 2000000, "settings_status": 0}
        with self.assertRaises(ValueError):
            module.verify_settings(report, 0, 2000000)
        report["requested_usage"] = 0
        module.verify_settings(report, 0, 2000000)

    def test_bitrate_requires_successful_settings_and_readback(self):
        """请求值与配置读回必须吻合，不能只记录命令行的期望码率。"""
        report = {"requested_usage": 1, "requested_bitrate_bps": 2000000,
                  "configured_average_bitrate_bps": 1000000, "settings_status": 0}
        with self.assertRaises(ValueError):
            module.verify_settings(report, 1, 2000000)
        report["configured_average_bitrate_bps"] = 2000000
        report["settings_status"] = -12902
        with self.assertRaises(ValueError):
            module.verify_settings(report, 1, 2000000)


if __name__ == "__main__":
    unittest.main()
