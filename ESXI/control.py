import atexit
import ssl
import os
import sqlite3
import paramiko
from typing import Dict, Any
from pyVim import connect
from pyVmomi import vim
from esxi_config import ESXI_IP

# --- ESXi 连接信息 (请修改为你的实际信息) ---
ESXI_HOST = "10.112.221.173"
ESXI_USER = "root"
ESXI_PASS = "410@Bupt"
# 用于写入的外层索引，例如 's05'
ESXI_KEY = "s05"

def get_vm_by_name(content, vm_name):
    """根据名称查找虚拟机对象"""
    try:
        vm_container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True)
        for vm in vm_container.view:
            if vm.name == vm_name:
                return vm
    except Exception as e:
        print(f"查找虚拟机时出错: {e}")
    return None


def collect_esxi_inventory(content, host_key: str) -> dict:
    """遍历所有虚拟机，收集外部网卡名称 -> MAC -> IP。

    返回结构: { host_key: { vm_name: { nic_name: { 'mac':..., 'ips':[...]} } } }
    如果 VM 没有 VMware Tools 或无网络信息，会在结果中保留空结构。
    """
    inventory = {host_key: {}}
    try:
        vm_container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True)
        for vm in vm_container.view:
            vm_name = vm.name
            guest = getattr(vm, 'guest', None)
            if not guest or not getattr(guest, 'net', None):
                # 没有 VMware Tools 或无网络信息，仍然添加空结构
                inventory[host_key].setdefault(vm_name, {})
                continue

            nic_dict = {}
            for nic in guest.net:
                nic_name = nic.network or 'unknown'
                nic_dict[nic_name] = {
                    'mac': getattr(nic, 'macAddress', ''),
                    'ips': list(getattr(nic, 'ipAddress', []) or [])
                }
            inventory[host_key][vm_name] = nic_dict
        return inventory
    except Exception as e:
        print(f"收集 inventory 出错: {e}")
        return inventory


# ----------------- SQLite persistence helpers -----------------
DB_FILENAME = os.path.join(os.path.dirname(__file__), 'esxi_data.db')


def _get_db_conn():
    """返回 sqlite3 连接并确保数据库表已创建。"""
    conn = sqlite3.connect(DB_FILENAME)
    conn.row_factory = sqlite3.Row
    try:
        # enable foreign key cascade behavior
        conn.execute('PRAGMA foreign_keys = ON')
    except Exception:
        pass
    _ensure_db(conn)
    return conn


def _ensure_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # vm table stores vm rows; esxi_key is used to group by region
    cur.execute('''
    CREATE TABLE IF NOT EXISTS vm (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        esxi_key TEXT NOT NULL,
        name TEXT NOT NULL,
        vm_vmid TEXT,
        vm_moid TEXT,
        UNIQUE(esxi_key, name)
    )
    ''')
    # nic table stores external nic info per vm
    cur.execute('''
    CREATE TABLE IF NOT EXISTS nic (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vm_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        mac TEXT,
        source TEXT,
        FOREIGN KEY(vm_id) REFERENCES vm(id) ON DELETE CASCADE
    )
    ''')
    # nic_ip table stores ip addresses per nic
    cur.execute('''
    CREATE TABLE IF NOT EXISTS nic_ip (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nic_id INTEGER NOT NULL,
        ip TEXT NOT NULL,
        FOREIGN KEY(nic_id) REFERENCES nic(id) ON DELETE CASCADE,
        UNIQUE(nic_id, ip)
    )
    ''')
    # inner_nic table: store internal interface names discovered inside the VM
    cur.execute('''
    CREATE TABLE IF NOT EXISTS inner_nic (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nic_id INTEGER NOT NULL,
        mac TEXT,
        inner_name TEXT,
        FOREIGN KEY(nic_id) REFERENCES nic(id) ON DELETE CASCADE
    )
    ''')
    conn.commit()


# ----------------- 公共查询 API -----------------
# 下列函数提供查询 ESXi 相关信息的接口，并以中文注释说明行为


