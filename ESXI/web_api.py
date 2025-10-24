from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import sqlite3
import json
import traceback

try:
    import paramiko
except Exception:
    paramiko = None

app = Flask(__name__, static_folder='../web_ui', static_url_path='')
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), 'esxi_data.db')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = ON')
    except Exception:
        pass
    return conn


@app.route('/api/servers')
def api_servers():
    # Return configured ESXI_IP from esxi_config.py plus whether in DB
    try:
        from esxi_config import ESXI_IP
    except Exception:
        ESXI_IP = {}

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT esxi_key FROM vm")
    db_keys = {r['esxi_key'] for r in cur.fetchall()}
    conn.close()

    out = []
    for k, ip in ESXI_IP.items():
        out.append({
            'key': k,
            'ip': ip,
            'in_db': k in db_keys
        })
    # include any db-only regions
    for k in db_keys:
        if k not in ESXI_IP:
            out.append({'key': k, 'ip': None, 'in_db': True})
    return jsonify(out)


@app.route('/api/regions')
def api_regions():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT esxi_key FROM vm ORDER BY esxi_key")
    rows = [r['esxi_key'] for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/inventory/<esxi_key>')
def api_inventory(esxi_key):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM vm WHERE esxi_key = ? ORDER BY name", (esxi_key,))
    vms = []
    for vm in cur.fetchall():
        vm_id = vm['id']
        vm_name = vm['name']
        cur.execute("SELECT id, name, mac FROM nic WHERE vm_id = ? ORDER BY name", (vm_id,))
        nics = []
        for nic in cur.fetchall():
            nic_id = nic['id']
            nic_name = nic['name']
            mac = nic['mac']
            cur.execute("SELECT ip FROM nic_ip WHERE nic_id = ? ORDER BY ip", (nic_id,))
            ips = [r['ip'] for r in cur.fetchall()]
            cur.execute("SELECT inner_name FROM inner_nic WHERE nic_id = ? LIMIT 1", (nic_id,))
            row = cur.fetchone()
            inner_name = row['inner_name'] if row else None
            nics.append({'id': nic_id, 'name': nic_name, 'mac': mac, 'ips': ips, 'inner_name': inner_name})
        vms.append({'id': vm_id, 'name': vm_name, 'nics': nics})
    conn.close()
    return jsonify({'esxi_key': esxi_key, 'vms': vms})


# Serve the SPA
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/topology/create_ports', methods=['POST'])
def api_create_ports():
    """POST payload: { region: 's05', links: ['h1-sw1','sw1-sw2', ...] }
    This will SSH to the ESXi host IP configured for the region (esxi_config.ESXI_IP)
    using root/410@Bupt and execute the script `Creat_Port_VSitch.sh` with the link names
    as arguments. Returns stdout/stderr.
    """
    data = request.get_json(force=True)
    region = data.get('region')
    links = data.get('links') or []

    if not region:
        return jsonify({'ok': False, 'error': 'missing region'}), 400

    try:
        from esxi_config import ESXI_IP
    except Exception:
        ESXI_IP = {}

    ip = ESXI_IP.get(region)
    if not ip:
        return jsonify({'ok': False, 'error': f'no ESXi IP configured for region {region}'}), 400

    if paramiko is None:
        return jsonify({'ok': False, 'error': 'paramiko not available on server; cannot SSH'}), 500

    username = 'root'
    password = '410@Bupt'

    cmd = './Creat_Port_VSitch.sh '
    if links:
        # join arguments safely (they should be simple labels)
        cmd += ' '.join([f"'{l}'" for l in links])

    logs = {'stdout': '', 'stderr': ''}
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=username, password=password, timeout=10)
        # run the command in the user's home directory; keeppty=False is fine
        stdin, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode(errors='ignore')
        err = stderr.read().decode(errors='ignore')
        logs['stdout'] = out
        logs['stderr'] = err
        client.close()
        return jsonify({'ok': True, 'logs': logs})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'ok': False, 'error': str(e), 'trace': tb, 'logs': logs}), 500


