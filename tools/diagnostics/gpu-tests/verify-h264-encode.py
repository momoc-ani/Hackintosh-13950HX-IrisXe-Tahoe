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


def verify_manifest(report: dict, inter: bool = False) -> tuple[int, int, int]:
    """要求硬件状态、完整回调和连续 PTS；P 帧验收须显式启用。"""
    if report.get("phase", "encoded") != "encoded":
        raise ValueError("该报告未完成帧编码，不能作为编码通过证据")
    width, height, count = report["width"], report["height"], report["requested_frames"]
    if min(width, height, count) <= 0 or width % 2 or height % 2:
        raise ValueError("NV12 尺寸或帧数不合法")
    if report.get("intra_only") != (not inter) or report.get("UsingHardwareAcceleratedVideoEncoder") != 1:
        raise ValueError("报告的硬件状态或帧间模式与验收要求不符")
    if report.get("EncoderID") != "local.test.intel.h264" or not report.get("target_gpu_registry_id"):
        raise ValueError("缺少进程内 Intel 编码器选择证据")
    if any(report[key] for key in ("callback_errors", "dropped", "submit_errors", "complete_status")):
        raise ValueError("编码发生错误或丢帧")
    if report["callbacks"] != count or len(report["frames"]) != count:
        raise ValueError("回调数、样本数与请求数不一致")
    for index, frame in enumerate(report["frames"]):
        if frame["bytes"] <= 0 or (not frame["keyframe"] and (not inter or index == 0)):
            raise ValueError("缺少完整样本或首帧关键帧")
        if frame["pts_value"] != index or frame["pts_timescale"] != 30:
            raise ValueError("帧 PTS 不连续")
    if inter and not any(not frame["keyframe"] for frame in report["frames"]):
        raise ValueError("帧间测试没有产生非关键帧")
    return width, height, count


def verify_settings(report: dict, expected_usage=None, expected_bitrate=None, data_rate_limit=None) -> None:
    """用途和软目标须匹配；硬上限另须通过 VideoToolbox 精确属性读回。"""
    if expected_usage is not None and report.get("requested_usage") != expected_usage:
        raise ValueError("会话用途与本组验收要求不匹配")
    if expected_bitrate is not None and (
        report.get("requested_bitrate_bps") != expected_bitrate or
        report.get("configured_average_bitrate_bps") != expected_bitrate or
        report.get("settings_status") != 0
    ):
        raise ValueError("请求码率、配置读回或设置状态与要求不匹配")
    if data_rate_limit is not None:
        observed = report.get("configured_data_rate_limits")
        if (report.get("requested_data_rate_limit") != {"bytes": data_rate_limit[0], "seconds": data_rate_limit[1]} or
            observed not in ([data_rate_limit[0], data_rate_limit[1]], (data_rate_limit[0], data_rate_limit[1])) or
            report.get("data_rate_limit_readback_status") != 0 or report.get("settings_status") != 0):
            raise ValueError("硬上限设置或属性读回与要求不匹配")


def verify_quality(result: dict, minimum_psnr: float) -> None:
    """拒绝虽可解码但内容为空白、旧帧或严重损坏的结果。"""
    if result["samples"] <= 0:
        raise ValueError("未比较任何像素")
    if result["pixel_exact"]:
        return
    psnr = result["psnr_db"]
    if psnr is None or not math.isfinite(psnr) or psnr < minimum_psnr:
        raise ValueError(f"画质未达阈值 {minimum_psnr} dB，实际 {psnr}")


def verify_frame_quality(decoded: Path, reference: Path, width: int, height: int,
                         count: int, minimum_psnr: float) -> dict:
    """逐帧检查有效 NV12 像素，防止少数坏帧被整段平均画质掩盖。"""
    frame_bytes = width * height * 3 // 2
    if decoded.stat().st_size != frame_bytes * count or reference.stat().st_size != frame_bytes * count:
        raise ValueError("逐帧验收输入不包含完整帧")
    scores = []
    with decoded.open("rb") as output, reference.open("rb") as original:
        for index in range(count):
            actual, expected = output.read(frame_bytes), original.read(frame_bytes)
            squares = 0 if actual == expected else sum((a - b) ** 2 for a, b in zip(actual, expected))
            score = None if not squares else 10 * math.log10(255 * 255 * frame_bytes / squares)
            try:
                verify_quality({"samples": frame_bytes, "pixel_exact": squares == 0, "psnr_db": score},
                               minimum_psnr)
            except ValueError as error:
                raise ValueError(f"第 {index} 帧（从 0 起）: {error}") from error
            scores.append(score)
    lossy = [(value, index) for index, value in enumerate(scores) if value is not None]
    worst, index = min(lossy) if lossy else (None, None)
    return {"frame_psnr_db": scores, "minimum_frame_psnr_db": worst, "worst_frame_index": index}


