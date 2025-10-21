#!/bin/bash

# ==============================================================================
# 通用 OVS DPDK 交换机配置脚本
# 用法: ./configure_ovs_dpdk_sw.sh <网桥名> <DPID(十进制)> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ...
# 示例: ./configure_ovs_dpdk_sw.sh sw1 1 10.112.88.99 6633 h1-sw1 ens36 sw1-sw2 ens37 sw1-sw3 ens38
# ==============================================================================

# 1. 参数初始化和验证
BRIDGE_NAME="$1"
DPID_DEC="$2"
CONTROLLER_IP="$3"
CONTROLLER_PORT="$4"

if [ $# -lt 4 ] || [ $((($#-4) % 2)) -ne 0 ]; then
    echo "错误: 参数数量不足或格式错误。"
    echo "用法: $0 <网桥名> <DPID(十进制)> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ..."
    exit 1
fi

# 2. DPID 转换为 16 进制 (16位)
# printf "%016x" 用于将十进制转换为16位（8字节）的十六进制字符串
DPID_HEX=$(printf "%016x" "$DPID_DEC")

# 3. 辅助函数：将 ensXX 转换为 DPDK PCI ID (02:YY.0)
# 假设: ensN -> 02:(N-32).0 (十六进制表示)
# 示例: ens36 -> 36-32=4 -> 04 -> 02:04.0
pci_id_from_ens() {
    local ENS_NAME="$1"
    # 提取数字 N
    local N=$(echo "$ENS_NAME" | sed 's/[^0-9]*//g')
    if [ -z "$N" ]; then
        echo "错误: 无法从 $ENS_NAME 提取数字。" >&2
        return 1
    fi
    # 计算差值并转换为十六进制
    local DIFF=$((N - 32))
    if [ "$DIFF" -lt 1 ] || [ "$DIFF" -gt 32 ]; then # 假设合理范围
        echo "警告: $ENS_NAME 转换的 PCI ID 超出常规范围。" >&2
    fi
    # 将差值转换为两位十六进制，并拼接成完整的 PCI ID
    local HEX_SUFFIX=$(printf "%02x" "$DIFF")
    echo "02:$HEX_SUFFIX.0"
}

# 4. 激活 PCI (假设 setup_dpdk.sh 已准备好)
# 注意: setup_dpdk.sh 的内容需要根据实际绑定的PCI号来修改，这里只执行一次
# 实际操作中，此脚本应在执行之前先确认并修改 setup_dpdk.sh。
# 这里我们跳过对 setup_dpdk.sh 的内容修改，只执行激活命令。
echo "----0. 准备阶段 reset网桥 & 开启ovs"
sudo ./reset_sw_config.sh
sudo ovs-ctl start

echo "--- 1. 激活 PCI 设备 (假设 setup_dpdk.sh 已根据网卡P CI号修改并准备)"
sudo ./setup_dpdk.sh

# 5. 创建 OVS 网桥 (datapath_type=netdev for DPDK)
echo "--- 2. 创建 OVS 网桥 ---"
sudo ovs-vsctl add-br "$BRIDGE_NAME" -- set bridge "$BRIDGE_NAME" datapath_type=netdev

# 6. 设置 datapath-id (dpid)
echo "--- 3. 设置 datapath-id ---"
sudo ovs-vsctl set bridge "$BRIDGE_NAME" other-config:datapath-id="$DPID_HEX"

# 7. 添加 DPDK 端口 (从第五个参数开始，每两个为一组)
echo "--- 4. 添加 DPDK 端口 ---"
shift 4 # 移除前4个参数 (网桥名, DPID, IP, Port)
while [ "$#" -ge 2 ]; do
    EXT_PORT_NAME="$1"
    INT_NIC_NAME="$2"

    # 获取 PCI ID
    PCI_ID=$(pci_id_from_ens "$INT_NIC_NAME")
    if [ $? -ne 0 ]; then
        echo "跳过端口 $EXT_PORT_NAME/$INT_NIC_NAME"
    else
        echo "端口: $EXT_PORT_NAME ($INT_NIC_NAME -> $PCI_ID)"
        sudo ovs-vsctl add-port "$BRIDGE_NAME" "$EXT_PORT_NAME" \
            -- set Interface "$EXT_PORT_NAME" type=dpdk options:dpdk-devargs="$PCI_ID"
    fi

    shift 2 # 移除处理过的两个参数
done

# 8. 连接控制器
echo "--- 5. 连接控制器 ---"
sudo ovs-vsctl set-controller "$BRIDGE_NAME" tcp:"$CONTROLLER_IP":"$CONTROLLER_PORT"

# 9. 查看网络配置信息
echo "--- 6. 查看网络配置 ---"
sudo ovs-vsctl show

echo "OVS DPDK 交换机 $BRIDGE_NAME 配置完成。"