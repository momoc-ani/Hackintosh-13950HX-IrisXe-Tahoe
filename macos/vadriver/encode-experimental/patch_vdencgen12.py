#!/usr/bin/env python3
"""生成独立 Gen12 AVC VDENC 候选：完整 walker、配套长度和单活动参考映射。"""
import argparse
import struct
from pathlib import Path

WALKER, WALKER_TAIL = 0xCA0, 0xDA0
MAP_BODY, MAP = 0xF90, 0x1128


def assemble(base: int, instructions: list) -> bytes:
    """将明确的机器码和命名分支组合为位置相关代码，不增加动态库依赖。"""
    labels, position = {}, base
    for item in instructions:
        if isinstance(item, str):
            labels[item] = position
        else:
            position += len(item) if isinstance(item, bytes) else len(item[0]) + 4
    result = bytearray()
    for item in instructions:
        if isinstance(item, bytes):
            result += item
        elif isinstance(item, tuple):
            opcode, target = item
            destination = labels[target] if isinstance(target, str) else target
            result += opcode + struct.pack("<i", destination - base - len(result) - len(opcode) - 4)
    return bytes(result)


def caves() -> dict[int, bytes]:
    """仅使用核对为空的四处 header padding，避开 SPS、VPHAL 和 HEVC 补丁。"""
    raw = bytes.fromhex
    walker = assemble(WALKER, [
        # 参数为 encoder variant、command context、实际尾指针的地址。
        raw("4c8b97080a0000 4d8b8204020000 4d8b8a0c020000 4c8b1a 4989d2"),
        # source offset 是 token +0xc1c 的 DWORD 偏移，宽度来自实际 source 命令。
        raw("488b97a06a0000 8b921c0c0000 488b4610 8b449008 c1e804 25ff3f0000 89c6"),
        raw("b91b000000 31c0 4c89df f3ab 4d8903 4d894b08 41c70319008770"),
        (b"\xe9", WALKER_TAIL),
    ])
    tail = raw("41897314 41c743380000403f 41c743403f3f3f00 498d436c 498902 4883e810 c3")
    entry = assemble(MAP, [
        # RBX 沿用原提交函数的完整 encoder；保留 fflush 的 FILE 参数。
        raw("57 4c8b83c86b0000 488b4310 4c8b4858 488b90b8000000 4c89ce 41baffffffff 4531db"),
        (b"\xe9", MAP_BODY),
    ])
    mapping = assemble(MAP_BODY, [
        "loop", raw("4839d6"), (raw("0f83"), "finish"), raw("8b06 3d08000471"),
        (raw("0f84"), "reference"), raw("3d09000371"), (raw("0f84"), "slice"),
        (b"\xe9", "next"),
        "reference", raw("4585db"), (raw("0f85"), "apply"), raw("837e0400"),
        (raw("0f85"), "leave"), raw("0fb64e08 f6c180"), (raw("0f85"), "leave"),
        raw("d1e9 83e10f 4183faff"), (raw("0f84"), "slot"), raw("4139ca"),
        (raw("0f85"), "leave"),
        "slot", raw("4189ca b909000000"),
        # 先验证所有 slice 的单引用布局，再进入第二遍改写，拒绝部分提交。
        "inactive", raw("807c0e0080"), (raw("0f85"), "leave"), raw("ffc1 83f928"),
        (raw("0f85"), "inactive"), (b"\xe9", "next"),
        "apply", raw("806608e1"), (b"\xe9", "next"),
        "slice", raw("8b4e08 81e100003f3f 81f900000100"), (raw("0f85"), "leave"),
        # MI 与媒体命令分别使用各自长度；NOOP/ARB_CHECK/BATCH_BUFFER_END 为单字。
        "next", raw("8b06 89c1 c1e91d 85c9"), (raw("0f85"), "media"),
        raw("89c1 c1e917 83e13f 83f900"), (raw("0f84"), "one"),
        raw("83f905"), (raw("0f84"), "one"), raw("83f90a"), (raw("0f84"), "one"),
        raw("83e03f"), (b"\xe9", "length"),
        "media", raw("25ff0f0000"),
        "length", raw("83c002 488d3486"), (b"\xe9", "loop"),
        "one", raw("4883c604"), (b"\xe9", "loop"),
        "finish", raw("4585db"), (raw("0f85"), "leave"), raw("4183faff"),
        (raw("0f84"), "leave"),
        # selected flags 保持原样，只有资源索引须等于 VDENC 活动引用。
        raw("438b8490780c0000 89c1 81e1ff0f0000 85c9"), (raw("0f84"), "leave"),
        raw("413b88b80c0000"), (raw("0f85"), "leave"),
        raw("4189c2 498db8780c0000 31c0 b910000000 f3ab 458990780c0000 41ffc3 4c89ce"),
        (b"\xe9", "loop"),
        "leave", raw("5f"), (b"\xe9", 0x23E39E),
    ])
    regions = {WALKER: walker, WALKER_TAIL: tail, MAP_BODY: mapping, MAP: entry}
    for base, limit in ((WALKER, 0xD00), (WALKER_TAIL, 0xE00), (MAP_BODY, 0x1100), (MAP, 0x1160)):
        if base + len(regions[base]) > limit:
            raise ValueError(f"候选代码超出已核对空闲区 {base:#x}: {len(regions[base])} 字节")
    return regions


def sites() -> list[tuple[int, bytes, bytes]]:
    """同时改变实际写入、slice 累计、整帧预估与提交前引用映射。"""
    original = bytes.fromhex(
        "488b83080a0000498b4d00488b9004020000488b800c02000048894108488911498b4500488d481049894d00")
    hook = bytes.fromhex("4889df 488b75b8 4c89ea")
    hook += b"\xe8" + struct.pack("<i", WALKER - (0x268EA + len(hook) + 5))
    return [
        (0x268EA, original, hook.ljust(len(original), b"\x90")),
        (0x26973, bytes.fromhex("4183c60b"), bytes.fromhex("4183c622")),
        (0x22A16, bytes.fromhex("8d7c3b0d"), bytes.fromhex("8d7c3b24")),
        (0x277AA, bytes.fromhex("e8ef6b2100"), b"\xe8" + struct.pack("<i", MAP - 0x277AF)),
    ]


def apply_patch(image: bytes | bytearray) -> bytearray:
    """仅接受已核对指令与空区，返回副本；不写系统、不注册编码能力。"""
    result, regions = bytearray(image), caves()
    complete = all(result[o:o + len(old)] == new for o, old, new in sites())
    complete &= all(result[o:o + len(code)] == code for o, code in regions.items())
    if complete:
        return result
    for offset, old, _ in sites():
        if result[offset:offset + len(old)] != old:
            raise ValueError(f"VA 入口不匹配已核对版本：{offset:#x}")
    for offset, code in regions.items():
        if result[offset:offset + len(code)] != bytes(len(code)):
            raise ValueError(f"候选代码区已被使用：{offset:#x}")
    for offset, code in regions.items():
        result[offset:offset + len(code)] = code
    for offset, old, new in sites():
        if len(old) != len(new):
            raise ValueError("入口替换长度不一致")
        result[offset:offset + len(new)] = new
    return result


def main() -> None:
    """只写显式指定的独立副本；系统安装与全局能力发布必须另行验收。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("请提供独立输出副本")
    args.output.write_bytes(apply_patch(args.source.read_bytes()))
    print("Gen12 VDENC candidate: 27-DWORD walker; +23 DWORD/slice; single-reference mapping")


if __name__ == "__main__":
    main()
