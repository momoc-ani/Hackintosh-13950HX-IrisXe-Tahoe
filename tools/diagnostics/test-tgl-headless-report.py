#!/usr/bin/env python3
"""验证用户态 TGL Metal 设备标记为 headless 而 AMD 不受影响。"""

from pathlib import Path
import subprocess
import tempfile


def main() -> int:
    """提取生产判断函数并覆盖 TGL、AMD 和空类名。"""
    root = Path(__file__).resolve().parents[2]
    source = (root / "macos/src/libxedrv.m").read_text()
    start = source.index("static int should_report_tgl_headless")
    end = source.index("\n// Metal 查询", start)
    helper = source[start:end]
    program = r'''
#include <cassert>
#include <cstring>
''' + helper + r'''
int main() {
    assert(should_report_tgl_headless("MTLIGAccelDevice"));
    assert(!should_report_tgl_headless("AMDMTLGFX10Device"));
    assert(!should_report_tgl_headless(nullptr));
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix="tgl-headless-report-") as directory:
        src = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        src.write_text(program)
        subprocess.run(["clang++", "-std=c++17", str(src), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)])
        if result.returncode == 0:
            print("PASS: TGL headless report")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
