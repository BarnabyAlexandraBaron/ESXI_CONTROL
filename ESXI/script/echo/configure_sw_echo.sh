#!/bin/bash

# ==============================================================================
# OVS DPDK 交换机配置脚本 (优化版，从网桥名提取DPID) - 仅打印命令（不执行）
# 用法: ./Configure_SW.sh <网桥名> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ...
# 示例: ./Configure_SW.sh sw1 10.112.88.99 6633 h1-sw1 ens36 sw1-sw2 ens37 sw1-sw3 ens38
# ==============================================================================

# 1. 参数初始化和验证
BRIDGE_NAME="$1"
CONTROLLER_IP="$2"
CONTROLLER_PORT="$3"

# 检查参数数量是否正确
# 预期参数数量: 1 (网桥) + 2 (控制器信息) + N*2 (端口对)
if [ $# -lt 3 ] || [ $((($#-3) % 2)) -ne 0 ]; then
	    echo "错误: 参数数量不足或格式错误。" >&2
	        echo "用法: $0 <网桥名> <控制器IP> <控制器端口> <外部端口名1> <内部网卡名1> ..." >&2
		    exit 1
	    fi

	    # 2. 从网桥名提取 DPID (十进制)
	    # 提取 'sw' 后面的数字。示例: sw1 -> 1
	    DPID_DEC=$(echo "$BRIDGE_NAME" | sed 's/[^0-9]*//g')

	    if [ -z "$DPID_DEC" ]; then
		        echo "错误: 无法从网桥名 '$BRIDGE_NAME' 中提取数字作为 DPID。" >&2
			    exit 1
		    fi

		    # 3. DPID 转换为 16 进制 (16位)
		    DPID_HEX=$(printf "%016x" "$DPID_DEC")

		    # 4. 辅助函数：将 ensXX 转换为 DPDK PCI ID (02:YY.0)
		    # 假设: ensN -> 02:(N-32).0 (十六进制表示)
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

								    # 设置 datapath-id (使用提取的 $DPID_HEX)
								    echo "sudo ovs-vsctl set bridge $BRIDGE_NAME other-config:datapath-id=$DPID_HEX"

								    # 添加 DPDK 端口
								    shift 3 # 移除前3个参数 (网桥名, IP, Port)
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