def get_esxi_servers_info() -> dict:
        """返回 ESXi 区域信息。

        返回格式:
            { esxi_key: { 'ip': 管理IP或None, 'in_db': True/False } }

        优先从 DB 中检测已有区域（vm 表），并与配置文件 `ESXI_IP` 合并。
        """
        info = {}
        db_keys = []
        try:
                conn = _get_db_conn()
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT esxi_key FROM vm")
                rows = cur.fetchall()
                db_keys = [r[0] for r in rows]
                conn.close()
        except Exception:
                db_keys = []

        # start with configured ESXI_IP entries
        for k, ip in ESXI_IP.items():
                info[k] = { 'ip': ip, 'in_db': k in db_keys }

        # include any db-only regions
        for k in db_keys:
                if k not in info:
                        info[k] = { 'ip': None, 'in_db': True }

        return info


def query_esxi_inventory(esxi_key: str) -> dict:
    """查询单个 ESXi 区域的 inventory。

    输入:
        esxi_key - 区域字符串（例如 's05'）

    返回:
        { vm_name: { nic_name: { 'mac': <mac>, 'ips': [ip,...] }, ... }, ... }

    行为:
        - 优先从 SQLite DB（vm, nic, nic_ip）中读取并重构结构。
        - 若 DB 无该区域数据，则返回空字典。
    """
    try:
        conn = _get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM vm WHERE esxi_key = ?", (esxi_key,))
        vm_rows = cur.fetchall()
        if not vm_rows:
            conn.close()
            # DB 中无该区域数据，返回空字典
            return {}

        result = {}
        for vm_row in vm_rows:
            vm_id = vm_row['id']
            vm_name = vm_row['name']
            result[vm_name] = {}
            cur.execute("SELECT id, name, mac FROM nic WHERE vm_id = ?", (vm_id,))
            nic_rows = cur.fetchall()
            for nic_row in nic_rows:
                nic_id = nic_row['id']
                nic_name = nic_row['name']
                mac = nic_row['mac']
                cur.execute("SELECT ip FROM nic_ip WHERE nic_id = ?", (nic_id,))
                ip_rows = cur.fetchall()
                ips = [r['ip'] for r in ip_rows]
                result[vm_name][nic_name] = { 'mac': mac, 'ips': ips }
        conn.close()
        return result
    except Exception:
        # DB 读取失败时返回空字典
        return {}

# ----------------- end Public API -----------------


def save_inventory_to_db(esxi_key: str, inventory_region: Dict[str, Any]) -> None:
    """将给定区域的 inventory 写入 DB（覆盖）。

    参数 inventory_region: mapping vm_name -> { nic_name: {'mac':..., 'ips':[...]} }
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        # Delete existing VMs for this esxi_key (cascade will remove nics and nic_ip)
        cur.execute("BEGIN")
        cur.execute("DELETE FROM vm WHERE esxi_key = ?", (esxi_key,))

        for vm_name, nic_map in inventory_region.items():
            cur.execute("INSERT INTO vm (esxi_key, name) VALUES (?, ?)", (esxi_key, vm_name))
            vm_id = cur.lastrowid
            if isinstance(nic_map, dict):
                for nic_name, nic_info in nic_map.items():
                    mac = nic_info.get('mac', '') if isinstance(nic_info, dict) else ''
                    cur.execute("INSERT INTO nic (vm_id, name, mac, source) VALUES (?, ?, ?, ?)",
                                (vm_id, nic_name, mac, 'guest'))
                    nic_id = cur.lastrowid
                    ips = []
                    if isinstance(nic_info, dict):
                        ips = nic_info.get('ips', []) or []
                    for ip in ips:
                        try:
                            cur.execute("INSERT OR IGNORE INTO nic_ip (nic_id, ip) VALUES (?, ?)", (nic_id, ip))
                        except Exception:
                            pass
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[DB] 保存 inventory 到 DB 出错: {e}")
    finally:
        conn.close()


def save_vmids_to_db(esxi_key: str, vmids_map: Dict[str, str]) -> None:
    """写入或更新指定区域的 VM ID / MOID 信息。

    参数 vmids_map: mapping vm_name -> vm_moid
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        for vm_name, vm_moid in vmids_map.items():
            # try update existing
            cur.execute("SELECT id FROM vm WHERE esxi_key = ? AND name = ?", (esxi_key, vm_name))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE vm SET vm_moid = ? WHERE id = ?", (str(vm_moid), r['id']))
            else:
                cur.execute("INSERT INTO vm (esxi_key, name, vm_moid) VALUES (?, ?, ?)", (esxi_key, vm_name, str(vm_moid)))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[DB] 保存 VMIDs 到 DB 出错: {e}")
    finally:
        conn.close()

