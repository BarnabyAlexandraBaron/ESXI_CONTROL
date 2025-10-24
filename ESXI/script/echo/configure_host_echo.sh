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

# 1. 把 ens36 纳入 NetworkManager 管理（自动生成连接配置）
echo "sudo nmcli device set ${INTERFACE} managed yes"

# 2. 新建/修改连接，只设地址和手动模式
# 注意：这里我们使用 nmcli connection add 的形式，即使它通常用于新建。
# 在纯echo版本中，我们不判断连接是否存在。
echo "sudo nmcli connection add type ethernet ifname ${INTERFACE} con-name ${CONNECTION_NAME} autoconnect yes \\"
echo "  ipv6.method manual \\"
echo "  ipv6.addresses ${IPV6_ADDRESS_WITH_MASK}"

# 3. 立即生效
echo "sudo nmcli connection up ${CONNECTION_NAME}"

# 4. 确保接口处于“up”状态
echo "sudo ifconfig ${INTERFACE} up"