def verify_prefix(prefix: Path, minimum_psnr: float, inter: bool = False,
                  expected_usage=None, expected_bitrate=None, data_rate_limit=None) -> dict:
    """用 ffmpeg 的软件解码器还原所有帧，独立检查实际码流和输入参考。"""
    report = json.loads(Path(str(prefix) + ".json").read_text())
    width, height, count = verify_manifest(report, inter)
    verify_settings(report, expected_usage, expected_bitrate, data_rate_limit)
    encoded = Path(str(prefix) + ".h264")
    reference = Path(str(prefix) + ".nv12")
    decoded = prefix.with_name(prefix.name + "-verified.nv12")
    # 直接指定软件解码，避免把同一个 Intel VA 路径用作编码与解码双重参考。
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-hwaccel", "none", "-threads", "1",
        "-i", str(encoded), "-pix_fmt", "nv12", "-f", "rawvideo", "-y", str(decoded),
    ], check=True, timeout=60)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "frame=pict_type,key_frame,width,height", "-of", "json", str(encoded),
    ], check=True, capture_output=True, text=True, timeout=30)
    frames = json.loads(probe.stdout)["frames"]
    if len(frames) != count or any(
        (frame["width"], frame["height"]) != (width, height) or
        frame["pict_type"] not in (("I", "P") if inter else ("I",)) or
        bool(frame["key_frame"]) != report["frames"][index]["keyframe"]
        for index, frame in enumerate(frames)
    ) or (inter and not any(frame["pict_type"] == "P" for frame in frames)):
        raise ValueError("独立解码出的帧数、尺寸或帧类型不匹配")
    expected = width * height * 3 // 2 * count
    if reference.stat().st_size != expected or decoded.stat().st_size != expected:
        raise ValueError("参考图像或解码图像不包含完整帧")
    quality = pixels.compare_pixels(decoded, reference, False)
    verify_quality(quality, minimum_psnr)
    frame_quality = verify_frame_quality(decoded, reference, width, height, count, minimum_psnr)
    result = {
        "prefix": str(prefix), "width": width, "height": height, "frames": count,
        "intra_only": not inter, "frame_types": [frame["pict_type"] for frame in frames],
        "minimum_psnr_db": minimum_psnr,
        "requested_usage": report.get("requested_usage"),
        "requested_bitrate_bps": report.get("requested_bitrate_bps"),
        "configured_average_bitrate_bps": report.get("configured_average_bitrate_bps"),
        "actual_bitrate_bps": encoded.stat().st_size * 8 * 30 / count,
        "bitrate_control_pass": None,
        "low_latency_rate_control_requested": report.get("low_latency_rate_control_requested"),
        "requested_data_rate_limit": report.get("requested_data_rate_limit"),
        "data_rate_limit_readback_status": report.get("data_rate_limit_readback_status"),
        "target_gpu_registry_id": report["target_gpu_registry_id"], **quality, **frame_quality, "pass": True,
    }
    prefix.with_name(prefix.name + "-quality.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    """保存通过结果，任何会话、码流或画质错误都以非零状态退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--minimum-psnr", type=float, default=35.0)
    parser.add_argument("--inter", action="store_true", help="要求真实 I/P 码流及非关键帧")
    parser.add_argument("--usage", choices=("0", "1", "default"), help="要求记录的用途与本组模式一致")
    parser.add_argument("--bitrate", type=int, help="要求码率设置成功且读回值一致；实际输出另行记录")
    parser.add_argument("--data-rate-limit-bytes", type=int, help="要求硬数据率限制字节数被精确读回")
    parser.add_argument("--data-rate-limit-seconds", type=int, help="要求硬数据率限制窗口秒数被精确读回")
    args = parser.parse_args()
    try:
        usage = int(args.usage) if args.usage in ("0", "1") else args.usage
        if (args.data_rate_limit_bytes is None) != (args.data_rate_limit_seconds is None):
            raise ValueError("硬数据率限制须同时指定字节数和窗口秒数")
        limit = ((args.data_rate_limit_bytes, args.data_rate_limit_seconds)
                 if args.data_rate_limit_bytes is not None else None)
        result = verify_prefix(args.prefix, args.minimum_psnr, args.inter, usage, args.bitrate, limit)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        args.prefix.with_name(args.prefix.name + "-quality.json").write_text(
            json.dumps({"prefix": str(args.prefix), "pass": False, "error": str(error)}, ensure_ascii=False, indent=2) + "\n"
        )
        raise SystemExit(f"FAIL: {error}") from error
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
