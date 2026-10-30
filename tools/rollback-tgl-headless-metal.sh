#!/bin/zsh
# 回退完整 TGL 无头 Metal 用户态 bundle。
# 用法：sudo zsh tools/rollback-tgl-headless-metal.sh

set -e

TARGET="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle"
BACKUP="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle.before-tgl-headless-20261030"

# 检查 root 权限和本轮安装前备份。
check_inputs() {
  [[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
  [[ -d "$BACKUP" ]] || { echo "找不到回退备份：$BACKUP" >&2; exit 1; }
}

# 恢复安装前的 Metal bundle。
restore_bundle() {
  ditto "$BACKUP" "$TARGET"
  chown -R root:wheel "$TARGET"
}

# 让旧 bundle 在重启或新进程中重新加载。
restart_iconservices() {
  killall iconservicesagent 2>/dev/null || true
}

# 输出回退结果，不修改 EFI 或 boot-args。
report() {
  echo "已恢复：$TARGET"
  echo "EFI 和 boot-args 未修改"
}

check_inputs
restore_bundle
restart_iconservices
report
