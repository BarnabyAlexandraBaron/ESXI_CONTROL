#!/usr/bin/env python3
"""
只建一次 SSH 连接给 ESXi，取完 IP+MAC 后再连目标 VM。
usage:
    python get_port_name.py
"""
import re
import sys
from typing import Tuple, List

import paramiko
import os
from vm_config import *

# ---------------- 配置 ---------------- #

# 全局维护写回 vm_config.py 时要保留的配置项顺序
GLOBAL_CONFIG_KEYS = (
    "ESXI_IP",
    "VM_ID",
    "VM_IP",
    "VM_EXTERNAL_DEVICE",# h1-sw1
    "ESXI_USER",
    "ESXI_PWD",
    "VM_USER",
    "VM_PWD",
    "BASIC_COMMAND",
    "VM_INER_DEVICE",# ens36
    "VM_MAC",
    "VM_INFO", # 从 serverconfig.py 复制过来
)

# ------------------------------------- #

def run_cmds(ip: str, cmds: List[str], user: str, pwd: str) -> List[str]:
    """顺序执行命令并返回输出列表"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, username=user, password=pwd, timeout=10)
        results = []
        for cmd in cmds:
            stdin, stdout, stderr = ssh.exec_command(cmd)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            print(f"[+] 正在执行{cmd}")
            results.append(out if not err else f"STDERR:\n{err}")
        return results
    except Exception as e:
        return [f"SSH 失败: {e}"] * len(cmds)
    finally:
        ssh.close()

def stage_one_get_ip_mac(ip: str,target_vm_id: str,target_port_name: str) -> Tuple[str, str]:
    """一次 SSH 登录 ESXi，返回 (vm_ip, mac)"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, username=ESXI_USER, password=ESXI_PWD, timeout=10)
        # 两条命令一次执行完
        cmd = (
            f"sh get_ip_sin.sh {target_vm_id} 2>/dev/null | grep 'ID: {target_vm_id}' ; "
            f'sh get_mac_by_linenum.sh {target_vm_id} "{target_port_name}"'
        )
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out_lines = [line.strip() for line in stdout if line.strip()]
        err = stderr.read().decode().strip()
        if err:
            raise RuntimeError(err)

        # 解析 IP，支持类似: "VM: s05-switchpc1 (ID: 26) -> IP: 10.112.138.215"
        ip_match = re.search(rf"\(ID:\s*{target_vm_id}\).*?IP:\s*([0-9.]+)", out_lines[0])
        if not ip_match:
            raise RuntimeError(f"未找到 ID={target_vm_id} 的 IP")
        vm_ip = ip_match.group(1)

        # 解析 MAC（假设第二行就是纯 MAC）
        mac = out_lines[1]
        return vm_ip, mac
    finally:
        ssh.close()


def stage_one_get_device(ip: str, mac: str) -> str:
    """一次 SSH 登录目标 VM，根据 MAC 取接口名"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ip, username=VM_USER, password=VM_PWD, timeout=10)
        cmd = f"ip -o link | awk -v mac='{mac}' '$0~mac{{print substr($2,1,length($2)-1)}}'" #根据mac获取 内部网卡名称 GET_INNER_IFACE_NAME
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if err:
            raise RuntimeError(err)
        return out
    finally:
        ssh.close()


def stage_one_set_hostip(ip: str, iface: str, host_ipv6: str) -> List[str]:
    """
    登录目标 VM，给指定接口配置静态 IPv6 地址
    :param ip:        目标 VM 的 IPv4 地址（管理口）
    :param iface:     网卡设备名（如 ens3）
    :param host_ipv6: 期望设置的 IPv6 地址（可带 /长度 或不带，默认补 /64）
    :return:          每条命令的输出列表
    """
    # 如果用户没写掩码，自动补 /64
    if "/" not in host_ipv6:
        host_ipv6 += "/64"

    commands = [
        f"sudo nmcli device set {iface} managed yes",
        f"sudo nmcli con show {iface} &>/dev/null && sudo nmcli con mod {iface} ipv6.method manual ipv6.addresses {host_ipv6} || sudo nmcli con add type ethernet ifname {iface} con-name {iface} autoconnect yes ipv6.method manual ipv6.addresses {host_ipv6}",
        f"sudo nmcli con down {iface}",
        f"sudo nmcli con up {iface}"
    ]

    print(f"[+] 在 {ip} 上为 {iface} 设置 IPv6: {host_ipv6}")
    results = run_cmds(ip, commands, VM_USER, VM_PWD)
    for cmd, out in zip(commands, results):
        print(f"[*] 执行: {cmd}\n{out}\n")
    return results
    

def Stage_One(stage_ip: str,target_vm_id: str,target_port_name: str ,host_ipv6:str):
    """"阶段一：根据 ESXI区域 和 对应host的虚拟机ID 和 对应host的端口名称 取设备名称"""
    vm_ip, mac = stage_one_get_ip_mac(stage_ip,target_vm_id,target_port_name)
    print(f"VM IP: {vm_ip}, MAC: {mac}")
    iface = stage_one_get_device(vm_ip, mac)
    print("接口名:", iface)
     # 举例：给该接口配 IPv6
    stage_one_set_hostip(vm_ip, iface, host_ipv6)



