#!/bin/bash

# ==============================================================================
#  Universal OVS/DPDK Reset Script for Switches
#  This script will revert OVS and DPDK configurations to the system's default state.
# ==============================================================================

# 确保脚本以root权限运行
if [[ $EUID -ne 0 ]]; then
   echo "错误：此脚本必须使用sudo或以root用户身份运行" 
   exit 1
fi

echo "--- 开始还原交换机配置 ---"

# 假设DPDK的设备绑定脚本在当前目录
DPDK_DEV_BIND_TOOL="./dpdk-devbind.py"

if [ ! -f "$DPDK_DEV_BIND_TOOL" ]; then
    echo "错误：未在当前目录找到 '$DPDK_DEV_BIND_TOOL' 脚本。"
    echo "请将此脚本与 dpdk-devbind.py 放在同一目录下，或修改脚本中的路径。"
    exit 1
fi

# --- 步骤 1: 动态查找并清理 OVS 网桥 ---
# 获取本机上的所有OVS网桥名称
BRIDGE_NAMES=$(ovs-vsctl list-br)

if [ -z "$BRIDGE_NAMES" ]; then
    echo "未找到任何 OVS 网桥，跳过 OVS 清理步骤。"
else
    for br in $BRIDGE_NAMES; do
        echo "正在处理网桥: $br"

        echo "  -> 正在删除流表..."
        ovs-ofctl del-flows $br

        echo "  -> 正在断开控制器连接..."
        ovs-vsctl del-controller $br

        echo "  -> 正在删除所有端口..."
        for port in $(ovs-vsctl list-ports $br); do
            echo "     - 删除端口 $port"
            ovs-vsctl del-port $br $port
        done

        echo "  -> 正在删除网桥 $br..."
        ovs-vsctl del-br $br
    done
fi

# --- 步骤 2: 停止 OVS 服务 ---
echo "正在停止 OVS 服务..."
if systemctl is-active --quiet openvswitch-switch; then
    systemctl stop openvswitch-switch
elif ovs-ctl status | grep -q "is running"; then
    ovs-ctl stop
else
    echo "OVS 服务似乎未在运行。"
fi


# --- 步骤 3: 自动解绑 DPDK 网卡并恢复内核驱动 ---
echo "正在查找并恢复被 DPDK 绑定的网卡..."

# 查找所有使用 vfio-pci 驱动的设备，并恢复它们
# 使用 process substitution 来安全地逐行读取
while IFS= read -r line; do
    # 提取PCI地址 (第一个字段)
    pci_addr=$(echo "$line" | awk '{print $1}')
    
    # 检查是否存在'unused='字段来确定原始驱动
    if [[ "$line" == *unused=* ]]; then
        # 提取 'unused=' 后面的驱动名称
        kernel_driver=$(echo "$line" | sed -n 's/.*unused=\([^ ]*\).*/\1/p')
        
        if [ -n "$pci_addr" ] && [ -n "$kernel_driver" ]; then
            echo "  -> 正在将设备 $pci_addr 重新绑定回内核驱动 $kernel_driver..."
            $DPDK_DEV_BIND_TOOL -b "$kernel_driver" "$pci_addr"
        else
            echo "  -> 警告: 无法解析设备信息: $line"
        fi
    else
        # 如果没有'unused'信息，尝试通用解绑
        echo "  -> 警告: 未找到设备 $pci_addr 的原始驱动信息，尝试通用解绑..."
        $DPDK_DEV_BIND_TOOL -u "$pci_addr"
    fi
done < <($DPDK_DEV_BIND_TOOL --status | grep 'drv=vfio-pci')


# --- 步骤 4: 释放大页内存 ---
echo "正在释放大页内存 (HugePages)..."
sysctl -w vm.nr_hugepages=0

echo ""
echo "--- 还原完成 ---"
echo "所有 OVS 网桥和 DPDK 端口已被移除，网卡已恢复至内核控制。"
echo "您现在可以使用 'ip a' 或 'ifconfig' 命令来查看恢复后的网络接口。"
