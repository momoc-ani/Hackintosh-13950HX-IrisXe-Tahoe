#!/usr/bin/env python3
"""比较 xe-codec-decode 的硬件与软件帧，验收完整有效 Y/UV 像素。"""
import argparse
import array
import json
import math
import sys
from pathlib import Path


# 比较一对完整视频输出；原始数据相等的块直接通过，差异块逐样本计算误差。
def compare_pixels(left: Path, right: Path, ten_bit: bool) -> dict:
    """逐块读取 NV12/P010，10-bit 只比较各 16-bit 字中的高 10 位。"""
    if left.stat().st_size != right.stat().st_size:
        raise ValueError("硬件与软件图像长度不相等")
    count = different = total = squares = largest = 0
    with left.open("rb") as hardware, right.open("rb") as software:
        while block := hardware.read(1024 * 1024):
            reference = software.read(len(block))
            count += len(block) // (2 if ten_bit else 1)
            if block == reference:
                continue
            if ten_bit:
                a, b = array.array("H"), array.array("H")
                a.frombytes(block); b.frombytes(reference)
                if sys.byteorder != "little":
                    a.byteswap(); b.byteswap()
                differences = (abs((x >> 6) - (y >> 6)) for x, y in zip(a, b))
            else:
                differences = (abs(x - y) for x, y in zip(block, reference))
            for delta in differences:
                different += delta != 0
                total += delta
                squares += delta * delta
                largest = max(largest, delta)
    if not count:
        raise ValueError("没有输出像素")
    peak = 1023 if ten_bit else 255
    return {
        "samples": count, "different_samples": different, "max_abs_difference": largest,
        "mean_abs_difference": total / count,
        "psnr_db": None if not squares else 10 * math.log10(peak * peak * count / squares),
        "pixel_exact": different == 0,
    }


# 将会话成功、帧完整性、PTS 和像素内容一起作为通过条件。
def compare_pair(root: Path, name: str) -> dict:
    """验证报告中真实硬件/软件状态，并排除帧数或图像布局不匹配。"""
    hw = json.loads((root / f"{name}-hardware.json").read_text())
    sw = json.loads((root / f"{name}-software.json").read_text())
    if not (hw["transfer_ok"] and sw["transfer_ok"] and hw["using_hardware"] and not sw["using_hardware"]):
        raise ValueError("硬件或软件解码会话没有通过")
    if hw["source"] != sw["source"]:
        raise ValueError("硬件与软件报告使用了不同输入文件")
    if not hw["frames"] or hw["frames"] != sw["frames"]:
        raise ValueError("硬件/软件帧数为空或不匹配")
    if hw["frame_metadata"] != sw["frame_metadata"]:
        raise ValueError("帧 PTS、布局或尺寸不一致")
    expected = sum(row["bytes"] for row in hw["frame_metadata"])
    for mode in ("hardware", "software"):
        if (root / f"{name}-{mode}.yuv").stat().st_size != expected:
            raise ValueError("图像文件长度与各帧记录不匹配")
    ten_bit = hw["frame_metadata"][0]["format"] == 0x78343230
    pixels = compare_pixels(root / f"{name}-hardware.yuv", root / f"{name}-software.yuv", ten_bit)
    return {"fixture": name, "frames": hw["frames"], "using_hardware": True, **pixels}


# 所有已生成报告都必须有配对软件参考，出现非零差异时返回失败退出码。
def main() -> None:
    """保存逐文件摘要；该命令不会把 callback 成功直接当成解码通过。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    names = sorted(path.name.removesuffix("-hardware.json") for path in root.glob("*-hardware.json"))
    if not names:
        raise SystemExit("没有硬件解码报告")
    results = []
    for name in names:
        try:
            result = compare_pair(root, name)
        except (KeyError, ValueError, OSError) as error:
            result = {"fixture": name, "pixel_exact": False, "error": str(error)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
    (root / "pixel-comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(any(not row["pixel_exact"] for row in results))


if __name__ == "__main__":
    main()