def Stage_Init_VMID(esxi_ip: str) -> dict:
    """登录 ESXi，运行 `vim-cmd vmsvc/getallvms`，解析 vm name 与 vmid。
    将结果写入同目录下的 `vm_config.py` 中的 `VM_ID` 变量。

    :param esxi_ip: ESXi 管理 IP（例如 ESXI_IP['s05']）
    :return: 解析出的 mapping 字典（name->vmid）
    """
    # 先找到哪个区域对应这个 IP
    host_key = None
    for k, v in ESXI_IP.items():
        if v == esxi_ip:
            host_key = k
            break
    if host_key is None:
        raise ValueError(f"未能在 ESXI_IP 中找到 IP: {esxi_ip}")

    # 使用 run_cmds 执行 ESXi 命令，避免手动管理 SSH 连接
    res = run_cmds(esxi_ip, [BASIC_COMMAND["获取所有虚拟机ID"]], ESXI_USER, ESXI_PWD)
    if not res:
        raise RuntimeError("未获取到命令输出")
    output = res[0]
    if isinstance(output, str) and (output.startswith("STDERR:") or output.startswith("SSH 失败")):
        raise RuntimeError(output)
    out_lines = [line.rstrip() for line in output.splitlines()]

    mapping = {}
    for line in out_lines:
        line = line.strip()
        if not line:
            continue
        # 跳过非以数字开头的行（表头、提示等）
        if not re.match(r"^\d+", line):
            continue
        parts = line.split()
        # parts: [vmid, Name, File, ...]，Name 可能包含无空格，所以第二列应为 name
        if len(parts) < 2:
            continue
        vmid = parts[0]
        name = parts[1]
        mapping[name] = vmid

    # 读取现有 vm_config.py（如果存在），只更新 VM_ID 对应主机的部分，避免清空其它主机的数据
    cfg_path = os.path.join(os.path.dirname(__file__), "vm_config.py")
    existing = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            code = f.read()
            exec(code, existing)
    except Exception:
        existing = {}

    vm_id_table = existing.get("VM_ID", {k: {} for k in ESXI_IP.keys()})
    vm_id_table[host_key] = mapping
    existing["VM_ID"] = vm_id_table

    # 写回文件，保留已有的其它配置项（使用 GLOBAL_CONFIG_KEYS 保持一致）
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("# Auto-generated by CONTROL.py Stage_Init_VMID\n")
        for key in GLOBAL_CONFIG_KEYS:
            if key in existing:
                f.write(key + " = " + repr(existing[key]) + "\n\n")

    print(f"[+] 写入 {cfg_path}，主机 {host_key} 共 {len(mapping)} 个 VM 条目")
    return mapping
    # 结束 Stage_Init_VMID

def stage_two_bind_pci():

    pass

