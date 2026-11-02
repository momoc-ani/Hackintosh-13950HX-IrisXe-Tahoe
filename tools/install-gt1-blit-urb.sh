#!/bin/zsh
# 安装已验证的 GT1 Blit3D URB 候选；保留整包备份，不改 EFI 或内核扩展。
# 用法：sudo zsh tools/install-gt1-blit-urb.sh /绝对路径/候选.bundle
set -euo pipefail

SOURCE=${1:?请指定已构建并验证的候选 bundle}
TARGET=/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$TARGET.before-gt1-blit-urb-$STAMP"
WORK=""
MOVED_OLD=0

# 核对当前 32 EU / 4-bank 拓扑、候选计数和未改动的适配库。
check_inputs() {
  [[ $EUID -eq 0 ]] || { print -u2 '请使用 sudo 运行'; exit 1; }
  [[ -d "$SOURCE" && -d "$TARGET" && ! -e "$BACKUP" ]] || { print -u2 '输入或备份路径无效'; exit 1; }
  /usr/bin/python3 - "$SOURCE" "$TARGET" <<'PY'
import plistlib, struct, subprocess, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:])
devices = plistlib.loads(subprocess.check_output(['/usr/sbin/ioreg','-a','-r','-c','IntelAccelerator']))
if not any(device.get('xe-topo') == 0x2004 for device in devices):
    sys.exit('当前未确认 32 EU / L3=4 拓扑，停止安装')
relative = Path('Contents/MacOS/AppleIntelTGLGraphicsMTLDriver')
old, new = (target / relative).read_bytes(), (source / relative).read_bytes()
expected = (0x78300000,0x10030200,0x78300000,0x10030200,0x78310000,0x10000000,0x78320000,0x10000000,0x78330000,0x10000000)
if struct.unpack_from('<10I', new, 0x43de50) != expected:
    sys.exit('候选 Blit3D URB 表不匹配')
if struct.unpack_from('<2I', old, 0x43de50) != (0x78300000,0x10030380):
    sys.exit('当前安装不符合本轮原始状态，停止覆盖')
library = Path('Contents/MacOS/libxedrv.dylib')
if (source / library).read_bytes() != (target / library).read_bytes():
    sys.exit('候选改变了 libxedrv，本轮只允许替换 Blit3D URB 表')
PY
  codesign --verify --strict "$SOURCE"
}

# 在同一文件系统中准备完整新包，避免覆盖正在被进程映射的可执行文件。
prepare_bundle() {
  WORK=$(mktemp -d /Library/GPUBundles/.gt1-blit-urb.XXXXXX)
  ditto "$SOURCE" "$WORK/AppleIntelTGLGraphicsMTLDriver.bundle"
  chown -R root:wheel "$WORK/AppleIntelTGLGraphicsMTLDriver.bundle"
  codesign --verify --strict "$WORK/AppleIntelTGLGraphicsMTLDriver.bundle"
}

# 安装失败时恢复本轮之前的完整 bundle，并保留失败候选供排查。
restore_on_error() {
  trap - ZERR
  if [[ $MOVED_OLD -eq 1 && -d "$BACKUP" ]]; then
    [[ ! -e "$TARGET" ]] || mv "$TARGET" "$WORK/failed.bundle"
    mv "$BACKUP" "$TARGET"
    print -u2 "安装失败，已恢复原包；候选保留在 $WORK"
  fi
}

# 通过目录改名切换 bundle，使已运行进程继续持有旧文件的映射。
activate_bundle() {
  mv "$TARGET" "$BACKUP"
  MOVED_OLD=1
  mv "$WORK/AppleIntelTGLGraphicsMTLDriver.bundle" "$TARGET"
  codesign --verify --strict "$TARGET"
  rmdir "$WORK"
}

# 输出精确回退路径；应用重新启动后才会使用新的用户态驱动。
report() {
  print "安装时间：$STAMP"
  print "已安装：$TARGET"
  print "完整备份：$BACKUP"
  print "回退命令：sudo zsh tools/rollback-gt1-blit-urb.sh '$BACKUP'"
  print 'EFI、boot-args、内核扩展均未修改；请完整重开受影响应用后验收。'
}

check_inputs
prepare_bundle
trap restore_on_error ZERR
activate_bundle
trap - ZERR
report
