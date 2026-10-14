import os
import sys
import tempfile
import unittest
from datetime import datetime

# 将 src 目录加入导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import core as cv


class CoreLogicTestCase(unittest.TestCase):
    def test_parse_filename_times(self):
        # 标准 14 位时间戳
        fn = "cam1_20261014100000_20261014100100.mp4"
        res = cv.parse_filename_times(fn)
        self.assertIsNotNone(res)
        start, end = res
        self.assertEqual(start, datetime(2026, 10, 14, 10, 0, 0))
        self.assertEqual(end, datetime(2026, 10, 14, 10, 1, 0))

        # 带 compact_ 前缀
        fn_compact = "compact_cam1_20261014100000_20261014100100.mp4"
        res_compact = cv.parse_filename_times(fn_compact)
        self.assertIsNotNone(res_compact)

        # 非法格式
        self.assertIsNone(cv.parse_filename_times("invalid_name.mp4"))
        self.assertIsNone(cv.parse_filename_times("test_123_456.mp4"))

    def test_snap_opus_sample_rate(self):
        self.assertEqual(cv._snap_opus_sample_rate(44100), 48000)
        self.assertEqual(cv._snap_opus_sample_rate(48000), 48000)
        self.assertEqual(cv._snap_opus_sample_rate(16000), 16000)
        self.assertEqual(cv._snap_opus_sample_rate(11025), 12000)
        self.assertEqual(cv._snap_opus_sample_rate("invalid"), 48000)

    def test_formatting_helpers(self):
        self.assertEqual(cv.format_seconds(12.3456), "12.3456")
        self.assertEqual(cv.format_seconds(12.0), "12")
        self.assertEqual(cv.format_hms(3665), "1 小时 1 分 5 秒")
        self.assertEqual(cv.format_hms(59), "0 小时 0 分 59 秒")
        self.assertEqual(cv.format_eta(125), "2 分 5 秒")
        self.assertEqual(cv.format_eta(-5), "0 分 0 秒")

    def test_compute_encode_maxrate(self):
        # 源码率 2Mbps，总时长 60s，保留 30s -> 放大 2 倍 -> 4Mbps
        maxrate = cv.compute_encode_maxrate(2_000_000, 60.0, 30.0)
        self.assertEqual(maxrate, 4_000_000)

        # 极限极小保留时长 -> 触发上限兜底 ENCODE_MAX_MAXRATE (100Mbps)
        extreme_rate = cv.compute_encode_maxrate(2_000_000, 600.0, 0.1)
        self.assertEqual(extreme_rate, cv.ENCODE_MAX_MAXRATE)

        # 极低码率 -> 触发下限兜底 ENCODE_MIN_MAXRATE (500kbps)
        low_rate = cv.compute_encode_maxrate(100_000, 60.0, 60.0)
        self.assertEqual(low_rate, cv.ENCODE_MIN_MAXRATE)

    def test_build_video_encoder_attempts_qsv(self):
        # 验证 Intel QSV 参数构造
        attempts = cv.build_video_encoder_attempts("hevc_qsv", 22, 2_000_000)
        self.assertEqual(len(attempts), 1)
        label, args = attempts[0]
        self.assertEqual(label, "ICQ")
        self.assertIn("-c:v", args)
        self.assertIn("hevc_qsv", args)
        self.assertIn("-global_quality", args)
        self.assertIn("22", args)
        self.assertIn("-maxrate", args)
        self.assertIn("2000000", args)

    def test_build_video_encoder_attempts_nvenc(self):
        attempts = cv.build_video_encoder_attempts("hevc_nvenc", 20, 1_500_000)
        self.assertEqual(len(attempts), 1)
        label, args = attempts[0]
        self.assertEqual(label, "CQ")
        self.assertIn("hevc_nvenc", args)
        self.assertIn("-cq", args)
        self.assertIn("20", args)

    def test_user_config_persistence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig_config_path = cv.CONFIG_PATH
            try:
                cv.CONFIG_PATH = os.path.join(tmp_dir, "test_config.json")
                test_cfg = {
                    "input_dir": "D:\\test\\in",
                    "output_dir": "D:\\test\\out",
                    "output_mode": "lossless",
                }
                cv.save_user_config(test_cfg)
                loaded = cv.load_user_config()
                self.assertEqual(loaded.get("input_dir"), "D:\\test\\in")
                self.assertEqual(loaded.get("output_mode"), "lossless")
            finally:
                cv.CONFIG_PATH = orig_config_path

    def test_build_segments(self):
        # 60 秒视频，0-10 秒运动，10-50 秒静止，50-60 秒运动
        duration = 60.0
        kept_times = [0.0, 5.0, 10.0, 50.0, 55.0, 60.0]
        segments = cv.build_segments(duration, kept_times)
        # 应该包含 normal, static, normal 三段
        self.assertGreaterEqual(len(segments), 2)
        kinds = [s["kind"] for s in segments]
        self.assertIn("static", kinds)
        self.assertIn("normal", kinds)

    def test_clean_stale_temp_dirs(self):
        # 创建一个模拟的临时目录
        temp_dir = tempfile.gettempdir()
        dummy_dir = os.path.join(temp_dir, "compact_video_test_stale")
        os.makedirs(dummy_dir, exist_ok=True)
        # 故意将 mtime 修改为 10 小时前
        old_time = os.path.getmtime(dummy_dir) - 36000
        os.utime(dummy_dir, (old_time, old_time))

        cleaned = cv.clean_stale_temp_dirs(max_age_hours=2)
        self.assertGreaterEqual(cleaned, 1)
        self.assertFalse(os.path.exists(dummy_dir))


if __name__ == "__main__":
    unittest.main()
