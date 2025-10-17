#!/bin/sh
# -----------------------------------------------------------------------------
# 适用于 ESXi 6.x
# 创建 vSwitch 和同名端口组，并将 vSwitch 安全策略设置为 Accept
# 端口组默认继承 vSwitch 策略（无需使用 inherit 参数）
# -----------------------------------------------------------------------------
set -euo pipefail
set -x  # 开启调试输出

if [ $# -lt 1 ]; then
  echo "用法: $0 <端口组1> [<端口组2> ...]"
  exit 1
fi

for PG_NAME in "$@"; do
  VSWITCH="$PG_NAME"

  # 1️⃣ 创建 vSwitch
  if ! esxcli network vswitch standard list | grep -q "$VSWITCH"; then
    echo "==> 创建 vSwitch $VSWITCH"
    esxcli network vswitch standard add --vswitch-name="$VSWITCH"
  else
    echo "==> vSwitch $VSWITCH 已存在"
  fi

  # 2️⃣ 设置 vSwitch 安全策略为 Accept
  echo "==> 设置 vSwitch $VSWITCH 的安全策略为 Accept"
 
  esxcli network vswitch standard policy security set \
    -v "$VSWITCH" \
    -p true \
    -m true \
    -f true

  # 3️⃣ 创建端口组（继承 vSwitch 策略）
  if ! esxcli network vswitch standard portgroup list | grep -q "$PG_NAME"; then
    echo "==> 创建端口组 $PG_NAME"
    esxcli network vswitch standard portgroup add \
      --portgroup-name="$PG_NAME" \
      --vswitch-name="$VSWITCH"
  else
    echo "==> 端口组 $PG_NAME 已存在"
  fi

  echo "完成：vSwitch 和端口组 $PG_NAME 已创建并设置。"
  echo
done

echo "✅ 所有对象创建完成。请验证："
echo "  esxcli network vswitch standard list"
echo "  esxcli network vswitch standard portgroup list"
