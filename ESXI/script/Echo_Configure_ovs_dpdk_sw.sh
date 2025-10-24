#!/bin/bash

# ==============================================================================
# OVS DPDK 交换机配置脚本 - 仅打印命令（不执行）
# 用法: ./test_ovs_dpdk_sw.sh <网桥名> <DPID(十进制)> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ...
# ==============================================================================

# 1. 参数初始化和验证
BRIDGE_NAME="$1"
DPID_DEC="$2"
CONTROLLER_IP="$3"
CONTROLLER_PORT="$4"

# 检查参数数量是否正确
if [ $# -lt 4 ] || [ $((($#-4) % 2)) -ne 0 ]; then
    # 错误信息只输出到标准错误，不影响命令输出
    echo "错误: 参数数量不足或格式错误。" >&2
    echo "用法: $0 <网桥名> <DPID(十进制)> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ..." >&2
    exit 1
fi

# 2. DPID 转换为 16 进制 (16位)
# 1 -> 0000000000000001
DPID_HEX=$(printf "%016x" "$DPID_DEC")

# 3. 辅助函数：将 ensXX 转换为 DPDK PCI ID (02:YY.0)
pci_id_from_ens() {
    local ENS_NAME="$1"
    local N=$(echo "$ENS_NAME" | sed 's/[^0-9]*//g')
    local DIFF=$((N - 32))
    # 确保 DIFF 在合理范围内，否则可能返回无效的 PCI ID
    if [ "$DIFF" -lt 1 ]; then
        return 1
    fi
    local HEX_SUFFIX=$(printf "%02x" "$DIFF")
    echo "02:$HEX_SUFFIX.0"
}

# --- 打印命令汇总 (不含注释) ---

# 准备阶段 reset网桥 & 开启ovs
echo "sudo ./reset_sw_config.sh"
echo "sudo ovs-ctl start"

# 激活 PCI 设备
echo "sudo ./setup_dpdk.sh"

# 创建 OVS 网桥
echo "sudo ovs-vsctl add-br $BRIDGE_NAME -- set bridge $BRIDGE_NAME datapath_type=netdev"

# 设置 datapath-id
echo "sudo ovs-vsctl set bridge $BRIDGE_NAME other-config:datapath-id=$DPID_HEX"

# 添加 DPDK 端口
shift 4 # 移除前4个参数
while [ "$#" -ge 2 ]; do
    EXT_PORT_NAME="$1"
    INT_NIC_NAME="$2"

    PCI_ID=$(pci_id_from_ens "$INT_NIC_NAME")
    
    # 检查 PCI_ID 转换是否成功
    if [ $? -eq 0 ]; then
        echo "sudo ovs-vsctl add-port $BRIDGE_NAME $EXT_PORT_NAME -- set Interface $EXT_PORT_NAME type=dpdk options:dpdk-devargs=$PCI_ID"
    fi

    shift 2 # 移除处理过的两个参数
done

# 连接控制器
echo "sudo ovs-vsctl set-controller $BRIDGE_NAME tcp:$CONTROLLER_IP:$CONTROLLER_PORT"

# 查看网络配置信息
echo "sudo ovs-vsctl show"
