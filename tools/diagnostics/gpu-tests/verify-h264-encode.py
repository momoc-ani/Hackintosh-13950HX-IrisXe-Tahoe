#!/usr/bin/env python3
"""独立软件解码 Intel 编码探针的真实码流，并验收帧数、尺寸和完整有效像素。"""
import argparse
import importlib.util
import json
import math
import subprocess
from pathlib import Path


# 编码验收与硬解验收共用逐样本比较器，避免两套像素统计出现偏差。
spec = importlib.util.spec_from_file_location("codec_pixels", Path(__file__).with_name("compare-codec-frames.py"))
pixels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pixels)


def verify_manifest(report: dict) -> tuple[int, int, int]:
    """要求真实硬件状态、完整回调、独立 I 帧与连续 PTS 同时成立。"""
    width, height, count = report["width"], report["height"], report["requested_frames"]
    if min(width, height, count) <= 0 or width % 2 or height % 2:
        raise ValueError("NV12 尺寸或帧数不合法")
    if not report.get("intra_only") or report.get("UsingHardwareAcceleratedVideoEncoder") != 1:
        raise ValueError("需要 Intel 硬件全 I 帧报告")
    if report.get("EncoderID") != "local.test.intel.h264" or not report.get("target_gpu_registry_id"):
        raise ValueError("缺少进程内 Intel 编码器选择证据")
    if any(report[key] for key in ("callback_errors", "dropped", "submit_errors", "complete_status")):
        raise ValueError("编码发生错误或丢帧")
    if report["callbacks"] != count or len(report["frames"]) != count:
        raise ValueError("回调数、样本数与请求数不一致")
    for index, frame in enumerate(report["frames"]):
        if not frame["keyframe"] or frame["bytes"] <= 0:
            raise ValueError("输出并非完整的独立关键帧")
        if frame["pts_value"] != index or frame["pts_timescale"] != 30:
            raise ValueError("帧 PTS 不连续")
    return width, height, count


def verify_quality(result: dict, minimum_psnr: float) -> None:
    """拒绝虽可解码但内容为空白、旧帧或严重损坏的结果。"""
    if result["samples"] <= 0:
        raise ValueError("未比较任何像素")
    if result["pixel_exact"]:
        return
    psnr = result["psnr_db"]
    if psnr is None or not math.isfinite(psnr) or psnr < minimum_psnr:
        raise ValueError(f"画质未达阈值 {minimum_psnr} dB，实际 {psnr}")


def verify_prefix(prefix: Path, minimum_psnr: float) -> dict:
    """用 ffmpeg 的软件解码器还原所有帧，独立检查实际码流和输入参考。"""
    report = json.loads(Path(str(prefix) + ".json").read_text())
    width, height, count = verify_manifest(report)
    encoded = Path(str(prefix) + ".h264")
    reference = Path(str(prefix) + ".nv12")
    decoded = prefix.with_name(prefix.name + "-verified.nv12")
    # 直接指定软件解码，避免把同一个 Intel VA 路径用作编码与解码双重参考。
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-hwaccel", "none", "-threads", "1",
        "-i", str(encoded), "-pix_fmt", "nv12", "-f", "rawvideo", "-y", str(decoded),
    ], check=True, timeout=60)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "frame=pict_type,key_frame,width,height", "-of", "json", str(encoded),
    ], check=True, capture_output=True, text=True, timeout=30)
    frames = json.loads(probe.stdout)["frames"]
    if len(frames) != count or any(
        (frame["width"], frame["height"], frame["pict_type"], frame["key_frame"]) != (width, height, "I", 1)
        for frame in frames
    ):
        raise ValueError("独立解码出的帧数、尺寸或帧类型不匹配")
    expected = width * height * 3 // 2 * count
    if reference.stat().st_size != expected or decoded.stat().st_size != expected:
        raise ValueError("参考图像或解码图像不包含完整帧")
    quality = pixels.compare_pixels(decoded, reference, False)
    verify_quality(quality, minimum_psnr)
    result = {
        "prefix": str(prefix), "width": width, "height": height, "frames": count,
        "intra_only": True, "minimum_psnr_db": minimum_psnr,
        "target_gpu_registry_id": report["target_gpu_registry_id"], **quality, "pass": True,
    }
    prefix.with_name(prefix.name + "-quality.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    """保存通过结果，任何会话、码流或画质错误都以非零状态退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--minimum-psnr", type=float, default=35.0)
    args = parser.parse_args()
    try:
        result = verify_prefix(args.prefix, args.minimum_psnr)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        args.prefix.with_name(args.prefix.name + "-quality.json").write_text(
            json.dumps({"prefix": str(args.prefix), "pass": False, "error": str(error)}, ensure_ascii=False, indent=2) + "\n"
        )
        raise SystemExit(f"FAIL: {error}") from error
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
