#!/bin/zsh
# 恢复本轮 GT1 Blit3D URB 安装之前的完整用户态驱动包。
# 用法：sudo zsh tools/rollback-gt1-blit-urb.sh /Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle.before-gt1-blit-urb-时间
set -euo pipefail

TARGET=/Library/GPUBundles/AppleIntelTGLGraphicsMTLDriver.bundle
BACKUP=${1:?请指定安装记录中的完整备份目录}
CURRENT="$TARGET.before-gt1-rollback-$(date +%Y%m%d-%H%M%S)"

# 确认回退来源属于本轮备份且签名完整，保留现有安装以便再次检查。
check_inputs() {
  [[ $EUID -eq 0 ]] || { print -u2 '请使用 sudo 运行'; exit 1; }
  [[ "$BACKUP" == "$TARGET".before-gt1-blit-urb-* && -d "$BACKUP" && -d "$TARGET" && ! -e "$CURRENT" ]] || {
    print -u2 '备份或当前安装路径无效'; exit 1;
  }
  codesign --verify --strict "$BACKUP"
}

# 移动整个包恢复原有映射来源，失败时把当前安装放回原路径。
restore_bundle() {
  mv "$TARGET" "$CURRENT"
  if ! mv "$BACKUP" "$TARGET"; then
    mv "$CURRENT" "$TARGET"
    return 1
  fi
  codesign --verify --strict "$TARGET"
  print "已恢复：$TARGET"
  print "回退前安装保存在：$CURRENT"
  print '请完整重开应用；EFI 和内核扩展未修改。'
}

check_inputs
restore_bundle