# ----------------- end DB helpers -----------------


def cleanup_db_regions_not_in_esxi_ip() -> None:
    """删除 DB 中那些不在配置 `ESXI_IP` 中的 region（按 esxi_key）。

    用途：当配置发生变化（比如从配置中移除 s07）时，清理 DB 中遗留的 region 数据。
    """
    try:
        conn = _get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT esxi_key FROM vm")
        rows = [r[0] for r in cur.fetchall()]
        to_remove = [k for k in rows if k not in ESXI_IP]
        if to_remove:
            cur.execute('BEGIN')
            for k in to_remove:
                cur.execute("DELETE FROM vm WHERE esxi_key = ?", (k,))
            conn.commit()
            print(f"[DB] 移除不在 ESXI_IP 中的 region: {', '.join(to_remove)}")
        conn.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        print(f"[DB] 清理多余 region 时出错: {e}")



def _read_mynewconfig(cfg_path: str) -> dict:
    """读取 `esxi_config.py` 并返回其中的变量字典（会过滤 __dunder__ 项）。"""
    existing = {}
    if not os.path.exists(cfg_path):
        return existing
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            code = f.read()
            # execute into a temporary namespace and filter dunder keys
            temp_ns = {}
            exec(code, temp_ns)
            # copy only non-dunder keys to existing
            for k, v in temp_ns.items():
                if not (k.startswith('__') and k.endswith('__')):
                    existing[k] = v
    except Exception as e:
        print(f"读取 {cfg_path} 失败: {e}")
    return existing


def _write_mynewconfig(cfg_path: str, existing: dict) -> None:
    """将 existing 字典写回 `esxi_config.py`，保留重要变量顺序（尽量原子替换）。"""
    try:
        # write to a temp file then replace to be somewhat atomic
        tmp_path = cfg_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write('# Auto-generated by ESXICONTROL.py\n')
            # prefer ESXI_IP, VM_INFO, ESXI_VMIDS ordering if present
            ordered = []
            for key in ('ESXI_IP', 'VM_INFO', 'ESXI_VMIDS'):
                if key in existing:
                    ordered.append(key)
            # then append any other keys
            for key in existing:
                if key not in ordered and not (key.startswith('__') and key.endswith('__')):
                    ordered.append(key)
            for key in ordered:
                try:
                    f.write(key + ' = ' + repr(existing[key]) + '\n\n')
                except Exception:
                    pass
        # replace original file
        try:
            os.replace(tmp_path, cfg_path)
        except Exception:
            # fallback to rename
            os.remove(cfg_path) if os.path.exists(cfg_path) else None
            os.rename(tmp_path, cfg_path)
    except Exception as e:
        print(f"写入 {cfg_path} 失败: {e}")


