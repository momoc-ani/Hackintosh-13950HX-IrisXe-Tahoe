#!/bin/zsh
# 回退 IconServices Intel TGL 隔离方案。
# 用法：sudo zsh tools/rollback-iconservices-isolation.sh

set -e

TARGET="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle"
BACKUP="/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle.before-iconservices-isolation-20261030"
FLAG="/Library/GPUBundles/xe-iconservices-disable"

# 检查 root 权限和安装前备份，防止恢复到不完整状态。
check_inputs() {
  [[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
  [[ -d "$BACKUP" ]] || { echo "找不到回退备份：$BACKUP" >&2; exit 1; }
}

# 恢复隔离方案安装前的 Metal bundle。
restore_bundle() {
  ditto "$BACKUP" "$TARGET"
  chown -R root:wheel "$TARGET"
}

# 删除只针对 iconservicesagent 的隔离标志。
remove_flag() {
  if [[ -e "$FLAG" ]]; then
    rm -f "$FLAG"
  fi
}

# 让 IconServices 在下一次启动时重新读取恢复后的 bundle。
restart_iconservices() {
  killall iconservicesagent 2>/dev/null || true
}

# 输出回退结果；不修改 OpenCore 或 boot-args。
report() {
  echo "已恢复：$TARGET"
  echo "已移除：$FLAG"
  echo "OpenCore/boot-args 未修改；当前 -x 安全启动参数保持不变"
}

check_inputs
restore_bundle
remove_flag
restart_iconservices
report
