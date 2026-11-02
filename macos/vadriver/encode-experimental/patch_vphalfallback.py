#!/usr/bin/env python3
"""普通 VPHAL 转换在创建阶段回退；保留编码器内部 scaler。输入/输出均应为实验副本。"""
import argparse
import struct
from pathlib import Path

SITE = 0x13E43
CAVE = 0xE60
ORIGINAL = bytes.fromhex("554889e54157")


def build_guard() -> bytes:
    """生成 x86_64 入口分流代码，遍历请求链，仅拒绝用途 0 的普通 VPHAL 功能。"""
    # 请求使用插件原始节点布局：type +0，capability +8，用途 +0x14，next +0x38。
    sequence = [
        bytes.fromhex("488b068b08488b40084531c0"),  # request header, count, node array, index=0
        "outer:", bytes.fromhex("4139c8"), (bytes.fromhex("0f83"), "original"),
        bytes.fromhex("4e8b0cc0"),  # nodes[index]
        "inner:", bytes.fromhex("4d85c9"), (bytes.fromhex("0f84"), "next"),
        bytes.fromhex("41833902"), (bytes.fromhex("0f85"), "chain"),  # type == 2
        bytes.fromhex("4981790800080000"), (bytes.fromhex("0f84"), "usage"),  # scale
        bytes.fromhex("4981790800020000"), (bytes.fromhex("0f85"), "chain"),  # color
        "usage:", bytes.fromhex("4183791400"), (bytes.fromhex("0f85"), "chain"),
        bytes.fromhex("b801000000c3"),  # unsupported context; VT may choose software transfer
        "chain:", bytes.fromhex("4d8b4938"), (b"\xe9", "inner"),
        "next:", bytes.fromhex("41ffc0"), (b"\xe9", "outer"),
        "original:", ORIGINAL, (b"\xe9", SITE + len(ORIGINAL)),
    ]
    labels, offset = {}, 0
    for item in sequence:
        if isinstance(item, str):
            labels[item.removesuffix(":")] = CAVE + offset
        else:
            offset += len(item) if isinstance(item, bytes) else len(item[0]) + 4
    result = bytearray()
    for item in sequence:
        if isinstance(item, bytes):
            result += item
        elif isinstance(item, tuple):
            opcode, target = item
            destination = labels[target] if isinstance(target, str) else target
            result += opcode + struct.pack("<i", destination - (CAVE + len(result) + len(opcode) + 4))
    if CAVE + len(result) > 0xF00:
        raise ValueError("分流代码超出已有空闲区")
    return bytes(result)


def apply_patch(image: bytes | bytearray) -> bytearray:
    """验证原始指令与空闲区后生成候选；已有相同修补时保持字节不变。"""
    result = bytearray(image)
    guard = build_guard()
    entry = b"\xe9" + struct.pack("<i", CAVE - SITE - 5) + b"\x90"
    if result[SITE:SITE + 6] == entry and result[CAVE:CAVE + len(guard)] == guard:
        return result
    if result[SITE:SITE + 6] != ORIGINAL:
        raise ValueError("上下文创建入口不是已核对的 16.0.32 布局")
    if result[CAVE:CAVE + len(guard)] != bytes(len(guard)):
        raise ValueError("分流代码目标区已被使用")
    result[CAVE:CAVE + len(guard)] = guard
    result[SITE:SITE + 6] = entry
    return result


def main() -> None:
    """只写指定输出副本，不安装 bundle，也不注册系统编码能力。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("请提供独立输出副本")
    args.output.write_bytes(apply_patch(args.source.read_bytes()))
    print("VPHAL fallback: generic scale/color declined; encoder-internal scaler retained")


if __name__ == "__main__":
    main()
