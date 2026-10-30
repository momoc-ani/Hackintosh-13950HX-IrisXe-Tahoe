#!/usr/bin/env python3
"""验证 IconServices TGL 设备隔离只匹配目标进程和显式回退开关。"""

from pathlib import Path
import subprocess
import tempfile


def main() -> int:
    """提取真实进程判断函数并覆盖启用、禁用、非目标进程场景。"""
    root = Path(__file__).resolve().parents[2]
    source = (root / "macos/src/libxedrv.m").read_text()
    start = source.index("static int should_block_iconservices_device(const char *program")
    end = source.index("\n// 根据进程名", start)
    helper = source[start:end]
    program = r'''
#include <cassert>
#include <cstring>
''' + helper + r'''
int main() {
    assert(should_block_iconservices_device("iconservicesagent", 1));
    assert(!should_block_iconservices_device("iconservicesagent", 0));
    assert(!should_block_iconservices_device("WindowServer", 1));
    assert(!should_block_iconservices_device(nullptr, 1));
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix="iconservices-device-isolation-") as directory:
        src = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        src.write_text(program)
        subprocess.run(["clang++", "-std=c++17", str(src), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)])
        if result.returncode == 0:
            print("PASS: IconServices TGL device isolation")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
