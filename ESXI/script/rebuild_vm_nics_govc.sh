#!/bin/bash
# =========================================================
# 用途：通过 govc 重建 VM 网卡（保留 ethernet-0，删除其他后重新添加）
# 用法：./rebuild_vm_nics_govc.sh <区域名称> <虚拟机名称> <网络适配器1> [网络适配器2] ...
# 例如：./rebuild_vm_nics_govc.sh s05 s05-switchpc7 sw4-h2 VM-Net1
# 默认网卡类型：E1000
# =========================================================

# -------------------- 参数检查 --------------------
if [ $# -lt 3 ]; then
  echo "用法: $0 <区域名称> <虚拟机名称> <网络1> [网络2] ..."
  echo "示例: $0 s05 s05-switchpc7 sw4-h2"
  exit 1
fi

REGION="$1"
VM_NAME="$2"
shift 2
NETWORKS="$@"

# -------------------- 区域映射表 --------------------
declare -A REGION_IPS=(
  ["s02"]="10.112.122.157"
  ["s05"]="10.112.221.173"
  ["s06"]="10.112.198.103"
  ["s07"]="10.112.217.54"
  ["s09"]="10.112.59.241"
)

# 检查区域是否存在
if [[ -z "${REGION_IPS[$REGION]}" ]]; then
  echo "❌ 未知区域 '$REGION'，可用区域为: ${!REGION_IPS[@]}"
  exit 2
fi

# -------------------- 配置 govc 环境变量 --------------------
ESXI_IP="${REGION_IPS[$REGION]}"
export GOVC_URL="https://root:410@Bupt@${ESXI_IP}"
export GOVC_INSECURE=1

echo "🌐 已选择区域 '$REGION'，连接到 ESXi: ${ESXI_IP}"

# -------------------- 网卡类型 --------------------
NET_ADAPTER_TYPE="e1000"

# -------------------- 虚拟机检查 --------------------
govc vm.info "$VM_NAME" >/dev/null 2>&1
if [ $? -ne 0 ]; then
  echo "❌ 未找到虚拟机 '$VM_NAME'，请检查名称是否正确"
  exit 3
fi
echo "✅ 找到虚拟机 '$VM_NAME'"

# -------------------- 删除除 ethernet-0 外的网卡 --------------------
echo "🧹 删除除 ethernet-0 外的所有网卡..."
for dev in $(govc device.ls -vm "$VM_NAME" | grep ethernet | grep -v "ethernet-0"); do
  govc device.remove -vm "$VM_NAME" "$dev" >/dev/null 2>&1
  echo "🗑️ 删除设备 $dev"
done

# -------------------- 添加指定网络 --------------------
for NET in $NETWORKS; do
  # 检查 PortGroup 是否存在
  govc ls "/ha-datacenter/network/${NET}" >/dev/null 2>&1
  if [ $? -ne 0 ]; then
    echo "⚠️ PortGroup '$NET' 不存在，请先创建"
    continue
  fi

  echo "➕ 添加网络 '$NET' 到 '$VM_NAME' (类型: $NET_ADAPTER_TYPE)"
  govc vm.network.add -vm "$VM_NAME" -net.adapter "$NET_ADAPTER_TYPE" "$NET" >/dev/null 2>&1
  if [ $? -eq 0 ]; then
    echo "✅ 成功添加 '$NET'"
  else
    echo "❌ 添加 '$NET' 失败，请检查虚拟机电源状态或网络类型"
  fi
done

echo "🎯 操作完成"
