#!/bin/sh

# 检查参数数量
if [ "$#" -lt 2 ]; then
    echo "用法: $0 <虚拟机名称> <网络适配器名称1> [网络适配器名称2] ..."
    echo "示例: $0 MyVM 'VM Network' 'DMZ Network'"
    exit 1
fi

VM_NAME="$1"
shift
NEW_PORTGROUPS=("$@")

# 1. 查找虚拟机 Vmid
VMLIST=$(vim-cmd vmsvc/getallvms | grep -i "${VM_NAME}" | awk '{print $1}')

if [ -z "$VMLIST" ]; then
    echo "错误: 未找到名称包含 \"${VM_NAME}\" 的虚拟机。"
    exit 1
fi

VM_IDS=$(echo "$VMLIST" | wc -l)

if [ "$VM_IDS" -gt 1 ]; then
    echo "错误: 找到多个名称包含 \"${VM_NAME}\" 的虚拟机，请使用更精确的名称或 Vmid:"
    vim-cmd vmsvc/getallvms | grep -i "${VM_NAME}"
    exit 1
fi

VMID="$VMLIST"
echo "已找到虚拟机: ${VM_NAME} (Vmid: ${VMID})"

# 检查虚拟机是否已关闭电源，配置修改要求关机
VM_POWER_STATE=$(vim-cmd vmsvc/power.getstate ${VMID} | grep -i "Powered" | awk '{print $NF}')
if [ "$VM_POWER_STATE" != "off" ]; then
    echo "警告: 虚拟机当前电源状态为 ${VM_POWER_STATE}。请先关闭虚拟机电源后重试。"
    echo "您可以运行 'vim-cmd vmsvc/power.off ${VMID}' 强制关闭电源 (不推荐) 或通过正常途径关闭 guest OS。"
    exit 1
fi

echo "正在获取设备列表..."

# 2. 清除除了 'Network adapter 1' 之外的所有网络适配器
# 注意: 'Network adapter 1' 的 label 并不一定是 key=4000，需要动态查找
NETWORK_ADAPTER_KEYS=""
DEVICE_INFO=$(vim-cmd vmsvc/device.getdevices ${VMID})

# 使用 awk 提取网络适配器的 key 和 label
echo "${DEVICE_INFO}" | awk '
/^\(vim.vm.device.VirtualEthernetCard/ {
    # 找到一个网络适配器块的开始
    is_nic = 1
    nic_key = ""
    nic_label = ""
}
is_nic && /key =/ {
    # 提取 key
    nic_key = $3
    sub(/,/, "", nic_key)
}
is_nic && /label = "Network adapter/ {
    # 提取 label
    nic_label = $4
    sub(/^"/, "", nic_label)
    sub(/"/, "", nic_label)
}
is_nic && /summary =/ {
    # 网络适配器块结束，检查并记录 key
    if (nic_label != "" && nic_key != "") {
        # 记录所有网络适配器的 key，方便后续删除
        if (nic_label != "Network adapter 1") {
            # 记录除了 "Network adapter 1" 之外的 key
            print nic_key
        }
        is_nic = 0  # 块处理完毕
    }
}
' > /tmp/nics_to_remove_${VMID}.txt

NIC_KEYS_TO_REMOVE=$(cat /tmp/nics_to_remove_${VMID}.txt)
rm /tmp/nics_to_remove_${VMID}.txt

if [ -z "$NIC_KEYS_TO_REMOVE" ]; then
    echo "未找到除了 'Network adapter 1' 之外的网络适配器需要删除。"
else
    echo "准备删除以下 key 的网络适配器 (保留 'Network adapter 1'):"
    echo "$NIC_KEYS_TO_REMOVE" | while read KEY; do
        echo "正在删除 Key: ${KEY}..."
        # vim-cmd vmsvc/device.removedevice <vmid> <deviceKey>
        if vim-cmd vmsvc/device.removedevice ${VMID} ${KEY}; then
            echo "  Key ${KEY} 删除成功。"
        else
            echo "  错误: Key ${KEY} 删除失败。"
        fi
    done
fi

# 3. 挂载新的网络适配器 (E1000)
# vim-cmd vmsvc/device.network_add <vmid> <portgroup> <adapter_type> <wake_on_lan> <start_connected>
# adapter_type: e1000, vmxnet, vmxnet2, vmxnet3. 我们使用 e1000
# start_connected: 1 (连接)

echo "---"
echo "准备挂载新的 e1000 网络适配器..."

SUCCESS_COUNT=0
FAILURE_COUNT=0

for PG in "${NEW_PORTGROUPS[@]}"; do
    # 检查 portgroup 是否存在 (这是一个健壮性要求，但 vim-cmd 没有直接命令检查 portgroup)
    # 我们可以通过尝试添加来间接检查，如果失败则会提示找不到网络。
    
    echo "尝试添加连接到网络 \"${PG}\" 的 e1000 适配器..."
    if vim-cmd vmsvc/device.network_add ${VMID} "${PG}" e1000 0 1; then
        echo "  成功: 新的 e1000 适配器已挂载并连接到 \"${PG}\"。"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        echo "  错误: 挂载到 \"${PG}\" 失败。请检查该网络适配器名称 (Portgroup/vSwitch) 是否存在且可访问。"
        FAILURE_COUNT=$((FAILURE_COUNT + 1))
    fi
done

echo "---"
echo "操作摘要:"
echo "已处理虚拟机: ${VM_NAME} (Vmid: ${VMID})"
echo "保留了 'Network adapter 1' 并移除了其他旧网卡。"
echo "成功挂载新网卡数量: ${SUCCESS_COUNT}"
echo "挂载失败网卡数量: ${FAILURE_COUNT}"

# 清理
unset VM_NAME VMID NEW_PORTGROUPS VMLIST VM_IDS VM_POWER_STATE NIC_KEYS_TO_REMOVE DEVICE_INFO PG SUCCESS_COUNT FAILURE_COUNT KEY IS_NIC NIC_KEY NIC_LABEL

exit 0