#!/bin/bash

# ==============================================================================
# 通用 OVS DPDK 交换机配置脚本 (可执行版，从网桥名提取DPID)
# 用法: ./configure_ovs_dpdk_sw.sh <网桥名> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ...
# 示例: ./configure_ovs_dpdk_sw.sh sw1 10.112.88.99 6633 h1-sw1 ens36 sw1-sw2 ens37 sw1-sw3 ens38
# ==============================================================================

# 1. 参数初始化和验证
BRIDGE_NAME="$1"
CONTROLLER_IP="$2"
CONTROLLER_PORT="$3"

# 检查参数数量是否正确
# 预期参数数量: 1 (网桥) + 2 (控制器信息) + N*2 (端口对)
if [ $# -lt 3 ] || [ $((($#-3) % 2)) -ne 0 ]; then
    echo "错误: 参数数量不足或格式错误。"
    echo "用法: $0 <网桥名> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ..."
    exit 1
fi

# 2. 从网桥名提取 DPID (十进制)
DPID_DEC=$(echo "$BRIDGE_NAME" | sed 's/[^0-9]*//g')

if [ -z "$DPID_DEC" ]; then
    echo "错误: 无法从网桥名 '$BRIDGE_NAME' 中提取数字作为 DPID。"
    exit 1
fi

# 3. DPID 转换为 16 进制 (16位)
DPID_HEX=$(printf "%016x" "$DPID_DEC")
echo "提取的DPID (十进制): $DPID_DEC -> (十六进制): $DPID_HEX"

# 4. 辅助函数：将 ensXX 转换为 DPDK PCI ID (02:YY.0)
pci_id_from_ens() {
    local ENS_NAME="$1"
    local N=$(echo "$ENS_NAME" | sed 's/[^0-9]*//g')
    local DIFF=$((N - 32))
    if [ "$DIFF" -lt 1 ]; then
        return 1
    fi
    local HEX_SUFFIX=$(printf "%02x" "$DIFF")
    echo "02:$HEX_SUFFIX.0"
}

# --- 实际命令执行开始 ---

echo "----0. 准备阶段: 重置并启动 OVS ---"
# 确保权限，并在脚本中执行
sudo ./reset_sw_config.sh || { echo "错误: 执行 reset_sw_config.sh 失败。"; exit 1; }
sudo ovs-ctl start || { echo "错误: 启动 ovs-ctl 失败。"; exit 1; }

echo "--- 1. 激活 PCI 设备 (通过 setup_dpdk.sh) ---"
sudo ./setup_dpdk.sh || { echo "错误: 激活 PCI 设备失败。"; exit 1; }

echo "--- 2. 创建 OVS DPDK 网桥: $BRIDGE_NAME ---"
# 使用 --may-exist 避免重复创建时报错 (可选，取决于 reset_sw_config.sh 的行为)
sudo ovs-vsctl add-br "$BRIDGE_NAME" -- set bridge "$BRIDGE_NAME" datapath_type=netdev

echo "--- 3. 设置 datapath-id: $DPID_HEX ---"
sudo ovs-vsctl set bridge "$BRIDGE_NAME" other-config:datapath-id="$DPID_HEX"

echo "--- 4. 添加 DPDK 端口 ---"
shift 3 # 移除前3个参数 (网桥名, IP, Port)
# 遍历端口对
while [ "$#" -ge 2 ]; do
    EXT_PORT_NAME="$1"
    INT_NIC_NAME="$2"

    PCI_ID=$(pci_id_from_ens "$INT_NIC_NAME")
    
    if [ $? -eq 0 ]; then
        echo "添加端口: $EXT_PORT_NAME ($INT_NIC_NAME -> $PCI_ID)"
        sudo ovs-vsctl add-port "$BRIDGE_NAME" "$EXT_PORT_NAME" \
            -- set Interface "$EXT_PORT_NAME" type=dpdk options:dpdk-devargs="$PCI_ID"
    else
        echo "警告: 无法为 $INT_NIC_NAME 计算出有效的 PCI ID，跳过此端口。"
    fi

    shift 2
done

echo "--- 5. 连接控制器: $CONTROLLER_IP:$CONTROLLER_PORT ---"
sudo ovs-vsctl set-controller "$BRIDGE_NAME" tcp:"$CONTROLLER_IP":"$CONTROLLER_PORT"

echo "--- 6. 查看网络配置 ---"
sudo ovs-vsctl show

echo "OVS DPDK 交换机 $BRIDGE_NAME 配置完成。"