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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
