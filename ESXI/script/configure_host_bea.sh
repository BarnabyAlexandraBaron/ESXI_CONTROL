#!/bin/bash
# 🌸 可爱 IPv6 配置助手 🌸
# ===========================
# 🐰 用法：
#   ./configure_host_bea.sh <ipv6_address>
#   例如：
#     ./configure_host_bea.sh 2001:db8::1234
#
# 🌟 功能说明：
#   本脚本会使用 NetworkManager 为指定的接口 (ens36)
#   配置指定的 IPv6 地址并立即生效。
# ===========================

# 🧩 检查是否提供了 IPv6 地址参数
if [ -z "$1" ]; then
  echo "❌ 错误：请提供一个 IPv6 地址作为参数（例如：2001:db8::1）"
  echo "💡 用法：$0 <ipv6_address>"
  exit 1
fi

# 🎯 参数定义
IPV6_ADDRESS_NO_MASK="$1"
IPV6_ADDRESS_WITH_MASK="${IPV6_ADDRESS_NO_MASK}/64"
INTERFACE="ens36"
CONNECTION_NAME="ens36"

echo "🌼 将使用 IPv6 地址：${IPV6_ADDRESS_WITH_MASK} 配置接口 ${INTERFACE}"

# 🌈 步骤 1：把 ens36 纳入 NetworkManager 管理
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🐾 步骤 1️⃣ : 将 ${INTERFACE} 纳入 NetworkManager 管理"
sudo nmcli device set "${INTERFACE}" managed yes

# 🍀 步骤 2：新建或修改连接配置
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🐾 步骤 2️⃣ : 新建或修改连接 ${CONNECTION_NAME}"

# 检查连接是否存在
if sudo nmcli connection show "${CONNECTION_NAME}" &>/dev/null; then
  echo "🔧 连接 ${CONNECTION_NAME} 已存在，正在修改配置..."
  sudo nmcli connection modify "${CONNECTION_NAME}" \
    ipv6.method manual \
    ipv6.addresses "${IPV6_ADDRESS_WITH_MASK}"
else
  echo "✨ 连接 ${CONNECTION_NAME} 不存在，正在新建中..."
  sudo nmcli connection add type ethernet ifname "${INTERFACE}" con-name "${CONNECTION_NAME}" autoconnect yes \
    ipv6.method manual \
    ipv6.addresses "${IPV6_ADDRESS_WITH_MASK}"
fi

# 🌻 步骤 3：立即生效
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🐾 步骤 3️⃣ : 激活连接 ${CONNECTION_NAME}"
sudo nmcli connection up "${CONNECTION_NAME}"

# 🦋 步骤 4：确保接口处于 up 状态
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🐾 步骤 4️⃣ : 确保接口 ${INTERFACE} 已启动"
sudo ifconfig "${INTERFACE}" up

# 🍰 完成提示
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 配置完成！请使用以下命令查看结果："
echo "   🐚 ip a show ${INTERFACE}"
echo "   🐚 nmcli device show ${INTERFACE}"
echo "💖 一切顺利！Have a lovely networking day~ 🌸"
