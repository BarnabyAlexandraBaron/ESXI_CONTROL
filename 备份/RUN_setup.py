ip_area_7=[1,1,1,1,1,6,7,1,9,1,11,12,13,14,15,15,17,18,5,1,5,5,5,5,5,5,5,5,5,5,1,1,1]
ip_area_5=[1,1,1,1,1,6,7,1,9,1,11,12,13,14,15,1,17,18,5,1,5,5,5,5,5,5,5,5,5,5,1,1,1]

pc_name = []
for i in ip_area_5:
    # print("switchpc"+str(i))
    pc_name.append("switchpc"+str(i))


commands = []
# 遍历ID从1到33
for id in range(33):
    # 计算十进制和十六进制的值
    decimal_value = 117 + (id - 1)
    hex_value = format(decimal_value, '016x')  # 格式化为16位十六进制字符串

    # 生成命令
    command0 = 'sudo ./setup_dpdk.sh'
    #command1 = f'sudo ovs-vsctl add-br sw{decimal_value} -- set bridge sw{decimal_value} datapath_type=netdev'
    #command2 = f'sudo ovs-vsctl set bridge sw{decimal_value} other-config:datapath-id={hex_value}'
    #command3 = f'sudo ovs-vsctl set-controller sw{decimal_value} tcp:172.31.1.1:6633'
    command1 = 'echo done'
    command2 = 'echo done'
    command3 = 'echo done'
    # 将命令添加到二维列表
    commands.append([command0, command1, command2, command3])


import paramiko
import time
import paramiko

def execute_command(ip_id, username, command, password='1234567', port=22):
    """
    执行指定的SSH命令，支持sudo权限
    :param ip_id: 服务器IP的最后一段
    :param username: 用户名
    :param command: 要执行的命令
    :param password: 密码，默认为'1234567'
    :param port: SSH端口，默认为22
    :return: 命令输出或错误信息
    """
    # 创建SSH对象
    ssh = paramiko.SSHClient()
    # 注意区域前缀 eg 区域5：172.31.5.ip_id 目前是5区域
    ip = "172.31.5." + ip_id
    print(20*"@"+ip + username)
    # 加载系统主机密钥并设置自动添加策略
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # 建立SSH连接
        ssh.connect(ip, port, username, password="1234567")
        print(f"SSH连接成功，正在执行命令：{command}")

        # 使用get_pty=True执行命令，支持sudo和多条命令
        stdin, stdout, stderr = ssh.exec_command(command, get_pty=True)
        
        # 为sudo命令输入密码
        stdin.write(password + '\n')
        stdin.flush()

        # 获取命令输出
        output = []
        for line in stdout:
            print(line.strip('\n'))
            output.append(line.strip('\n'))

        # 检查错误输出
        error = stderr.read().decode('utf-8')
        if error:
            print("命令执行出错：", error)
            return error

        return '\n'.join(output)

    except Exception as e:
        print("连接或执行命令失败：", str(e))
        return str(e)

    finally:
        # 关闭SSH连接
        ssh.close()
        print("SSH连接已关闭。")

# 昨天刚到13轮的样子
for i in range(13,33):
    print(i)
    # 示例调用
    print("第"+str(i)+"伦\n")
    for command_i in commands[i]:
        # print(command_i)
        execute_command(str(i+1),pc_name[i],command_i)
        # execute_command(str(i+18), pc_name[i], 'ls')  # 执行ls命令

# execute_command('1',pc_name[0],'sudo ls /root/snap')