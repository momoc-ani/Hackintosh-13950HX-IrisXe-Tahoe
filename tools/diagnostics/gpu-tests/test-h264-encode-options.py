#!/usr/bin/env python3
"""运行真实探针的选项描述模式，确认各用途与码率不会被固定值覆盖。"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("xe-h264-encode-lab.m")


class EncodeOptionTests(unittest.TestCase):
    """只描述参数，不注册编码器、不创建会话、不连接 GPU。"""

    @classmethod
    def setUpClass(cls):
        """编译实际探针，使测试覆盖最终执行文件的解析行为。"""
        cls.temporary = tempfile.TemporaryDirectory()
        cls.program = Path(cls.temporary.name) / "encoder"
        subprocess.run(["clang", "-fobjc-arc", "-Wall", "-Wextra", "-Werror", str(SOURCE),
                        "-framework", "Foundation", "-framework", "Metal", "-framework", "VideoToolbox",
                        "-framework", "CoreMedia", "-framework", "CoreVideo", "-o", str(cls.program)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        """删除测试编译产物，保留仓库与系统目录。"""
        cls.temporary.cleanup()

    def describe(self, *options):
        """请求探针返回实际选项，任何未支持选项都会使测试失败。"""
        result = subprocess.run([str(self.program), "unused", "1280", "720", "2",
                                 "--describe-options", *options], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_existing_default_stays_usage_one_at_eight_megabits(self):
        """扩展探针不能悄悄改变此前所有成功样本的用途与码率。"""
        result = self.describe()
        self.assertEqual(result["requested_usage"], 1)
        self.assertEqual(result["requested_bitrate_bps"], 8000000)
        self.assertTrue(result["real_time"])

    def test_ordinary_export_and_low_bitrate_are_not_overridden(self):
        """Usage=0、2 Mbit/s 与非实时属性必须传递到会话配置。"""
        result = self.describe("--usage", "0", "--bitrate", "2000000", "--inter")
        self.assertEqual(result["requested_usage"], 0)
        self.assertEqual(result["requested_bitrate_bps"], 2000000)
        self.assertFalse(result["real_time"])
        self.assertFalse(result["intra_only"])

    def test_unspecified_usage_and_public_low_latency_are_distinct(self):
        """默认选择与公开低延迟参数单独记录，均不强塞私有 Usage=1。"""
        generic = self.describe("--usage", "default", "--create-only")
        low = self.describe("--low-latency")
        self.assertEqual(generic["requested_usage"], "default")
        self.assertFalse(generic["low_latency_rate_control_requested"])
        self.assertTrue(generic["create_only"])
        self.assertEqual(low["requested_usage"], "default")
        self.assertTrue(low["low_latency_rate_control_requested"])
        self.assertTrue(low["real_time"])

    def test_invalid_or_contradictory_options_fail_before_session_creation(self):
        """错误码率、非法用途和互斥输入不能变为其他有效测试。"""
        for options in [("--usage", "2"), ("--bitrate", "0"), ("--bitrate", "2000000x"),
                        ("--bitrate", "2147483648"), ("--usage", "0", "--low-latency"),
                        ("--bgra", "--encoder-pool")]:
            with self.subTest(options=options):
                result = subprocess.run([str(self.program), "unused", "1280", "720", "2",
                                         "--describe-options", *options], capture_output=True)
                self.assertEqual(result.returncode, 2)

    def test_raw_nv12_input_is_explicit_and_complete(self):
        """压缩压力样本须保留路径，并在连接 GPU 前拒绝不完整的有效像素。"""
        raw = Path(self.temporary.name) / "input.nv12"
        raw.write_bytes(bytes([80]) * 1280 * 720 * 3)
        result = self.describe("--input-nv12", str(raw))
        self.assertEqual(result["input_nv12"], str(raw))
        raw.write_bytes(bytes([80]) * (1280 * 720 * 3 - 1))
        result = subprocess.run([str(self.program), "unused", "1280", "720", "2",
                                 "--describe-options", "--input-nv12", str(raw)], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_hard_data_rate_limit_requires_bytes_and_window(self):
        """独立 hard-cap 参数需同时有正数字节数与秒数并完整进入报告。"""
        result = self.describe("--data-rate-limit-bytes", "250000",
                               "--data-rate-limit-seconds", "1")
        self.assertEqual(result["data_rate_limit"], {"bytes": 250000, "seconds": 1})
        for options in [("--data-rate-limit-bytes", "250000"),
                        ("--data-rate-limit-seconds", "1"),
                        ("--data-rate-limit-bytes", "0", "--data-rate-limit-seconds", "1"),
                        ("--data-rate-limit-bytes", "250000", "--data-rate-limit-seconds", "0")]:
            with self.subTest(options=options):
                result = subprocess.run([str(self.program), "unused", "1280", "720", "2",
                                         "--describe-options", *options], capture_output=True)
                self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
