#!/bin/bash

# 检查是否提供了 IPv6 地址作为参数
if [ -z "$1" ]; then
  echo "错误：请提供一个 IPv6 地址作为参数（例如：2000:db8::1）。"
  echo "用法：$0 <ipv6_address>"
  exit 1
fi

IPV6_ADDRESS_NO_MASK="$1"
IPV6_ADDRESS_WITH_MASK="${IPV6_ADDRESS_NO_MASK}/64"
INTERFACE="ens36"
CONNECTION_NAME="ens36"

echo "将使用 IPv6 地址：${IPV6_ADDRESS_WITH_MASK} 配置接口 ${INTERFACE}"

# 1. 把 ens36 纳入 NetworkManager 管理（自动生成连接配置）
echo "--- 步骤 1: 将 ${INTERFACE} 纳入 NetworkManager 管理 ---"
sudo nmcli device set "${INTERFACE}" managed yes

# 2. 新建/修改连接，只设地址和手动模式
# 注意：使用 con-name "${CONNECTION_NAME}" type ethernet ifname "${INTERFACE}" autoconnect yes 确保连接存在
# 如果连接已存在，则使用 modify
echo "--- 步骤 2: 新建或修改连接 ${CONNECTION_NAME} ---"

# 尝试删除旧连接（可选，用于确保全新配置）
# sudo nmcli connection delete "${CONNECTION_NAME}" 2>/dev/null

# 检查连接是否存在
if sudo nmcli connection show "${CONNECTION_NAME}" &>/dev/null; then
  echo "连接 ${CONNECTION_NAME} 已存在，正在修改..."
  sudo nmcli connection modify "${CONNECTION_NAME}" \
    ipv6.method manual \
    ipv6.addresses "${IPV6_ADDRESS_WITH_MASK}"
else
  echo "连接 ${CONNECTION_NAME} 不存在，正在新建..."
  sudo nmcli connection add type ethernet ifname "${INTERFACE}" con-name "${CONNECTION_NAME}" autoconnect yes \
    ipv6.method manual \
    ipv6.addresses "${IPV6_ADDRESS_WITH_MASK}"
fi


# 3. 立即生效
echo "--- 步骤 3: 激活连接 ${CONNECTION_NAME} ---"
sudo nmcli connection up "${CONNECTION_NAME}"

# 4. 确保接口处于“up”状态 (可选，nmcli connection up 通常会处理)
echo "--- 步骤 4: 确保接口 ${INTERFACE} 处于启动状态 ---"
sudo ifconfig "${INTERFACE}" up

echo "配置完成。请使用 'ip a show ${INTERFACE}' 或 'nmcli device show ${INTERFACE}' 检查配置。"