def init_esxi_region(esxi_host: str, esxi_user: str, esxi_pass: str, esxi_key: str) -> dict:
    """初始化指定 ESXi 区域的 inventory 并写入 `esxi_config.py` 与 DB。

    参数:
      - esxi_host: ESXi 管理 IP 或主机名
      - esxi_user: API 用户
      - esxi_pass: API 密码
      - esxi_key: 外层索引（例如 's05'）

    函数行为: 连接 ESXi，收集所有 VM 的网卡信息（外部网卡名、MAC、IP），
    并将结果保存到配置文件和 SQLite DB 中。
    """
    service_instance = None
    try:
        ctx = ssl._create_unverified_context()
        service_instance = connect.SmartConnect(host=esxi_host, user=esxi_user, pwd=esxi_pass, sslContext=ctx)
        if not service_instance:
            raise RuntimeError(f"无法连接到 ESXi {esxi_host}")
        content = service_instance.RetrieveContent()
        inventory = collect_esxi_inventory(content, esxi_key)

        # Persist inventory to sqlite only. Do NOT write anything to esxi_config.py.
        try:
            save_inventory_to_db(esxi_key, inventory.get(esxi_key, {}))
        except Exception as e:
            print(f"[DB] save inventory failed: {e}")
        return inventory
    except Exception as e:
        print(f"init_esxi_region 出错: {e}")
        return {}
    finally:
        try:
            if service_instance:
                connect.Disconnect(service_instance)
        except Exception:
            pass


def record_vm_ids(esxi_host: str, esxi_user: str, esxi_pass: str, esxi_key: str) -> dict:
    """收集 VM 名称与 Managed Object ID 的映射并写入 `ESXI_VMIDS`（配置文件与 DB）。

    参数与 `init_esxi_region` 相同。返回该区域的映射字典。
    """
    service_instance = None
    vmids_map = {esxi_key: {}}
    try:
        ctx = ssl._create_unverified_context()
        service_instance = connect.SmartConnect(host=esxi_host, user=esxi_user, pwd=esxi_pass, sslContext=ctx)
        if not service_instance:
            raise RuntimeError(f"无法连接到 ESXi {esxi_host}")
        content = service_instance.RetrieveContent()
        vm_container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        for vm in vm_container.view:
            try:
                vm_name = vm.name
                vm_id = vm._GetMoId()
                vmids_map[esxi_key][vm_name] = vm_id
            except Exception:
                continue

        # Persist VMIDs to sqlite only. Do NOT write anything to esxi_config.py.
        try:
            save_vmids_to_db(esxi_key, vmids_map.get(esxi_key, {}))
        except Exception as e:
            print(f"[DB] save vmids failed: {e}")
        return vmids_map
    except Exception as e:
        print(f"record_vm_ids 出错: {e}")
        return vmids_map
    finally:
        try:
            if service_instance:
                connect.Disconnect(service_instance)
        except Exception:
            pass


def initialize_db_from_config() -> None:
    """第1部分：根据配置的 ESXi 列表初始化并刷新 SQLite DB。

    遍历 `ESXI_IP` 中的每个区域，调用 `init_esxi_region` 与 `record_vm_ids`。
    """
    # Remove any regions present in the DB but no longer configured in ESXI_IP
    cleanup_db_regions_not_in_esxi_ip()

    for esxi_key, esxi_host in ESXI_IP.items():
        print(f"\n>>> 初始化 region {esxi_key} ({esxi_host})")
        # 先删除该 region 在 DB 中的旧数据，确保后续写入为覆盖行为。
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute("DELETE FROM vm WHERE esxi_key = ?", (esxi_key,))
            conn.commit()
            conn.close()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            print(f"[DB] 在初始化前清除 region {esxi_key} 的旧数据时出错: {e}")
        try:
            inv = init_esxi_region(esxi_host, ESXI_USER, ESXI_PASS, esxi_key)
            print(f"[init] 收集到 {len(inv.get(esxi_key, {}))} 个 VM 条目")
        except Exception as e:
            print(f"[init] 初始化 {esxi_key} 失败: {e}")
        try:
            vmids = record_vm_ids(esxi_host, ESXI_USER, ESXI_PASS, esxi_key)
            print(f"[init] 记录 VMIDs: {len(vmids.get(esxi_key, {}))} 项")
        except Exception as e:
            print(f"[init] 记录 VMIDs 失败: {e}")


