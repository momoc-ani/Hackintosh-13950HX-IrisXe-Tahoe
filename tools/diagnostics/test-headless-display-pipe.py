#!/usr/bin/env python3
"""验证独显显示模式下只关闭核显 accelerator 的 DisplayPipe。"""

from pathlib import Path
import subprocess
import tempfile


def main() -> int:
    """提取真实 DisplayPipe 开关并覆盖 headless/显式覆盖组合。"""
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/NootedGreen/kern_gen11.cpp").read_text()
    start = source.index("static bool isDisplayPipeForceDisabled()")
    end = source.index("\nstatic bool isV93PlaneGuardEnabled()", start)
    helper = source[start:end]
    program = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
static bool headless = false, dp0 = false, dp1 = false;
static bool checkKernelArgument(const char *arg) {
    if (!std::strcmp(arg, "-ngreenheadless")) return headless;
    if (!std::strcmp(arg, "-ngreendp0")) return dp0;
    if (!std::strcmp(arg, "-ngreendp1")) return dp1;
    return false;
}
static bool isExperimentalMonitorEnabled() { return false; }
static bool isLegacyOwnershipModeEnabled() { return false; }
static bool PE_parse_boot_argn(const char *, int *, unsigned long) { return false; }
static void logMessage(const char *, const char *, ...) {}
#define SYSLOG logMessage
''' + helper + r'''
int main() {
    headless = true;
    assert(isDisplayPipeForceDisabled());
    dp1 = true;
    assert(!isDisplayPipeForceDisabled());
    headless = false;
    dp1 = false;
    assert(!isDisplayPipeForceDisabled());
    dp0 = true;
    assert(isDisplayPipeForceDisabled());
    std::puts("PASS: headless DisplayPipe policy");
}
'''
    with tempfile.TemporaryDirectory(prefix="headless-display-pipe-") as directory:
        src = Path(directory) / "test.cpp"
        binary = Path(directory) / "test"
        src.write_text(program)
        subprocess.run(["clang++", "-std=c++17", str(src), "-o", str(binary)], check=True)
        return subprocess.run([str(binary)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