def Stage_Init_VMIP(esxi_ip: str) -> dict:
    """初始化 VM IP：
    - 从 `config.VM_ID` 读取对应 ESXi 主机的 name->vmid 映射（info_name_id）
    - 遍历 vmid 列表，在 ESXi 上执行 `sh get_ip_sin.sh {vmid} 2>/dev/null | grep 'ID: {vmid}'` 获取输出
    - 从输出中正则提取 IPv4（样例："VM: name (ID: 26) -> IP: 10.112.138.215"）
    - 构造 vmid->ip（若未找到则为空字符串），再根据 info_name_id 生成 name->ip
    - 将结果写回 `vm_config.py` 的 `VM_IP[host_key]`

    返回 name->ip 字典
    """
    # 找到 host_key
    host_key = None
    for k, v in ESXI_IP.items():
        if v == esxi_ip:
            host_key = k
            break
    if host_key is None:
        raise ValueError(f"未能在 ESXI_IP 中找到 IP: {esxi_ip}")

    info_name_id = VM_ID.get(host_key, {})
    if not info_name_id:
        print(f"[!] VM_ID 中没有找到主机 {host_key} 的条目，跳过")
        return {}

    # 一次性构造所有命令并发到 ESXi，避免为每个 vmid 建立独立 SSH 连接
    cmds = [f"sh get_ip_sin.sh {vmid} 2>/dev/null | grep 'ID: {vmid}'" for _, vmid in info_name_id.items()]
    results = run_cmds(esxi_ip, cmds, ESXI_USER, ESXI_PWD)

    vmid_ip = {}
    # results 与 info_name_id 项的顺序对应
    for (name, vmid), out in zip(info_name_id.items(), results):
        if not out or (isinstance(out, str) and (out.startswith("STDERR:") or out.startswith("SSH 失败"))):
            if out:
                print(f"[!] 获取 VM {vmid}({name}) IP 时出错: {out}")
            vmid_ip[vmid] = ""
            continue
        # 解析示例输出: VM: s05-switchpc1 (ID: 26) -> IP: 10.112.138.215
        m = re.search(rf"\(ID:\s*{vmid}\).*?IP:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out, re.S)
        if m:
            vmid_ip[vmid] = m.group(1)
        else:
            vmid_ip[vmid] = ""

    # 构造 name->ip
    name_ip = {name: vmid_ip.get(vmid, "") for name, vmid in info_name_id.items()}

    # 写回 vm_config.py 的 VM_IP（一次性写入，保留其它配置）
    cfg_path = os.path.join(os.path.dirname(__file__), "vm_config.py")
    existing = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            code = f.read()
            exec(code, existing)
    except Exception:
        existing = {}
    vm_ip_table = existing.get("VM_IP", {k: {} for k in ESXI_IP.keys()})
    vm_ip_table[host_key] = name_ip
    existing["VM_IP"] = vm_ip_table
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("# Auto-generated by CONTROL.py Stage_Init_VMIP\n")
        for key in GLOBAL_CONFIG_KEYS:
            if key in existing:
                f.write(key + " = " + repr(existing[key]) + "\n\n")

    print(f"[+] 写入 VM_IP 到 {cfg_path}，主机 {host_key} 共 {len(name_ip)} 个 IP 条目")
    return name_ip
    # 结束 Stage_Init_VMIP


def Stage_Init_INNERDEVICE(region: str):
    """为指定区域收集内部网卡名称并写入 VM_INER_DEVICE。

    步骤：
    - 从 `VM_IP[region]` 收集所有非空的管理 IP 列表 vm_ip_list
    - 在 `VM_INFO[region]` 中查找每个 vm_ip 所属的虚拟机及匹配该 ip 的外部网卡（控制网卡）
    - 对该虚拟机收集除控制网卡外的所有外部网卡名称与 MAC 列表
    - 得到三元组列表 (vm_ip, external_mac_list, external_name_list)
    - 对每个三元组，使用 `stage_one_get_device(vm_ip, mac)` 获取内部网卡名，构造四元组
      (vm_ip, external_mac_list, external_name_list, inner_iface_list)
    - 将四元组列表中的 vm_ip 替换为虚拟机 name（根据 VM_IP 的映射），并写入
      `vm_config.py` 的 `VM_INER_DEVICE[region]`。
    """
    if region not in ESXI_IP:
        raise ValueError(f"未知区域: {region}")

    # 从 VM_IP 中取出该区域的 name->ip 映射，得到 ip 列表
    name_ip_map = VM_IP.get(region, {})
    vm_ip_list = [ip for ip in name_ip_map.values() if ip]

    info_region = VM_INFO.get(region, {})

    ip_mac_exname = []  # list of (vm_ip, [macs], [ext_names])
    for vm_ip in vm_ip_list:
        found = False
        # 遍历该区域的所有 VM，找出包含 vm_ip 的网卡（控制网卡）
        for vm_name, nic_map in info_region.items():
            if not isinstance(nic_map, dict) or not nic_map:
                continue
            control_nics = []
            for nic_name, nic_info in nic_map.items():
                ips = nic_info.get('ips', []) if isinstance(nic_info, dict) else []
                if vm_ip in ips:
                    control_nics.append(nic_name)
            if control_nics:
                # 收集除控制网卡外的外部网卡名称与 mac
                ext_names = []
                ext_macs = []
                for nic_name, nic_info in nic_map.items():
                    if nic_name in control_nics:
                        continue
                    if isinstance(nic_info, dict):
                        mac = nic_info.get('mac', '')
                        ext_names.append(nic_name)
                        ext_macs.append(mac)
                ip_mac_exname.append((vm_ip, ext_macs, ext_names))
                found = True
                break
        if not found:
            print(f"[!] 区域 {region} 中未在 VM_INFO 找到 IP {vm_ip} 对应的 VM 条目")

    # 现在对每个三元组调用 stage_one_get_device 获取内部网卡名
    quad_list = []  # list of (vm_ip, ext_macs, ext_names, inner_ifaces)
    for vm_ip, ext_macs, ext_names in ip_mac_exname:
        inner_ifaces = []
        for mac in ext_macs:
            try:
                iface = stage_one_get_device(vm_ip, mac)
            except Exception as e:
                print(f"[!] 在 {vm_ip} 上根据 MAC {mac} 获取内部网卡失败: {e}")
                iface = ""
            if iface:
                # stage_one_get_device 可能返回多行/多个接口，用逗号分割
                parts = [p.strip() for p in iface.split() if p.strip()]
                inner_ifaces.extend(parts)
        quad_list.append((vm_ip, ext_macs, ext_names, inner_ifaces))

    # 将 vm_ip 替换为 vm name（根据 name_ip_map）
    ip_to_name = {ip: name for name, ip in name_ip_map.items()}
    quad_named = []
    for vm_ip, ext_macs, ext_names, inner_ifaces in quad_list:
        vm_name = ip_to_name.get(vm_ip, vm_ip)
        quad_named.append((vm_name, ext_macs, ext_names, inner_ifaces))

    # 写回 vm_config.py 的 VM_INER_DEVICE
    cfg_path = os.path.join(os.path.dirname(__file__), "vm_config.py")
    existing = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            code = f.read()
            exec(code, existing)
    except Exception:
        existing = {}

    vm_iner_table = existing.get("VM_INER_DEVICE", {k: [] for k in ESXI_IP.keys()})
    vm_iner_table[region] = quad_named
    existing["VM_INER_DEVICE"] = vm_iner_table

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("# Auto-generated by CONTROL.py Stage_Init_INNERDEVICE\n")
        for key in GLOBAL_CONFIG_KEYS:
            if key in existing:
                f.write(key + " = " + repr(existing[key]) + "\n\n")

    print(f"[+] 写入 VM_INER_DEVICE 到 {cfg_path}，区域 {region} 共 {len(quad_named)} 条记录")
    return quad_named
    # 结束 Stage_Init_INNERDEVICE

def Stage_Two():
    pass


def print_vm_iner_device(region: str) -> None:
    """Print VM_INER_DEVICE for a region as: pcname -> mac - external_name - internal_name"""
    try:
        vm_iner = VM_INER_DEVICE.get(region, [])
    except Exception:
        print(f"[!] VM_INER_DEVICE not defined or missing region {region}")
        return

    if not vm_iner:
        print(f"[!] VM_INER_DEVICE[{region}] is empty")
        return

    for entry in vm_iner:
        # entry: (vm_name, ext_macs, ext_names, inner_ifaces)
        if not isinstance(entry, (list, tuple)) or len(entry) < 4:
            continue
        vm_name, ext_macs, ext_names, inner_ifaces = entry
        print(f"=== {vm_name} ===")
        if not ext_macs:
            print("(no external NICs)")
            print("")
            continue
        # Print lines: mac - external_name - internal_name
        for i, mac in enumerate(ext_macs):
            ext_name = ext_names[i] if i < len(ext_names) else ""
            # Prefer matching internal iface by index; if counts mismatch, join all inner_ifaces
            if i < len(inner_ifaces):
                inner = inner_ifaces[i]
            else:
                inner = ",".join(inner_ifaces) if inner_ifaces else ""
            print(f"{mac} - {ext_name} - {inner}")
        print("")

def main() -> None:
    # Stage_One(ESXI_IP["s05"],TARGET_VM_ID,TARGET_PORT_NAME,"2001:db8:1::1234/64")
    # Stage_One(ESXI_IP["s05"],"26","h1-sw1","2001:db8:1::1/64")
    # print(VM_ID["s05"]["h2"])
    # print(VM_EXTERNAL_DEVICE["s05"]["h2"])
    # Stage_One(ESXI_IP["s05"],"29","h2-sw2","2001:db8:2::1/64")
    # 第一阶段 设置s05的h2机器的拓扑中的网卡的IP地址为2001:db8:2::1/64
    # 示例：先初始化并写入 vm_config.py，然后再使用 Stage_One
    # Stage_Init_VMID(ESXI_IP['s05'])  # 运行后会在脚本目录生成/更新 vm_config.py 的 VM_ID
    # Stage_One(ESXI_IP["s05"],VM_ID["s05"]["s05-switchpc1"],VM_EXTERNAL_DEVICE["s05"]["s05-switchpc1"][0],"2001:db8:1::1/64")

    # [+]获取某个区域所有的虚拟机ID，并写入 vm_config.py 的 VM_ID
    mapping = Stage_Init_VMID(ESXI_IP['s05'])
    print(mapping)


    # [+]初始化某个区域所有虚拟机的IP，并写入 vm_config.py 的 VM_IP 控制的IP地址
    ipmap = Stage_Init_VMIP(ESXI_IP['s05'])
    print(ipmap)

    # [+]初始化某个区域所有虚拟机的内部网卡名称，保存到 vm_config.py 的 VM_INER_DEVICE 
    Stage_Init_INNERDEVICE('s05')
    # 打印结果
    # print_vm_iner_device('s05')
        
    pass

if __name__ == "__main__":
    main()