@app.route('/api/topology/install_ports', methods=['POST'])
def api_install_ports():
    """POST payload:
    {
      region: 's05',
      nodes: [ {id:'h1', vm:'s05-switchpc2', ip:'10.1.2.3', ...}, ... ],
      links: [ {id:'l1', a:'h1', b:'sw1', label:'h1-sw1'}, ... ]
    }
    The endpoint will compute per-VM required ports and SSH to the controller to run
    rebuild_vm_nics_govc.sh commands. Returns logs per command.
    """
    data = request.get_json(force=True)
    region = data.get('region')
    nodes = data.get('nodes') or []
    links = data.get('links') or []

    if not region:
        return jsonify({'ok': False, 'error': 'missing region'}), 400

    # build nodeId -> vmName map
    node_to_vm = { n.get('id'): n.get('vm') for n in nodes if n.get('id') }

    # build adjacency list to list link labels per node
    node_links = { n.get('id'): [] for n in nodes if n.get('id') }
    for l in links:
        a = l.get('a'); b = l.get('b'); label = l.get('label') or f"{a}-{b}"
        if a in node_links: node_links[a].append(label)
        if b in node_links: node_links[b].append(label)

    # build vm -> ports mapping
    vm_ports = {}
    for nid, vm in node_to_vm.items():
        if not vm: continue
        ports = node_links.get(nid, [])
        vm_ports.setdefault(vm, []).extend(ports)

    # load controller config
    try:
        from controller_config import CONTROLLER
    except Exception:
        return jsonify({'ok': False, 'error': 'controller configuration missing'}), 500

    host = CONTROLLER.get('host')
    user = CONTROLLER.get('user')
    password = CONTROLLER.get('password')
    if not host or not user:
        return jsonify({'ok': False, 'error': 'incomplete controller configuration'}), 500

    if paramiko is None:
        return jsonify({'ok': False, 'error': 'paramiko not available on server; cannot SSH'}), 500

    commands = []
    for vm, ports in vm_ports.items():
        if not ports: continue
        # build command: sudo ./rebuild_vm_nics_govc.sh <region> <vm> <port1> <port2> ...
        cmd = ['./rebuild_vm_nics_govc.sh', region, vm] + ports
        commands.append(cmd)

    results = []
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=user, password=password, timeout=10)
        for cmd_args in commands:
            # join safely - we will run in a shell; join with quoted args
            safe_cmd = ' '.join([f"'{str(x)}'" for x in cmd_args])
            stdin, stdout, stderr = client.exec_command('sudo ' + safe_cmd)
            out = stdout.read().decode(errors='ignore')
            err = stderr.read().decode(errors='ignore')
            results.append({'cmd': 'sudo ' + ' '.join(cmd_args), 'stdout': out, 'stderr': err})
        client.close()
        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'ok': False, 'error': str(e), 'trace': tb, 'results': results}), 500


def _get_vm_primary_ip(esxi_key, vm_name):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id FROM vm WHERE esxi_key = ? AND name = ?', (esxi_key, vm_name))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    vm_id = row['id']
    # find first IPv4 address for this VM
    cur.execute('''
        SELECT nic_ip.ip FROM nic_ip
        JOIN nic ON nic.id = nic_ip.nic_id
        WHERE nic.vm_id = ? ORDER BY nic_ip.ip
    ''', (vm_id,))
    for r in cur.fetchall():
        ip = r['ip']
        if ip and ':' not in ip:
            conn.close()
            return ip
    conn.close()
    return None