def read_db_and_print() -> None:
    """第2部分：从 DB 读取并打印 esxi_key、vm_name、nic_name、mac、ip。

    每行输出格式: 区域 | VM 名称 | 网卡名 | MAC | IP
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    # iterate regions
    cur.execute("SELECT DISTINCT esxi_key FROM vm")
    regions = [r[0] for r in cur.fetchall()]
    if not regions:
        print("[read] DB 中没有数据，请先运行初始化 (initialize_db_from_config)")
        conn.close()
        return

    for region in regions:
        print(f"\n=== region: {region} ===")
        cur.execute("SELECT id, name FROM vm WHERE esxi_key = ? ORDER BY name", (region,))
        vms = cur.fetchall()
        for vm in vms:
            vm_id = vm['id']
            vm_name = vm['name']
            cur.execute("SELECT id, name, mac FROM nic WHERE vm_id = ? ORDER BY name", (vm_id,))
            nics = cur.fetchall()
            if not nics:
                print(f"{region} | {vm_name} | (no nic)")
                continue
            for nic in nics:
                nic_id = nic['id']
                nic_name = nic['name']
                mac = nic['mac'] or ''
                cur.execute("SELECT ip FROM nic_ip WHERE nic_id = ? ORDER BY ip", (nic_id,))
                ips = [r[0] for r in cur.fetchall()]
                if not ips:
                    print(f"{region} | {vm_name} | {nic_name} | {mac} | ")
                else:
                    for ip in ips:
                        print(f"{region} | {vm_name} | {nic_name} | {mac} | {ip}")
    conn.close()


def collect_and_store_inner_ifaces_for_region(esxi_key: str, vm_user: str = 'switchpc1', vm_pwd: str = '1234567', timeout: int = 5) -> dict:
    """For a given region (esxi_key), SSH into each VM IP and discover internal iface name for each external MAC.

    Behavior:
      - Query DB for nic entries (mac) and their associated IPs per VM in the region.
      - Group by VM IP and SSH once per IP using provided credentials.
      - For each mac on that VM, run the provided ip/awk command and capture the interface name.
      - Write results into `inner_nic` table, replacing any existing entries for the affected nic_ids.

    Returns a dict summary: { 'region': esxi_key, 'checked': int, 'updated': int, 'failures': [ ... ] }
    Failures list contains tuples describing the failure: (reason, details...)
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT vm.id as vm_id, vm.name as vm_name, nic.id as nic_id, nic.mac as mac, nic.name as nic_name, nic_ip.ip as ip
    FROM vm
    JOIN nic ON nic.vm_id = vm.id
    LEFT JOIN nic_ip ON nic_ip.nic_id = nic.id
    WHERE vm.esxi_key = ? AND nic.mac IS NOT NULL AND TRIM(nic.mac) <> ''
    ORDER BY vm.id
    """, (esxi_key,))
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return {'region': esxi_key, 'checked': 0, 'updated': 0, 'failures': []}

    # group by VM (one SSH session per VM). Collect available IPs per VM and list of NICs (macs)
    vms = {}  # vm_id -> {'name': vm_name, 'ips': set(...), 'nics': [(nic_id, mac), ...]}
    nic_ids_set = set()
    for r in rows:
        nic_id = r['nic_id']
        mac = r['mac'] or ''
        ip = r['ip']
        vm_id = r['vm_id']
        vm_name = r['vm_name']
        nic_ids_set.add(nic_id)
        ent = vms.setdefault(vm_id, {'name': vm_name, 'ips': set(), 'nics': []})
        if ip:
            ent['ips'].add(ip)
        ent['nics'].append((nic_id, mac))

    # We'll collect successful results only. Preload mac->inner_name cache
    # from DB so we can skip probing MACs we've already discovered in previous runs.
    results = []  # tuples (nic_id, mac, inner_name)
    mac_cache = {}  # mac -> inner_name (None if known absent)
    try:
        cur.execute("SELECT DISTINCT mac, inner_name FROM inner_nic WHERE mac IS NOT NULL AND TRIM(mac) <> ''")
        for r in cur.fetchall():
            mac_cache[r['mac']] = r['inner_name']
    except Exception:
        pass

    # Mark VMs with no IP; we will not record failures for them, just skip
    # their NICs since we cannot probe internal interfaces.

    # 用于在已建立 SSH 连接上针对某个 mac 执行命令并返回内部网卡名
    def probe_mac_on_ssh(ssh_client: paramiko.SSHClient, mac: str) -> str:
        # command returns interface name such as 'eth0' or 'ens33'
        cmd = f"ip -o link | awk -v mac='{mac}' '$0~mac{{print substr($2,1,length($2)-1)}}'"
        try:
            stdin, stdout, stderr = ssh_client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode('utf-8', errors='ignore').strip()
            # choose first non-empty line
            for line in out.splitlines():
                line = line.strip()
                if line:
                    return line
        except Exception:
            pass
        return None

    # 遍历每台 VM，只建立一次 SSH 连接（为提高效率），优先选择 IPv4 地址
    for vm_id, info in vms.items():
        # skip VMs that had no IPs (already reported)
        if not info['ips']:
            continue
        # choose an IP: prefer IPv4 (no ':'), else pick any
        chosen_ip = None
        for candidate in info['ips']:
            if ':' not in candidate:
                chosen_ip = candidate
                break
        if not chosen_ip:
            chosen_ip = next(iter(info['ips']))

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(hostname=chosen_ip, username=vm_user, password=vm_pwd, timeout=timeout)
        except Exception:
            # SSH failed for this VM; skip probing its NICs (do not record failures per request)
            try:
                ssh.close()
            except Exception:
                pass
            continue

        try:
            for nic_id, mac in info['nics']:
                inner_name = None
                if mac:
                    # if we already discovered this MAC earlier, reuse it
                    if mac in mac_cache:
                        inner_name = mac_cache[mac]
                    else:
                        inner_name = probe_mac_on_ssh(ssh, mac)
                        mac_cache[mac] = inner_name
                # only persist successful discoveries (inner_name not None)
                if inner_name:
                    results.append((nic_id, mac, inner_name))
        finally:
            try:
                ssh.close()
            except Exception:
                pass

    # 持久化：删除这些 nic_id 对应的旧 inner_nic，然后插入新的记录
    try:
        if results:
            # Build mapping mac -> inner_name discovered in this run
            discovered = {}
            for nic_id, mac, inner_name in results:
                if mac and inner_name:
                    discovered[mac] = inner_name

            # For each discovered mac, find all nic_ids in this region that have that mac
            nic_ids_to_update = []
            for mac, inner_name in discovered.items():
                cur.execute("SELECT nic.id FROM nic JOIN vm ON nic.vm_id = vm.id WHERE vm.esxi_key = ? AND nic.mac = ?", (esxi_key, mac))
                rows2 = cur.fetchall()
                for r in rows2:
                    nic_ids_to_update.append((r['id'], mac, inner_name))

            if nic_ids_to_update:
                cur.execute('BEGIN')
                qmarks = ','.join(['?'] * len(nic_ids_to_update))
                # Delete existing inner_nic rows for these nic_ids
                nic_id_only = [nid for nid, _, _ in nic_ids_to_update]
                qmarks2 = ','.join(['?'] * len(nic_id_only))
                cur.execute(f"DELETE FROM inner_nic WHERE nic_id IN ({qmarks2})", tuple(nic_id_only))
                updated = 0
                for nic_id, mac, inner_name in nic_ids_to_update:
                    cur.execute("INSERT INTO inner_nic (nic_id, mac, inner_name) VALUES (?, ?, ?)", (nic_id, mac, inner_name))
                    updated += 1
                conn.commit()
            else:
                updated = 0
        else:
            updated = 0
    except Exception:
        conn.rollback()
        updated = 0
    finally:
        conn.close()

    # Do not return or record failures per user request; return only summary counts
    return {'region': esxi_key, 'checked': len(rows), 'updated': updated}


def collect_all_regions_inner_ifaces(vm_user: str = 'switchpc1', vm_pwd: str = '1234567', timeout: int = 5) -> dict:
    """遍历 DB 中所有区域并收集内部网卡名（已保留，可能不常用）。

    返回格式: { region: <per-region summary dict> }
    """
    regions = get_regions_from_db()
    def _summarize_failures(failures: list) -> list:
        """将重复的 failure 元组进行计数并返回摘要列表。

        返回格式: [ (count, failure_tuple), ... ]，按 count 降序排列。
        """
        agg = {}
        for f in failures:
            try:
                key = tuple(f)
            except Exception:
                key = (str(f),)
            agg[key] = agg.get(key, 0) + 1
        # 转换为 (count, failure) 并排序
        items = [(c, k) for k, c in agg.items()]
        items.sort(reverse=True, key=lambda x: x[0])
        return items

    overall = {}
    for region in regions:
        print(f"\n>>> 正在收集区域 {region} 的内部网卡信息")
        res = collect_and_store_inner_ifaces_for_region(region, vm_user=vm_user, vm_pwd=vm_pwd, timeout=timeout)
        overall[region] = res
        failures = res.get('failures') or []
        # Print concise summary and then print the detailed inventory lines
        print(f"[内部网卡] 区域={region} 已更新={res.get('updated')} 检查={res.get('checked')}")
        print_inventory_with_inner_nic(region)
    return overall


def print_inventory_with_inner_nic(esxi_key: str) -> None:
    """打印指定区域的最终 inventory，每行格式:

    s02 | switchpc1 | VM Network | 00:0c:29:16:f3:34 | 10.112.76.69 | 内部网卡名称。

    说明: 输出只包含已经在 DB 中存在的内网名称(inner_nic)。若某 MAC 没有 inner_name，则该行不打印。
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM vm WHERE esxi_key = ? ORDER BY name", (esxi_key,))
    vms = cur.fetchall()
    for vm in vms:
        vm_id = vm['id']
        vm_name = vm['name']
        cur.execute("SELECT id, name, mac FROM nic WHERE vm_id = ? ORDER BY name", (vm_id,))
        nics = cur.fetchall()
        for nic in nics:
            nic_id = nic['id']
            nic_name = nic['name']
            mac = (nic['mac'] or '').strip()
            if not mac:
                continue
            # get IPs for this nic
            cur.execute("SELECT ip FROM nic_ip WHERE nic_id = ? ORDER BY ip", (nic_id,))
            ips = [r[0] for r in cur.fetchall()]
            # inner nic
            cur.execute("SELECT inner_name FROM inner_nic WHERE nic_id = ? LIMIT 1", (nic_id,))
            row = cur.fetchone()
            if not row:
                continue
            inner_name = row['inner_name']
            # print one line per IP (if multiple ips exist)
            if not ips:
                # still print a line with empty IP field
                print(f"{esxi_key} | {vm_name} | {nic_name} | {mac} |  | {inner_name}.")
            else:
                for ip in ips:
                    print(f"{esxi_key} | {vm_name} | {nic_name} | {mac} | {ip} | {inner_name}.")
    conn.close()


