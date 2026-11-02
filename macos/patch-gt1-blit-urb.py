#!/usr/bin/env python3
"""为 13950HX/A788、32 EU、1 slice、L3=4 构建 Blit3D URB 修补副本。

用法：python3 macos/patch-gt1-blit-urb.py 当前MTL17.bundle 全新输出.bundle
仅支持已核对的 GPUDriversIntel 16.0.32 / MTL17 布局，不覆盖输入或系统安装。
"""
import argparse
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess

EXECUTABLE = Path("Contents/MacOS/AppleIntelTGLGraphicsMTLDriver")
TABLE_OFFSET = 0x43DE50
REFERENCE_OFFSET = 0x96B8C
REFERENCE = bytes.fromhex("48 8d 15 bd 72 3a 00")
OLD_TABLE = (0x78300000, 0x10030380, 0x78300000, 0x10030380,
             0x78310000, 0x10000000, 0x78320000, 0x10000000,
             0x78330000, 0x10000000)


def patch_driver(original: bytes) -> bytes:
    """检查实际初始化函数的表引用，只降低两条 VS URB 命令的条目数。"""
    if original[REFERENCE_OFFSET:REFERENCE_OFFSET + len(REFERENCE)] != REFERENCE:
        raise ValueError("Blit3D 初始化函数布局不匹配，要求已核对的 16.0.32 驱动")
    expected = struct.pack("<10I", *OLD_TABLE)
    if original[TABLE_OFFSET:TABLE_OFFSET + len(expected)] != expected:
        raise ValueError("Blit3D URB 原始表不匹配，或输入已经修补")
    result = bytearray(original)
    # 保留起始地址和单条目大小；512 条目与本机 4-bank 普通渲染分配一致。
    for offset in (TABLE_OFFSET + 4, TABLE_OFFSET + 12):
        struct.pack_into("<I", result, offset, 0x10030200)
    return bytes(result)


def copy_bundle(source: Path, output: Path) -> None:
    """复制 bundle 内容及文件模式，避免继承系统文件的受限标志和所有者。"""
    output.mkdir()
    for item in sorted(source.rglob("*")):
        target = output / item.relative_to(source)
        if item.is_symlink():
            target.symlink_to(item.readlink(), target_is_directory=item.is_dir())
        elif item.is_dir():
            target.mkdir()
        else:
            shutil.copyfile(item, target)
            target.chmod(stat.S_IMODE(item.stat().st_mode))


def build_candidate(source: Path, output: Path) -> dict:
    """先验证输入，再生成和签名完整候选，记录可复核的字节修改范围。"""
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("输出必须是全新目录，不能覆盖输入 bundle")
    original = (source / EXECUTABLE).read_bytes()
    patched = patch_driver(original)
    copy_bundle(source, output)
    executable = output / EXECUTABLE
    executable.write_bytes(patched)
    subprocess.run(["codesign", "-f", "-s", "-", str(executable)], check=True)
    subprocess.run(["codesign", "-f", "-s", "-", str(output)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(output)], check=True)
    record = {
        "profile": "13950HX/A788 32EU 1-slice L3=4",
        "source": str(source.resolve()), "candidate": str(output.resolve()),
        "initializer_reference": hex(REFERENCE_OFFSET), "table": hex(TABLE_OFFSET),
        "changes": [{"offset": hex(TABLE_OFFSET + delta),
                     "before": "0x10030380", "after": "0x10030200",
                     "entries_before": 896, "entries_after": 512} for delta in (4, 12)],
        "libxedrv_changed": False,
    }
    output.with_suffix(".patch.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record


def main() -> None:
    """接收已安装或已构建的 MTL17 bundle，输出适用于本机拓扑的候选。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        record = build_candidate(args.source, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"构建失败：{error}\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