def _get_vm_nic_external_internal_pairs(esxi_key, vm_name):
    """Return list of {exter: <nic.name>, iner: <inner_name>} for the VM."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id FROM vm WHERE esxi_key = ? AND name = ?', (esxi_key, vm_name))
    row = cur.fetchone()
    out = []
    if not row:
        conn.close()
        return out
    vm_id = row['id']
    cur.execute('SELECT id, name FROM nic WHERE vm_id = ?', (vm_id,))
    nic_rows = cur.fetchall()
    for nic in nic_rows:
        nic_id = nic['id']
        nic_name = nic['name']
        cur.execute('SELECT inner_name FROM inner_nic WHERE nic_id = ? LIMIT 1', (nic_id,))
        r = cur.fetchone()
        inner = r['inner_name'] if r else None
        out.append({'exter': nic_name, 'iner': inner})
    conn.close()
    return out


@app.route('/api/topology/configure_sw', methods=['POST'])
def api_configure_sw():
    """Batch configure switches: POST payload same shape as install_ports.
    For each switch node (type!='host') find its vm, query DB for external->internal nic pairs that match the node's adjacent link labels,
    SSH to the VM and run ./configure_sw_bea.sh <bridge> <controller_ip> 6633 <exter> <iner> ...
    If the script is missing on the VM, upload local copy from ESXI/script and chmod +x.
    Returns per-VM results array.
    """
    data = request.get_json(force=True)
    region = data.get('region')
    nodes = data.get('nodes') or []
    links = data.get('links') or []

    if not region:
        return jsonify({'ok': False, 'error': 'missing region'}), 400

    # build node maps
    node_to_vm = { n.get('id'): n.get('vm') for n in nodes if n.get('id') }
    node_type = { n.get('id'): n.get('type') for n in nodes if n.get('id') }

    node_links = { n.get('id'): [] for n in nodes if n.get('id') }
    for l in links:
        a = l.get('a'); b = l.get('b'); label = l.get('label') or f"{a}-{b}"
        if a in node_links: node_links[a].append(label)
        if b in node_links: node_links[b].append(label)

    # controller IP read from controller_config.CONTROLLER.host
    try:
        from controller_config import CONTROLLER
    except Exception:
        return jsonify({'ok': False, 'error': 'controller configuration missing'}), 500
    controller_ip = CONTROLLER.get('host')
    if not controller_ip:
        return jsonify({'ok': False, 'error': 'controller host not configured'}), 500

    # SSH credentials for VM access: prefer explicit VM_SSH config
    try:
        from vm_ssh_config import VM_SSH
        ssh_user = VM_SSH.get('user')
        ssh_pass = VM_SSH.get('password')
    except Exception:
        # fallback to controller config (legacy)
        ssh_user = CONTROLLER.get('user')
        ssh_pass = CONTROLLER.get('password')

    if paramiko is None:
        return jsonify({'ok': False, 'error': 'paramiko not available on server; cannot SSH'}), 500

    results = []
    # local script path to upload if missing
    local_sw_script = os.path.join(os.path.dirname(__file__), 'script', 'configure_sw_bea.sh')

    # iterate switch nodes
    try:
        for nid, vm in node_to_vm.items():
            if not vm: continue
            # consider only switch nodes (type != 'host')
            if node_type.get(nid) == 'host':
                continue
            ports = node_links.get(nid, [])
            # fetch nic mappings from DB
            pairs = _get_vm_nic_external_internal_pairs(region, vm)
            # build args: for each port label in ports, find pair with exter==label and non-null iner
            args = []
            for p in ports:
                for pr in pairs:
                    if pr.get('exter') == p and pr.get('iner'):
                        args.extend([pr.get('exter'), pr.get('iner')])
                        break

            # skip if no args
            if not args:
                results.append({'cmd': f'{vm} (skip)', 'stdout': '', 'stderr': f'no external->internal pair found for node {nid} ports {ports}'})
                continue

            # get VM primary IP to SSH
            vm_ip = _get_vm_primary_ip(region, vm)
            if not vm_ip:
                results.append({'cmd': f'{vm} (skip)', 'stdout': '', 'stderr': f'no reachable IP found for vm {vm}'})
                continue

            # build remote command
            cmd_args = ['./configure_sw_bea.sh', nid, controller_ip, '6633'] + args
            safe_cmd = ' '.join([f"'{str(x)}'" for x in cmd_args])

            # SSH and ensure script exists/upload
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(vm_ip, username=ssh_user, password=ssh_pass, timeout=10)
            sftp = client.open_sftp()
            try:
                try:
                    sftp.stat('./configure_sw_bea.sh')
                except IOError:
                    # upload local script
                    sftp.put(local_sw_script, 'configure_sw_bea.sh')
                    client.exec_command('chmod +x ./configure_sw_bea.sh')
            finally:
                sftp.close()

            stdin, stdout, stderr = client.exec_command('sudo ./configure_sw_bea.sh ' + ' '.join([f"'{x}'" for x in cmd_args[1:]]))
            out = stdout.read().decode(errors='ignore')
            err = stderr.read().decode(errors='ignore')
            results.append({'cmd': ' '.join(cmd_args), 'stdout': out, 'stderr': err})
            client.close()

        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'ok': False, 'error': str(e), 'trace': tb, 'results': results}), 500


@app.route('/api/topology/configure_host', methods=['POST'])
def api_configure_host():
    """Batch configure hosts: for each host node (type==='host'), SSH to its VM and run ./configure_host_bea.sh <ipv6_address>
    ipv6_address is taken from the node.ip field in payload.
    """
    data = request.get_json(force=True)
    region = data.get('region')
    nodes = data.get('nodes') or []
    links = data.get('links') or []

    if not region:
        return jsonify({'ok': False, 'error': 'missing region'}), 400

    node_to_vm = { n.get('id'): n.get('vm') for n in nodes if n.get('id') }
    node_ip_arg = { n.get('id'): n.get('ip') for n in nodes if n.get('id') }
    node_type = { n.get('id'): n.get('type') for n in nodes if n.get('id') }

    try:
        from vm_ssh_config import VM_SSH
        ssh_user = VM_SSH.get('user')
        ssh_pass = VM_SSH.get('password')
    except Exception:
        try:
            from controller_config import CONTROLLER
            ssh_user = CONTROLLER.get('user')
            ssh_pass = CONTROLLER.get('password')
        except Exception:
            return jsonify({'ok': False, 'error': 'vm ssh configuration missing'}), 500

    if paramiko is None:
        return jsonify({'ok': False, 'error': 'paramiko not available on server; cannot SSH'}), 500

    results = []
    local_host_script = os.path.join(os.path.dirname(__file__), 'script', 'configure_host_bea.sh')

    try:
        for nid, vm in node_to_vm.items():
            if not vm: continue
            if node_type.get(nid) != 'host':
                continue
            ipv6 = node_ip_arg.get(nid) or ''
            if not ipv6:
                results.append({'cmd': f'{vm} (skip)', 'stdout': '', 'stderr': f'no ipv6 argument provided for host {nid}'})
                continue

            vm_ip = _get_vm_primary_ip(region, vm)
            if not vm_ip:
                results.append({'cmd': f'{vm} (skip)', 'stdout': '', 'stderr': f'no reachable IP found for vm {vm}'})
                continue

            cmd_args = ['./configure_host_bea.sh', ipv6]

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(vm_ip, username=ssh_user, password=ssh_pass, timeout=10)
            sftp = client.open_sftp()
            try:
                try:
                    sftp.stat('./configure_host_bea.sh')
                except IOError:
                    sftp.put(local_host_script, 'configure_host_bea.sh')
                    client.exec_command('chmod +x ./configure_host_bea.sh')
            finally:
                sftp.close()

            stdin, stdout, stderr = client.exec_command('./configure_host_bea.sh ' + f"'{ipv6}'")
            out = stdout.read().decode(errors='ignore')
            err = stderr.read().decode(errors='ignore')
            results.append({'cmd': './configure_host_bea.sh ' + ipv6, 'stdout': out, 'stderr': err})
            client.close()

        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'ok': False, 'error': str(e), 'trace': tb, 'results': results}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