def main():
    # Three-part main:
    # 1) 初始化数据库（可选；已注释掉，因为可能较慢）
    initialize_db_from_config()
    # 2)读取数据库并打印结构化信息
    read_db_and_print()
    # 3) 显式地迭代各个区域，并对每个区域进行探测，获取内部网卡信息。
    regions = get_regions_from_db()
    overall_failures = []

    for region in regions:
        print(f"\n>>> 开始探测区域 {region} 的内部网卡")
        res = collect_and_store_inner_ifaces_for_region(region)
        # print per-region summary immediately
        if res.get('failures'):
            print(f"[内部网卡] 区域={region} 已更新={res.get('updated')} 检查={res.get('checked')} 失败数={len(res.get('failures'))}")
            for f in res.get('failures'):
                overall_failures.append((region, f))
        else:
            print(f"[内部网卡] 区域={region} 已更新={res.get('updated')} 检查={res.get('checked')} 完成")

    if overall_failures:
        print('\n== 内部网卡采集失败清单 ==')
        for region, f in overall_failures:
            print(f"区域={region} 失败={f}")
    else:
        print('\n所有内部网卡探测完成（未发生 SSH 级别失败）。')

    # 



def get_regions_from_db():
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT esxi_key FROM vm")
            rows = cur.fetchall()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []
        
if __name__ == "__main__":
    main()