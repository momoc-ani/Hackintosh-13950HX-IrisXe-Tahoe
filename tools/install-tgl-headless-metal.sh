#!/bin/zsh
# 安装完整 TGL 无头 Metal 链路的用户态 bundle。
# 用法：sudo zsh tools/install-tgl-headless-metal.sh

set -e

ROOT=${0:A:h:h}
SOURCE="$ROOT/macos/out/AppleIntelTGLGraphicsMTLDriver.bundle"
TARGET="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle"
BACKUP="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle.before-tgl-headless-20261030"
ICON_FLAG="/Library/GPUBundles/xe-iconservices-disable"

# 检查 root 权限、构建产物和当前回退状态。
check_inputs() {
  [[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
  [[ -d "$SOURCE" ]] || { echo "找不到构建产物：$SOURCE" >&2; exit 1; }
  [[ -d "$TARGET" ]] || { echo "找不到系统 bundle：$TARGET" >&2; exit 1; }
  [[ ! -e "$ICON_FLAG" ]] || { echo "请先删除 IconServices 隔离标志：$ICON_FLAG" >&2; exit 1; }
}

# 保存当前 bundle，保证可以回到本轮安装前状态。
backup_current() {
  if [[ ! -e "$BACKUP" ]]; then
    ditto "$TARGET" "$BACKUP"
  fi
}

# 安装支持 headless 报告的 TGL Metal bundle。
install_bundle() {
  ditto "$SOURCE" "$TARGET"
  chown -R root:wheel "$TARGET"
}

# 现有进程不会重新加载 Metal bundle，因此让 IconServices 退出等待系统重启。
restart_iconservices() {
  killall iconservicesagent 2>/dev/null || true
}

# 输出安装结果和回退位置。
report() {
  echo "已安装：$TARGET"
  echo "已备份：$BACKUP"
  echo "未启用 IconServices 隔离标志"
  echo "请重启后验证 TGL Metal headless=1"
}

check_inputs
backup_current
install_bundle
restart_iconservices
report
