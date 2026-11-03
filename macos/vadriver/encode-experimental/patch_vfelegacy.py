#!/usr/bin/env python3
"""生成 Gen12 媒体 walker 的 VFE legacy 分派 CPU 验证副本，尚未通过 GPU 验收。"""
import argparse
from pathlib import Path

SITE = 0x2c9ae
ORIGINAL = bytes.fromhex("81ca00100000")
LEGACY = bytes.fromhex("81ca40100000")


def apply_patch(image):
    """仅在已核对的 walker 分支设置 DW3 bit 6，保留线程、URB 及其他命令。"""
    if image[:8] != bytes.fromhex("cffaedfe07000001"):
        raise ValueError("需要已分析的 x86_64 VA Mach-O")
    if image[0x2c956:0x2c95d] != bytes.fromhex("488d05c30d2f00"):
        raise ValueError("VFE 模板引用不是已核对的 16.0.32 布局")
    if image[SITE:SITE + len(ORIGINAL)] not in (ORIGINAL, LEGACY):
        raise ValueError("VFE walker 分支指令不匹配")
    result = bytearray(image)
    # Gen12 的 bit 6 为 1 表示关闭融合 EU；只改变 OR 指令的最低立即数字节。
    result[SITE:SITE + len(LEGACY)] = LEGACY
    return result


def main():
    """只写全新独立文件，不签名、安装或执行 VA，也不改已有 Gen12 候选。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve() or args.output.exists():
        parser.error("输出必须是全新的独立文件")
    result = apply_patch(args.source.read_bytes())
    with args.output.open("xb") as stream:
        stream.write(result)
    print("CPU 验证副本：仅 VFE DW3 bit 6；未重新签名、未安装、未通过 GPU 验收")


if __name__ == "__main__":
    main()
