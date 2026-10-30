#!/bin/zsh
# 安装可回退的 IconServices Intel TGL 隔离方案。
# 用法：sudo zsh tools/install-iconservices-isolation.sh

set -e

ROOT=${0:A:h:h}
SOURCE="$ROOT/macos/out/AppleIntelTGLGraphicsMTLDriver.bundle"
TARGET="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle"
BACKUP="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle.before-iconservices-isolation-20261030"
FLAG="/Library/GPUBundles/xe-iconservices-disable"

# 检查构建产物和 root 权限，避免半安装状态。
check_inputs() {
  [[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
  [[ -d "$SOURCE" ]] || { echo "找不到构建产物：$SOURCE" >&2; exit 1; }
  [[ -d "$TARGET" ]] || { echo "找不到系统 bundle：$TARGET" >&2; exit 1; }
}

# 保存当前 bundle，便于删除隔离开关后完整恢复。
backup_current() {
  if [[ ! -e "$BACKUP" ]]; then
    ditto "$TARGET" "$BACKUP"
  fi
}

# 安装新 bundle，并创建仅对 iconservicesagent 生效的回退开关。
install_isolation() {
  ditto "$SOURCE" "$TARGET"
  touch "$FLAG"
  chown -R root:wheel "$TARGET" "$FLAG"
}

# 让新的 libxedrv 在 IconServices 下一次初始化时生效。
restart_iconservices() {
  killall iconservicesagent 2>/dev/null || true
}

# 输出可核对的安装结果。
report() {
  echo "已安装：$TARGET"
  echo "已备份：$BACKUP"
  echo "已启用：$FLAG"
}

check_inputs
backup_current
install_isolation
restart_iconservices
report
