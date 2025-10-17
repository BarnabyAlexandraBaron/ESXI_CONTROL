from collections import defaultdict

import rpyc
import math
import logging
import time
import threading
import sys
import socket
import fcntl
import struct
from datetime import datetime
from rpyc import Service
from rpyc.utils.server import ThreadedServer
from ryu.base import app_manager
from ryu.topology.api import get_all_switch, get_all_link, get_all_host, get_switch, get_link, get_host
import networkx as nx
from ryu.lib import hub
from ryu.controller.handler import set_ev_cls
from ryu.controller import ofp_event
from ryu.ofproto import ofproto_v1_0
from ryu.ofproto import ofproto_v1_2
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ofproto_v1_4
from ryu.ofproto import ofproto_v1_4_parser
from ryu.ofproto import ofproto_v1_5
from ryu.lib import ofctl_v1_0
from ryu.lib import ofctl_v1_2
from ryu.lib import ofctl_v1_3
from ryu.lib import ofctl_v1_4
from ryu.lib import ofctl_v1_5
from ryu.lib.packet import packet, ethernet, icmpv6
from ryu.lib.packet import lldp, ether_types, arp
from ryu.ofproto.ether import ETH_TYPE_ARP, ETH_TYPE_IP, ETH_TYPE_IPV6
from ryu.ofproto.ether import ETH_TYPE_LLDP
from ryu.lib.packet.in_proto import IPPROTO_TCP, IPPROTO_UDP
import ryu.app.ofctl.api as ofctl_api
from ryu.lib.packet.ipv6 import ipv6
from ryu.ofproto import inet

from ryu import cfg

CONF = cfg.CONF
COARSE_SMART_ROUTE = "coarse_smart_route"
OSPF = "ospf"
# if CONF.alg == OSPF:
#     CONF.enable_slice = False

from net_info import FlowId, FlowStats, LinkInfo, SliceInfo

from ryu.topology import event, switches
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.topology.switches import LLDPPacket
from ryu.controller import ofp_event
from ryu.base.app_manager import lookup_service_brick
from operator import attrgetter
from threading import Lock
from ryu.ofproto import nx_match
from config import *
from utils import *

import ipaddress

PRIORITY_SERVICE = 65535

PRIORITY_NDP_REPLY = 100
PRIORITY_NDP_PACKETIN = 99
PRIORITY_NDP_FLOOD = 98

PRIORITY_ARP_REPLY = 100
PRIORITY_PACKETIN = 99
PUSH_SRV6_TABLE_ID = 0
GOTO_FORWARD_TABLE_ID = 0
FORWARD_TABLE_ID = 1

DEFAULT_IPV6_PRIORITY = 0
PUSH_SRV6_PRIORITY = 100
GOTO_FORWARD_TABLE_PRIORITY = 1
FORWARD_PRIORITY = 100
ETH_DEVICE = 'eno8303'

SPEEDS_1Gbps = int(1e6)  # 1Gbps=1e6kbps

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
LOG = logging.getLogger(__name__)

supported_ofctl = {
    ofproto_v1_0.OFP_VERSION: ofctl_v1_0,
    ofproto_v1_2.OFP_VERSION: ofctl_v1_2,
    ofproto_v1_3.OFP_VERSION: ofctl_v1_3,
    ofproto_v1_4.OFP_VERSION: ofctl_v1_4,
    ofproto_v1_5.OFP_VERSION: ofctl_v1_5,
}


class NetInfoCollector(app_manager.RyuApp):
    WAITING_TOPO_DISCOVER = 30
    WATING_CV = 3

    OFP_VERSIONS = [ofproto_v1_4.OFP_VERSION]

    def __init__(self, *args, **kwargs) -> None:
        super(NetInfoCollector, self).__init__(*args, **kwargs)
        self.name = 'netinfo_collector'
        # self.net_info_interface = NetInfoInterface(self)
        self.send_lldp_packet_interval = 3
        self.net_topo = nx.DiGraph()
        self.STOP = False
        self.echo_latency = {}
        self.sw_module = lookup_service_brick('switches')
        self.src2link = dict()
        self.dst2link = dict()
        self.host_ip2sw_port = dict()
        self.installed_flows = set()
        self.ports_slices_bandwidths = dict()
        self.route_alg = None
        self.backend_interface = None
        self.all_ports_slots = dict()
        self.CONF = cfg.CONF
        self.enable_slice = False
        self.datapaths = dict()
        self.controller_ip, self.controller_mac = self.get_local_ip_address(), self.get_local_mac_address()
        self.controller_ipv6 = CONTROLLER_IPV6
        self.region_map = {
            "区域1": {1, 2, 3, 7, 8, 15, 19, 20, 21, 22},
            "区域2": {4, 5, 6, 9, 10, 16, 23, 24 ,25 ,26},
            "区域3": {11, 12, 13, 14, 17, 18, 27, 28, 29, 30},
            "区域4": set(range(31, 51)),
            "区域6": set(range(84, 117))
        }
        '''
            self.main_segments_map = {
                ("2001:db8::1", "2001:db8::2"): [[1, 3, 4], [4, 6, '2001:db8::2']],
                ("2001:db8::3", "2001:db8::4"): [[...], [...]]
            }
        '''
        self.main_segments_map = {}
        self.backup_segments_map = {}
        '''
            self.link_to_segment_map = {
                ("2001:db8::1", "2001:db8::2"): {
                    'sw1-sw3': 0,
                    'sw3-sw1': 0,
                    ...
                },
                ...
            }
        '''
        self.link_to_segment_map = {}  #用于区域内故障切换的，每个segment都是每个区域的内部路径+
        '''
        self.local_paths = {
            ("2001:db8::1", "2001:db8::2"): {
                0: {"main": [1, 3], "backup": [1, 2, 3]},
                0: {"main": [4, 6], "backup": [4, 16, 6]}
            }
        }
        '''
        self.local_paths = {}
        self.net_topo_links = []
        '''
            inv_region:  {1: '区域1', 2: '区域1', 3: '区域1', 7: '区域1', 8: '区域1', 15: '区域1', 4: '区域2', 5: '区域2', 6: '区域2', 9: '区域2', 10: '区域2', 16: '区域2', 11: '区域3', 12: '区域3', 13: '区域3', 14: '区域3', 17: '区域3', 18: '区域3'}
        '''
        self.inv_region = {}
        self.inter_region_edge_group = {}
        self.adj = defaultdict(set)
        self.failover_start_time = 0
        print(f'ip={self.controller_ip}, mac={self.controller_mac}')
        self.init_condition_value()
        # hub.spawn(self.rpc)
        threading.Thread(target=self.collect_net_info).start()

        # 等待一段时间完成网络信息采集后再初始化
        # threading.Timer(self.WAITING_TOPO_DISCOVER+10,self.initialize).start()

    def get_local_mac_address(self):
        # 获取本地网络接口信息
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifname = "ens160"  # 修改为你自己的网络接口名称
        if_info = fcntl.ioctl(sock.fileno(), 0x8927, struct.pack('256s', ifname[:15].encode('utf-8')))
        mac_address = ':'.join(['{:02x}'.format(b) for b in if_info[18:24]])
        return mac_address

    def get_local_ip_address(self):
        # 获取本地IP地址
        ip_address = "10.112.88.99"
        return ip_address

    # def initialize(self):
    #     self.alg = self.CONF.alg
    #     if self.alg not in algs.keys():
    #         print("No supported route algorithm.")
    #         raise Exception
    #     self.route_alg = algs[self.alg](self.net_info_interface)
    #     self.backend_interface = rpyc.connect(BACKEND_RPYC_IP, BACKEND_RPYC_PORT).root

    # def rpc(self):
    #     #不知道为啥，这里需要修改一下rpyc的源码，否则会报错TypeError:socket.getaddrinfo() get an unexpected keyword "type", 修改位置为"/home/liuflin/.venv/ryu/lib/python3.6/site-packages/rpyc/utils/server.py:83"
    #     sr = ThreadedServer(self.net_info_interface, port=RPC_PORT, auto_register=False, protocol_config={'allow_public_attrs': True})
    #     sr.start()

    def collect_net_info(self):
        self.init_net_info()
        # try:
        #     while self.STOP != True:
        #         self.request_and_caculate_all_links_info()
        #         #self.log_all_links_info()
        #         # self.log_all_slices_info()
        #
        #         pass
        # except(KeyboardInterrupt):
        #     print("Bye!")
        #     sys.exit()

    def init_net_info(self):
        self.init_net_topo()
        #self.log_all_ports_info()  # 添加这行
        # self.init_requset()
        # self.log_all_ports_info()
        # self.log_hosts()

    def init_net_topo(self):
        self.logger.info("waiting to discover network topology...")
        self.edge_ports = set()
        self.dp2eports = dict()  # dp to edge ports
        net_topo = self.net_topo
        hub.sleep(self.WAITING_TOPO_DISCOVER)
        self.all_switches = get_all_switch(self)
        self.links = links = get_all_link(self)
        hosts = get_all_host(self)

        for host in hosts:
            for ip in host.ipv4:
                self.host_ip2sw_port[ip] = (host.port.dpid, host.port.port_no)

        # add switch nodes
        for sw in self.all_switches:
            self.datapaths[sw.dp.id] = sw.dp
            net_topo.add_node(sw.dp.id, flow_info={FlowId: FlowStats}, type="switch")
            self.send_del_flows(sw.dp)
            self.send_lldp_discover_flow(sw.dp)
            self._add_arp_packetin_flow(sw.dp)
            self.add_ndp_packetin_flow(sw.dp)
            self.add_goto_ipv6_forward_flow(sw.dp)
        # self.logger.info("Switches: " + str([sw.dp.id for sw in self.all_switches]))
        self.logger.info("Switches (sorted by dp.id ASC): " +
                         str(sorted([sw.dp.id for sw in self.all_switches])))
        # self.install_srv6_flow(self.datapaths[30],[31],'2001:db8::3', '2001:db8::4', 101, '2001:db8::4')
        # self.install_srv6_flow(self.datapaths[31],[30],'2001:db8::4', '2001:db8::3', 101, '2001:db8::3')
        # #h1-h2
        # self.install_srv6_flow(self.datapaths[1],[2],'2001:db8::1','2001:db8::2',101,'2003:db8::4')
        # self.install_srv6_flow(self.datapaths[1],[3],'2001:db8::1','2001:db8::2',100,'2003:db8::4')
        # #sw3-sw4 和sw3-sw5的组表实验
        # self.install_srv6_flow(self.datapaths[4],[6],'2001:db8::1','2003:db8::4',101,'2001:db8::2')
        # self.install_srv6_flow(self.datapaths[5],[6],'2001:db8::1','2003:db8::4',101,'2001:db8::2')
        # #h2-h1
        # self.install_srv6_flow(self.datapaths[6],[5],'2001:db8::2','2001:db8::1',101,'2002:db8::3')
        # # self.install_srv6_flow(self.datapaths[2],[1],'2001:db8::2','2002:db8::3',101,'2001:db8::1')
        # self.install_srv6_flow(self.datapaths[3],[1],'2001:db8::2','2002:db8::3',101,'2001:db8::1')
        #
        # #tcp规则
        # self.install_push_srv6_tcp_udp_flow(self.datapaths[1],[2,4,6],'2001:db8::1','2001:db8::2',0,5201,100)
        # self.install_push_srv6_tcp_udp_flow(self.datapaths[6],[5,3,1],'2001:db8::2','2001:db8::1',5201,0,100)
        #
        #
        # # node1-node2
        # self.install_srv6_flow(self.datapaths[1], [2], '2001:db8::102', '2001:db8::103', 101, '2003:db8::4')
        # self.install_srv6_flow(self.datapaths[4], [6], '2001:db8::102', '2003:db8::4', 101, '2001:db8::103')
        # self.install_srv6_flow(self.datapaths[5], [6], '2001:db8::102', '2003:db8::4', 101, '2001:db8::103')
        # # node2-node1
        # self.install_srv6_flow(self.datapaths[6], [5], '2001:db8::103', '2001:db8::102', 101, '2002:db8::3')
        # self.install_srv6_flow(self.datapaths[3], [1], '2001:db8::103', '2002:db8::3', 101, '2001:db8::102')
        # # node1-master
        # self.install_srv6_flow(self.datapaths[1], [7, 11, 13], '2001:db8::102', '2001:db8::101', 101, '2001:db8::101')
        # # master-node1
        # self.install_srv6_flow(self.datapaths[13], [11, 7, 1], '2001:db8::101', '2001:db8::102', 101, '2001:db8::102')
        # self.install_srv6_flow(self.datapaths[7], [1], '2001:db8::101', '2002:db8::7', 101, '2001:db8::102')
        # # node2-master
        # self.install_srv6_flow(self.datapaths[6], [10, 14, 18, 17, 13], '2001:db8::103', '2001:db8::101', 101, '2001:db8::101')
        # # master-node2
        # self.install_srv6_flow(self.datapaths[13], [17, 18, 14, 10, 6], '2001:db8::101', '2001:db8::103', 101, '2001:db8::103')
        #
        # #添加组表规则（sw2-sw3都是边界节点且有连线，且sw2有一条区间链路sw2-sw4）
        # self.add_fast_failover_group(self.datapaths[2],1,[(4,4),(3,3)])
        # self.add_fast_failover_group(self.datapaths[3],1,[(5,5),(3,3)])
        # self.add_fast_failover_group(self.datapaths[3],2,[(3,3),(5,5)])
        #
        # #添加跨区域规则(第三点规则)
        # self.add_srv6_flow_cross_region([15,12,10],[[8],[18,14],[16]],'2001:db8::3','2001:db8::4',100)
        # #回来的路线：host4->host3
        # self.install_srv6_flow(self.datapaths[16],[5,3,15],'2001:db8::4','2001:db8::3',101,'2001:db8::3')
        if self.sw_module is None:
            self.sw_module = lookup_service_brick('switches')
        self.all_ports = all_ports = self.sw_module.get_all_ports()
        self.logger.info("links_num:%d" % len(links))

        # add switch edges and get inner_ports
        self.inner_ports = set()
        for li in links:
            li.src.bandwidth_kbps = 1e6  # 1Gbps = 1e6kbps
            self.inner_ports.add(li.src)
            # curr_speed = min(li.src.ofpport.properties[0].curr_speed, li.dst.ofpport.properties[0].curr_speed)
            curr_speed = 1e9  # bps
            bandwidth = curr_speed
            net_topo.add_edge(li.src.dpid, li.dst.dpid, link_info=LinkInfo(li.src.port_no, li.dst.port_no, bandwidth))
            if not self.src2link.__contains__(li.src.dpid):
                self.src2link[li.src.dpid] = dict()
            if not self.dst2link.__contains__(li.dst.dpid):
                self.dst2link[li.dst.dpid] = dict()
            self.src2link[li.src.dpid][li.src.port_no] = net_topo.edges[li.src.dpid, li.dst.dpid]
            self.dst2link[li.dst.dpid][li.dst.port_no] = net_topo.edges[li.src.dpid, li.dst.dpid]

        # get edge_ports
        for port in all_ports:
            if port not in self.inner_ports:
                port.bandwidth_kbps = 10e6
                self.edge_ports.add(port)
                if self.dp2eports.get(port.dpid) == None:
                    self.dp2eports[port.dpid] = [port.port_no]
                else:
                    self.dp2eports[port.dpid].append(port.port_no)
            else:
                port.bandwidth_kbps = 1e6
        self.net_topo_links = sorted([(li.src.dpid, li.dst.dpid) for li in links])
        self.logger.info("Links: " + str(self.net_topo_links))
        # self.init_slices()
        self.init_forward_rules()
        self.add_ndp_flood_rule()
        self.discover_v6_hosts()
        self.logger.info("----------------------------------------")

        # 添加sw3的传送规则（因为sw2-sw4与sw2-sw3作为组表）,给sw3-sw5添加ipv6流表规则
        # self.add_forward_rule_with_group_table(self.datapaths[2], '2003:db8::4', 200, 1)
        # self.add_forward_rule_with_group_table(self.datapaths[3], '2003:db8::4', 200, 1)
        # self.add_forward_rule_with_group_table(self.datapaths[3], '2003:db8::5', 200, 2)

        # 计算路由（域内故障切换）
        # self.compute_and_setup_paths("2000:db8::1", "2000:db8::2")
        #self.compute_and_setup_paths("2000:db8::1", "2000:db8::3")
        # print("main_segments_map: ", self.main_segments_map)
        # print("backup_segments_map", self.backup_segments_map)
        # print("link_to_segment_map: ", self.link_to_segment_map)
        # print("local_paths: ", self.local_paths)
        # 区域间故障切换
        # 构建反向映射：node -> region
        # self.inv_region = {n: r for r, nodes in self.region_map.items() for n in nodes}
        # # print("inv_region: ", self.inv_region)
        #
        # # 构建邻接表
        # for u, v in self.net_topo_links:
        #     self.adj[u].add(v)
        #     self.adj[v].add(u)
        # # print(dict(self.adj))
        #
        # # 直接获取有向跨区域链路（12 条）
        # directed_links = self.find_inter_region_links_directed(self.net_topo_links)
        # # print("有向跨区域链路：", directed_links)
        #
        # for src, dst in directed_links:
        #     self.init_inter_region_edge_group(src, dst)
        #
        # # print("跨区域链路的direct与fallback情况", self.inter_region_edge_group)
        # for segment in self.main_segments_map[("2000:db8::1", "2000:db8::2")][:-1]:
        #     u, v = segment[-2], segment[-1]
        #     # print(f"我要来测试一下了,(u,v)=({u},{v})", self.inter_region_edge_group[(u, v)])
        #     info = self.inter_region_edge_group[(u, v)]
        #     direct = info.get("direct", [])
        #     fallback = info.get("fallback", [])
        #     if len(direct) == 2:
        #         # 规则 1：direct 长度为 2
        #         # print(f"(u,v)=({u},{v}) 命中规则 1：direct 长度为2 → {direct}")
        #         port_list = self.get_direct_ports_from_source(u, direct)
        #         # print("port_list:", port_list)
        #         # 1.添加组表规则
        #         self.clear_all_groups(self.datapaths[u])
        #         self.add_fast_failover_group(self.datapaths[u], 1,
        #                                      [(port_list[0], port_list[0]), (port_list[1], port_list[1])])
        #         # 2.添加ipv6的匹配组表规则
        #         self.add_forward_rule_with_group_table(self.datapaths[u],
        #                                                ":".join(f"{x:04x}" for x in dpid2ipv6(direct[0])), 200, 1)
        #         # 3.添加另一个区域的SRv6规则[对于（3,4）或(3,5)]
        #         for seg in self.main_segments_map[("2000:db8::1", "2000:db8::2")]:
        #             if seg[0] == v:
        #                 path, _ = self.compute_main_and_backup_paths_in_local_region(direct[1], seg[-2], [direct[1]] + seg[1:-1])
        #                 # 必不可能是第一段，因为跨域了
        #                 last = seg[-1]
        #                 if isinstance(last, int):
        #                     node_dst = ":".join(f"{x:04x}" for x in dpid2ipv6(last))
        #                 else:
        #                     node_dst = last  # 直接就是合法 IPv6 字符串
        #                 self.install_srv6_flow(self.datapaths[direct[1]], path[1:], "2000:db8::1",
        #                                        ":".join(f"{x:04x}" for x in dpid2ipv6(v)), 101, node_dst)
        #     elif len(direct) == 1 and len(fallback) == 1:
        #         # 规则 2：direct 和 fallback 都是 1
        #         print(f"(u,v)=({u},{v}) 命中规则 2：direct 和 fallback 都是1 → direct={direct}, fallback={fallback}")
        #     else:
        #         # 规则 3：其他情况
        #         print(f"(u,v)=({u},{v}) 命中规则 3：其他情况 → direct={direct}, fallback={fallback}")

    def discover_v6_hosts(self):
        for host in HOSTS_IPV6:
            self.flood_arp_request(host)
            time.sleep(1)  # waiting disccover
        self.logger.info(HOSTS_IPV6)

    #

    def init_forward_rules(self):
        for link in self.links:
            dst_ipv6 = ":".join(f"{x:04x}" for x in dpid2ipv6(link.dst.dpid))
            # self.logger.info(dst_ipv6)
            if self.enable_slice:
                self.add_forward_rule(link.src.dpid, link.src.port_no, dst_ipv6)
            else:
                self.add_forward_rule_no_slice(link.src.dpid, link.src.port_no, dst_ipv6, 100)

    # def init_slices(self):
    #     for datapath in self.datapaths.values():
    #         delete_all_meters(datapath)
    #
    #     for port in self.all_ports:
    #         datapath = self.datapaths[port.dpid]
    #         bandwidth_kbps = port.bandwidth_kbps
    #
    #         if self.enable_slice:
    #             rates = [int(bandwidth_kbps/N_SLICES)] * N_SLICES
    #             self.ports_slices_bandwidths[(port.dpid, port.port_no)] = rates
    #             create_port_slices(datapath, port.port_no, rates)
    #         else:
    #             create_meter(datapath, port.port_no, SPEEDS_1Gbps)

    def init_condition_value(self):
        self.port_stats_cv = threading.Condition()
        self.echo_cv = threading.Condition()
        self.meter_stats_cv = threading.Condition()
        self.meter_config_cv = threading.Condition()

    def init_requset(self):
        for (src_dpid, dst_dpid, link_info) in self.net_topo.edges(data="link_info"):
            src_dp = self.datapaths[src_dpid]
            dst_dp = self.datapaths[dst_dpid]
            # requset link info
            self.send_port_stats_request(src_dp, link_info.src_port_no)
            if self.enable_slice:
                self.send_slice_stats_request(src_dp, link_info.src_port_no)
            else:
                self.send_meter_stats_request(src_dp, link_info.src_port_no)
            self.send_port_stats_request(dst_dp, link_info.dst_port_no)
            # ready to set next stats
            link_info.inc_index()
        if self.enable_slice:
            for sw in self.all_switches:
                self.send_all_ports_get_slice_config_request(sw.dp)

    def request_delay_all_dps(self):
        for dpid in self.net_topo.nodes():
            dp = self.datapaths[dpid]
            self.send_echo_request(dp)

    def request_and_caculate_all_links_info(self):
        self.request_delay_all_dps()
        self.calculate_link_delay()
        for (src_dpid, dst_dpid, link_info) in self.net_topo.edges(data="link_info"):
            src_dp = self.datapaths[src_dpid]
            dst_dp = self.datapaths[dst_dpid]
            # requset link and slice info
            self.send_port_stats_request(src_dp, link_info.src_port_no)
            if self.enable_slice:
                self.send_slice_stats_request(src_dp, link_info.src_port_no)
            else:
                self.send_meter_stats_request(src_dp, link_info.src_port_no)
            self.send_port_stats_request(dst_dp, link_info.dst_port_no)
            # after get info then can caculate throughput and loss rate.
            link_info.cal_throughput()
            link_info.cal_loss()
            # ready to set next stats
            link_info.inc_index()
        if self.enable_slice:
            for sw in self.all_switches:
                self.send_all_ports_get_slice_config_request(sw.dp)

    def log_all_links_info(self):
        self.logger.info('================================================================================')
        self.logger.info('                                    LINKS INFO')
        self.logger.info('================================================================================')
        # self.logger.info('   src            dst          th-s0          th-s1        th-s2        th-s3     th-tx       th-all        loss         delay(ms)   bandwidth(Gbps)')
        # self.logger.info('-----------  ------------  ---------------  ----------   ---------   ----------  ---------  ----------    ----------    ----------   --------------') 
        self.logger.info('   src            dst          th-tx          loss         delay(ms)   bandwidth(Gbps)')
        self.logger.info('-----------  ------------    ---------      ----------    ----------   --------------')
        for (s, d, link_info) in self.net_topo.edges(data="link_info"):
            th = link_info.throughput / 1e6
            if CONF.enable_slice:
                th_all = link_info.all_throughput / 1e6
                slices_th = [slice_info.throughput / 1e6 for slice_info in link_info.slices_info]
            lo = link_info.loss
            delay = link_info.delay * 1000
            bandwidth = link_info.bandwidth / 1e9
            self.logger.info(
                f' {s:4d}           {d:4d}        {th:.4f}mbps      {lo:.4f}            {delay:.2f}       {bandwidth:.0f}')
            # self.logger.info(f'   {s:4d}         {d:4d}           {slices_th[0]:.4f}mbps   {slices_th[1]:.4f}mbps   {slices_th[2]:.4f}mbps    {slices_th[3]:.4f}mbps  {th:.4f}mbps    {th_all:.4f}mbps      {lo:.4f}      {delay:.2f}       {bandwidth:.0f}')
            # self.logger.info('%12x    %12x        %.4f       %.6f        %.2f         %.0f'%(s,d,th,lo, delay*1000, bandwidth))

    def log_all_slices_info(self):
        self.logger.info('============================================================================')
        self.logger.info('                               SLICES INFO')
        self.logger.info('============================================================================')
        self.logger.info('src             '
                         'dst             '
                         'slice  '
                         'throughput(bps)  '
                         'loss       '
                         'bandwidth')
        self.logger.info('--------------  '
                         '--------------  '
                         '-----  '
                         '----------   '
                         '---------  '
                         '----------')
        for (s, d, link_info) in self.net_topo.edges(data="link_info"):
            for i in range(N_SLICES):
                slice_info = link_info.slices_info[i]
                th = slice_info.throughput
                lo = slice_info.loss
                bandwidth = 0
                self.logger.info('%12x    %12x    %1d       %.8f      %.6f     %.0f' % (s, d, i, th, lo, bandwidth))

    # def log_all_ports_info(self):
    #     self.logger.info('-----------------------------------------------------------------------------')
    #     self.logger.info('datapath      port  bandwidth(bps)')
    #     self.logger.info('-----------   ----  ---------')
    #     sws = get_all_switch(self)
    #     for sw in sws:
    #         for p in sw.ports:
    #             dpid = sw.dp.id
    #             port_no = p.port_no
    #             curr_speed = p.ofpport.properties[0].curr_speed
    #             bandwidth = math.inf if curr_speed == 0 else curr_speed
    #             self.logger.info('%12x   %d    %.0f' % (dpid, port_no, bandwidth))
    #     self.logger.info('-----------------------------------------------------------------------------')


    def log_all_ports_info(self):
        """打印所有交换机的端口信息"""
        self.logger.info("=== All Switch Ports Information ===")
        for sw in self.all_switches:
            dpid = sw.dp.id
            self.logger.info(f"Switch {dpid} ports:")
            for port in sw.ports:
                self.logger.info(
                    f"  Port {port.port_no}: "
                    f"name={port.name}, "
                    f"mac={port.hw_addr}, "
                    f"speed={port.curr_speed if hasattr(port, 'curr_speed') else 'N/A'}"
                )

    def log_hosts(self):
        self.logger.info(f'===========HOSTS=============')
        self.logger.info(f'{self.host_ip2sw_port.keys()}')

    def calculate_link_delay(self):
        """
            Get link delay.
                        Controller
                        |        |
        src echo latency|        |dst echo latency
                        |        |
                    SwitchA-------SwitchB
                        
                    fwd_delay--->
                        <----reply_delay
            delay = (forward delay + reply delay - src datapath's echo latency
        """
        if self.sw_module is None:
            self.sw_module = lookup_service_brick('switches')
        for src in self.sw_module.ports.keys():
            dst = self.sw_module.ports[src].dst
            lldp_delay = self.sw_module.ports[src].delay
            if dst is not None and src != dst:
                src_latency = self.echo_latency[src.dpid]
                dst_latency = self.echo_latency[dst.dpid]
                delay = lldp_delay - (src_latency + dst_latency) / 2
                self.net_topo[src.dpid][dst.dpid]['link_info'].delay = max(delay, 0)

    def send_port_stats_request(self, datapath, port):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        req = ofp_parser.OFPPortStatsRequest(datapath, 0, port)
        datapath.send_msg(req)
        with self.port_stats_cv:
            self.port_stats_cv.wait(self.WATING_CV)

    def send_all_ports_get_slice_config_request(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPMeterConfigStatsRequest(datapath, 0, ofproto.OFPM_ALL)
        datapath.send_msg(req)
        with self.meter_config_cv:
            self.meter_config_cv.wait(self.WATING_CV)

    @set_ev_cls(ofp_event.EventOFPMeterConfigStatsReply, MAIN_DISPATCHER)
    def meter_config_stats_reply_handler(self, ev):
        try:
            dpid = ev.msg.datapath.id
            for stat in ev.msg.body:
                meter_id = stat.meter_id
                bands = stat.bands
                port_no = int(meter_id / N_SLICES)
                slice_id = meter_id % N_SLICES
                for band in bands:
                    slice_bandwidth_kbps = band.rate
                self.ports_slices_bandwidths[(dpid, port_no)][slice_id] = slice_bandwidth_kbps
        finally:
            with self.meter_config_cv:
                self.meter_config_cv.notify_all()

    def send_flow_stats_request(self, datapath, in_port):
        ofp_parser = datapath.ofproto_parser
        req = ofp_parser.OFPFlowStatsRequest(
            datapath, 0, match=ofp_parser.OFPMatch(in_port=in_port))
        datapath.send_msg(req)

    def send_echo_request(self, dp):
        """
            Seng echo request msg to datapath.
        """
        parser = dp.ofproto_parser
        echo_req = parser.OFPEchoRequest(dp,
                                         data=b"%.12f" % time.time())
        dp.send_msg(echo_req)
        with self.echo_cv:
            self.echo_cv.wait(self.WATING_CV)

    def send_flow_mod(self, datapath, match, inst, hard_timeout=0, idle_timeout=0, priority=PRIORITY_SERVICE,
                      table_id=0, cookie=0, cookie_mask=0, importance=0):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        buffer_id = ofp.OFP_NO_BUFFER
        req = ofp_parser.OFPFlowMod(datapath, cookie, cookie_mask,
                                    table_id, ofp.OFPFC_ADD,
                                    idle_timeout, hard_timeout,
                                    priority, buffer_id,
                                    ofp.OFPP_ANY, ofp.OFPG_ANY,
                                    ofp.OFPFF_SEND_FLOW_REM,
                                    importance,
                                    match, inst)
        datapath.send_msg(req)

    def send_lldp_discover_flow(self, dp):
        ofproto = dp.ofproto
        ofproto_parser = dp.ofproto_parser
        if ofproto.OFP_VERSION == ofproto_v1_0.OFP_VERSION:
            rule = nx_match.ClsRule()
            rule.set_dl_dst(arp.addrconv.mac.text_to_bin(
                lldp.LLDP_MAC_NEAREST_BRIDGE))
            rule.set_dl_type(ETH_TYPE_LLDP)
            actions = [ofproto_parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER, self.LLDP_PACKET_LEN)]
            dp.send_flow_mod(
                rule=rule, cookie=0, command=ofproto.OFPFC_ADD,
                idle_timeout=0, hard_timeout=0, actions=actions,
                priority=0xFFFF)
        elif ofproto.OFP_VERSION >= ofproto_v1_2.OFP_VERSION:
            match = ofproto_parser.OFPMatch(
                eth_type=ETH_TYPE_LLDP,
                eth_dst=lldp.LLDP_MAC_NEAREST_BRIDGE)
            # OFPCML_NO_BUFFER is set so that the LLDP is not
            # buffered on switch
            parser = ofproto_parser
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                              ofproto.OFPCML_NO_BUFFER
                                              )]
            inst = [parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(datapath=dp, match=match,
                                    idle_timeout=0, hard_timeout=0,
                                    instructions=inst,
                                    priority=0xFFFF)
            dp.send_msg(mod)
        else:
            LOG.error('cannot install flow. unsupported version. %x',
                      dp.ofproto.OFP_VERSION)

    def send_del_flows(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Create an empty match to match all flows
        match = parser.OFPMatch()

        # Create a flow deletion request message
        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=ofproto.OFPTT_ALL,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )

        # Send the flow deletion request to the switch
        datapath.send_msg(flow_mod)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        body = ev.msg.body
        # self.logger.info('datapath         port     '
        #                  'rx-pkts  rx-bytes rx-error '
        #                  'tx-pkts  tx-bytes tx-error')
        # self.logger.info('---------------- -------- '
        #                  '-------- -------- -------- '
        #                  '-------- -------- --------')

        try:
            dpid = ev.msg.datapath.id
            # 提取统计信息到下面两个字典
            for stat in sorted(body, key=attrgetter('port_no')):
                # self.logger.info('%016x %8x %8d %8d %8d %8d %8d %8d   %8d   %8d',
                #                  ev.msg.datapath.id, stat.port_no,
                #                  stat.rx_packets, stat.rx_bytes, stat.rx_errors,
                #                  stat.tx_packets, stat.tx_bytes, stat.tx_errors, stat.duration_sec, stat.duration_nsec)
                port_no = stat.port_no
                rx_pkts = stat.rx_packets
                tx_pkts = stat.tx_packets
                rx_bytes = stat.rx_bytes
                tx_bytes = stat.tx_bytes
                rx_drop_pkts = stat.rx_dropped + stat.rx_errors
                tx_drop_pkts = stat.tx_dropped + stat.tx_errors
                src_link_info = self.src2link[dpid][port_no]['link_info']
                src_link_info.set_tx_stats(tx_pkts, tx_bytes, tx_drop_pkts)
                src_link_info.set_duration(stat.duration_sec + stat.duration_nsec * 1e-9)
                dst_link_info = self.dst2link[dpid][port_no]['link_info']
                dst_link_info.set_rx_stats(rx_pkts, rx_bytes, rx_drop_pkts)
        finally:
            with self.port_stats_cv:
                self.port_stats_cv.notify_all()

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        """
            Handle the echo reply msg, and get the latency of link.
        """
        now_timestamp = time.time()
        latency = now_timestamp - eval(ev.msg.data)
        self.echo_latency[ev.msg.datapath.id] = latency
        with self.echo_cv:
            self.echo_cv.notify_all()

    # todo: test these update API: add_switch, del_switch, add_link, del_link, add_host
    @set_ev_cls(event.EventSwitchEnter)
    def add_switch(self, ev):
        dpid = ev.switch.dp.id
        self.src2link[dpid] = dict()
        self.dst2link[dpid] = dict()

    @set_ev_cls(event.EventSwitchLeave)
    def del_switch(self, ev):
        dpid = ev.switch.dp.id
        self.src2link.pop(dpid)
        self.dst2link.pop(dpid)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6)
        inst = [parser.OFPInstructionGotoTable(table_id=FORWARD_TABLE_ID)]
        mod = parser.OFPFlowMod(datapath=datapath, table_id=PUSH_SRV6_TABLE_ID, priority=DEFAULT_IPV6_PRIORITY,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    def add_forward_rule(self, src_dpid, src_port, dst_ipv6):
        src_datapath = self.datapaths[src_dpid]
        if src_datapath != None:
            ofproto = src_datapath.ofproto
            parser = src_datapath.ofproto_parser
            for nw_tos in range(N_SLICES):
                match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=dst_ipv6, ip_dscp=nw_tos)

                actions = parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(src_port)])

                inst = [parser.OFPInstructionMeter(meter_id=src_port * 4 + nw_tos), actions]

                mod = parser.OFPFlowMod(datapath=src_datapath, priority=FORWARD_PRIORITY, match=match,
                                        instructions=inst, table_id=FORWARD_TABLE_ID)

                src_datapath.send_msg(mod)

    def add_forward_rule_no_slice(self, src_dpid, src_port, dst_ipv6, priority, in_port=None):
        src_datapath = self.datapaths.get(src_dpid)
        if src_datapath is not None:
            ofproto = src_datapath.ofproto
            parser = src_datapath.ofproto_parser
            if in_port is not None:
                match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=dst_ipv6, in_port=in_port)
            else:
                match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=dst_ipv6)
            actions = parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(src_port)])
            inst = [actions]

            # 如果没有传入 in_port，则不包含该字段
            mod = parser.OFPFlowMod(
                datapath=src_datapath,
                command=ofproto.OFPFC_ADD,  # 使用 ADD 命令来添加流表项
                priority=priority,
                match=match,
                instructions=inst,
                table_id=FORWARD_TABLE_ID
            )
            # 发送流表添加消息到交换机
            src_datapath.send_msg(mod)

    def add_forward_rule_with_group_table(self, datapath, dst_ipv6, priority, group_id):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionGroup(group_id=group_id)]
        match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=dst_ipv6)
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst)
        datapath.send_msg(mod)

    def add_host_forward_rule(self, src_dpid, src_port, dst_ipv6):
        src_datapath = self.datapaths.get(src_dpid)
        if src_datapath != None:
            ofproto = src_datapath.ofproto
            parser = src_datapath.ofproto_parser

            match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=dst_ipv6)

            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(src_port)])]

            mod = parser.OFPFlowMod(datapath=src_datapath, priority=FORWARD_PRIORITY, match=match, instructions=inst,
                                    table_id=FORWARD_TABLE_ID)
            src_datapath.send_msg(mod)

    @set_ev_cls(event.EventLinkDelete)
    def del_link(self, ev):
        li = ev.link

    @set_ev_cls(event.EventHostAdd)
    def add_host_handler(self, ev):
        h = ev.host
        for ip in h.ipv6:
            if ip == "::":
                continue
            self.add_host(h.port.dpid, h.port.port_no, ip)

    def add_host(self, dpid, port_no, ip):
        # logging.info(f"Host {ip} Add.")
        self.add_host_forward_rule(dpid, port_no, ip)
        self.host_ip2sw_port[ip] = (dpid, port_no)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto

        # 处理超时事件
        if msg.reason == ofproto.OFPRR_HARD_TIMEOUT:
            # 获取被删除的规则信息
            cookie = msg.cookie
            priority = msg.priority
            match = msg.match

            # 创建一个 FlowMod 消息，用于删除对应的流表规则
            flow_mod = datapath.ofproto_parser.OFPFlowMod(
                datapath=datapath,
                cookie=cookie,
                command=ofproto.OFPFC_DELETE,
                priority=priority,
                match=match
            )

            # 发送 FlowMod 消息给交换机
            datapath.send_msg(flow_mod)

    def send_ndp_ad(self, datapath, port_no, mac_src, mac_dst, ipv6_src, ipv6_dst):
        eth = ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_IPV6,
            dst=mac_dst,  # 目标MAC地址为广播地址
            src=mac_src  # 控制器的MAC地址
        )
        v6 = ipv6(src=ipv6_src, dst=ipv6_dst, nxt=inet.IPPROTO_ICMPV6)
        ic = icmpv6.icmpv6(
            type_=icmpv6.ND_NEIGHBOR_ADVERT,
            data=icmpv6.nd_neighbor(dst=ipv6_src, option=icmpv6.nd_option_tla(hw_src=mac_src)))

        pkt = packet.Packet()
        pkt.add_protocol(eth)
        pkt.add_protocol(v6)
        pkt.add_protocol(ic)
        pkt.serialize()
        self.send_packet_output_port(datapath, port_no, pkt.data)

    def ndp_handler(self, src_mac, src_ipv6, dst_ipv6, icmpv6_type, ndp_hdr, dpid, in_port):
        if icmpv6_type == icmpv6.ND_NEIGHBOR_SOLICIT and ndp_hdr.dst == self.controller_ipv6:
            # send packet out message
            # logging.info(f'Recive ND_NEIGHBOR_SOLICIT: dpid={dpid}, src_ipv6={src_ipv6},dst_ip={dst_ipv6}')
            self.send_ndp_reply(self.datapaths[dpid], in_port, src_mac, src_ipv6)

        elif icmpv6_type == icmpv6.ND_NEIGHBOR_ADVERT:
            self.add_host(dpid, in_port, ndp_hdr.dst)
            # logging.info(f'Recive ND_NEIGHBOR_ADVERT: dpid={dpid}, src_ipv6={src_ipv6},dst_ip={dst_ipv6}, target = {ndp_hdr.dst}')
            return

    def arp_handler(self, data, pkt_type, pkt_data, dpid, port_no):
        arp_pkt, _, _ = pkt_type.parser(pkt_data)
        self.host_ip2sw_port[arp_pkt.src_ip] = (dpid, port_no)
        for edge_port in self.edge_ports:
            edge_dpid = edge_port.dpid
            if edge_dpid != dpid:
                edge_dp = self.datapaths[edge_dpid]
                self._add_arp_reply_flow(edge_dp, arp_pkt.src_ip, arp_pkt.src_mac)

        if arp_pkt.opcode == arp.ARP_REQUEST:
            # send packet out message
            # print(f'Flood arp request,dpid={dpid},src_ip={arp_pkt.src_ip},dst_ip={arp_pkt.dst_ip}')
            for edge_port in self.edge_ports:
                edge_dpid = edge_port.dpid
                if edge_dpid != dpid:
                    edge_dp = self.datapaths[edge_dpid]
                    self.send_packet_output_port(edge_dp, edge_port.port_no, data)

        elif arp_pkt.opcode == arp.ARP_REPLY:
            if arp_pkt.dst_ip in self.host_ip2sw_port.keys():
                (dst_dpid, port_no) = self.host_ip2sw_port.get(arp_pkt.dst_ip)
            else:
                return
            dst_dp = self.datapaths[dst_dpid]
            # send packet out message
            self.send_packet_output_port(dst_dp, port_no, data)

    def flow_handler(self, l2_src, l2_dst, src_port, dst_port, ):
        # 检查是否是已添加的意图
        src = self.host_ip2sw_port.get(l2_src)
        dst = self.host_ip2sw_port.get(l2_dst)
        # print(f'src={l2_src}:{src_port}, dst={l2_dst}:{dst_port}')
        if src is None or dst is None:
            return
        if self.backend_interface is None:
            return
        flow = self.backend_interface.get_packetin_flow(l2_src, l2_dst, src_port, dst_port)
        # print("flow:"+str(flow))
        if flow is None:
            return

        if self.route_alg is not None and (l2_src, l2_dst, src_port, dst_port) not in self.installed_flows:
            threading.Thread(target=self.route_alg.handle_packetin_flow,
                             args=(src, dst, flow)).start()  # 计算路径比较花时间，用线程避免事件阻塞
            self.installed_flows.add((l2_src, l2_dst, src_port, dst_port))  # todo 定时删除

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packetin_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        data = msg.data

        if msg.datapath.ofproto.OFP_VERSION == ofproto_v1_0.OFP_VERSION:
            in_port = msg.in_port
        elif msg.datapath.ofproto.OFP_VERSION >= ofproto_v1_2.OFP_VERSION:
            in_port = msg.match['in_port']

        # 链路层
        eth_header, pkt_type, pkt_data = ethernet.ethernet.parser(data)
        l2_proto = eth_header.ethertype

        if l2_proto == ETH_TYPE_ARP:
            self.arp_handler(data, pkt_type, pkt_data, dpid, in_port)
            return
        elif l2_proto == ETH_TYPE_IP:
            # 网络层
            l2_header, l2_type, l2_data = pkt_type.parser(pkt_data)
            l2_src = l2_header.src
            l2_dst = l2_header.dst
            l3_proto = l2_header.proto
            if (
                    l2_dst == "224.0.0.251" or l2_dst == "255.255.255.255" or l2_src == "0.0.0.0" or l2_dst == '224.0.0.22'):
                return

        elif l2_proto == ETH_TYPE_IPV6:
            ipv6_hdr, nxt, ipv6_data = pkt_type.parser(pkt_data)
            ipv6_src = ipv6_hdr.src
            if ipv6_hdr.nxt == inet.IPPROTO_ICMPV6:
                icmpv6_hdr, _, _ = nxt.parser(ipv6_data)
                self.add_host(dpid, in_port, ipv6_src)
                self.ndp_handler(eth_header.src, ipv6_src, ipv6_hdr.dst, icmpv6_hdr.type_, icmpv6_hdr.data, dpid,
                                 in_port)
            return

        else:
            return

        # 传输层
        l3_header, l3_type, l3_data = l2_type.parser(l2_data)
        if l3_proto == IPPROTO_TCP or l3_proto == IPPROTO_UDP:
            src_port = l3_header.src_port
            dst_port = l3_header.dst_port
        else:
            return
        self.flow_handler(l2_src, l2_dst, src_port, dst_port)

    def _add_arp_reply_flow(self, datapath, arp_tpa, arp_tha):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_ARP,
            arp_op=arp.ARP_REQUEST,
            arp_tpa=arp_tpa)

        actions = [
            parser.NXActionRegMove(
                src_field="eth_src", dst_field="eth_dst", n_bits=48),
            parser.OFPActionSetField(eth_src=arp_tha),
            parser.OFPActionSetField(arp_op=arp.ARP_REPLY),
            parser.NXActionRegMove(
                src_field="arp_sha", dst_field="arp_tha", n_bits=48),
            parser.NXActionRegMove(
                src_field="arp_spa", dst_field="arp_tpa", n_bits=32),
            parser.OFPActionSetField(arp_sha=arp_tha),
            parser.OFPActionSetField(arp_spa=arp_tpa),
            parser.OFPActionOutput(ofproto.OFPP_IN_PORT)]
        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions)]

        self._add_flow(datapath, PRIORITY_ARP_REPLY, match, instructions, 0)

    def add_goto_ipv6_forward_flow(self, datapath):
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=ETH_TYPE_IPV6)

        # 去forward table查
        goto_table_action = parser.OFPInstructionGotoTable(table_id=FORWARD_TABLE_ID)

        inst = [goto_table_action]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=GOTO_FORWARD_TABLE_ID,
            priority=GOTO_FORWARD_TABLE_PRIORITY,
            match=match,
            instructions=inst)

        datapath.send_msg(mod)

    def add_ndp_packetin_flow(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match_so = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ip_proto=inet.IPPROTO_ICMPV6,
                                   icmpv6_type=icmpv6.ND_NEIGHBOR_SOLICIT, ipv6_nd_target=self.controller_ipv6)
        match_ad = parser.OFPMatch(eth_type=ETH_TYPE_IPV6, ipv6_dst=self.controller_ipv6, ip_proto=inet.IPPROTO_ICMPV6,
                                   icmpv6_type=icmpv6.ND_NEIGHBOR_ADVERT)

        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        self._add_flow(datapath, PRIORITY_NDP_PACKETIN, match_so, instructions, 0)
        self._add_flow(datapath, PRIORITY_NDP_PACKETIN, match_ad, instructions, 0)

    '''
    @breif 利用最小生成树算法生成ndp flood规则, 避免网络风暴
    '''

    def add_ndp_flood_rule(self):
        # 获取无向图的最小生成树
        mst = nx.minimum_spanning_tree(self.net_topo.to_undirected())

        for dpid in mst.nodes():
            dp = self.datapaths.get(dpid)
            if dp is None:
                self.logger.warning(f"Datapath for dpid {dpid} not found.")
                continue

            ofproto = dp.ofproto
            parser = dp.ofproto_parser

            # 匹配邻居发现报文
            match_so = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ip_proto=inet.IPPROTO_ICMPV6,
                icmpv6_type=icmpv6.ND_NEIGHBOR_SOLICIT
            )
            match_ad = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ip_proto=inet.IPPROTO_ICMPV6,
                icmpv6_type=icmpv6.ND_NEIGHBOR_ADVERT
            )

            # ✅ 修改这里，处理无向边访问有向图的情况
            ports = []
            for src, dst in mst.edges(dpid):
                if self.net_topo.has_edge(src, dst):
                    ports.append(self.net_topo.edges[src, dst]["link_info"].src_port_no)
                elif self.net_topo.has_edge(dst, src):
                    ports.append(self.net_topo.edges[dst, src]["link_info"].src_port_no)
                else:
                    self.logger.warning(f"No such edge in net_topo: ({src}, {dst})")

            # 加上边缘端口（连接主机的端口）
            ports += self.dp2eports.get(dpid, [])

            actions = [
                parser.OFPActionOutput(port, ofproto.OFPCML_NO_BUFFER)
                for port in ports
            ]
            instructions = [
                parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
            ]

            self._add_flow(dp, PRIORITY_NDP_FLOOD, match_ad, instructions, 0)
            self._add_flow(dp, PRIORITY_NDP_FLOOD, match_so, instructions, 0)

    def _add_arp_packetin_flow(self, datapath):
        self._add_proto_packetin_flow(datapath, ether_types.ETH_TYPE_ARP)

    def _add_ip_packetin_flow(self, datapath):
        self._add_proto_packetin_flow(datapath, ether_types.ETH_TYPE_IP)

    def _add_proto_packetin_flow(self, datapath, ip_proto):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(eth_type=ip_proto)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions)]
        self._add_flow(datapath, PRIORITY_PACKETIN, match, instructions, 0)

    @staticmethod
    def _add_flow(datapath, priority, match, instructions,
                  table_id):
        parser = datapath.ofproto_parser

        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=table_id,
            priority=priority,
            match=match,
            instructions=instructions)

        datapath.send_msg(mod)

    def flood_arp_request(self, dst_ip):
        ip = ipaddress.ip_address(dst_ip)
        if isinstance(ip, ipaddress.IPv4Address):
            send_func = self.send_arp_request
        elif isinstance(ip, ipaddress.IPv6Address):
            # print("flood ndp request: "+dst_ip)
            send_func = self.send_ndp_request
        else:
            logging.error("dst_ip is not ipv4 neither ipv6.")
            return
        for port in self.edge_ports:
            datapath = self.datapaths[port.dpid]
            send_func(datapath, port.port_no, dst_ip)

    def send_ndp_reply(self, datapath, port_no, dst_mac, dst_ipv6):
        # 构造ndp请求报文
        eth = ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_IPV6,
            dst=dst_mac,
            src=self.controller_mac)

        v6 = ipv6(src=self.controller_ipv6, dst=dst_ipv6, nxt=inet.IPPROTO_ICMPV6)

        ic = icmpv6.icmpv6(
            type_=icmpv6.ND_NEIGHBOR_ADVERT,
            data=icmpv6.nd_neighbor(dst=self.controller_ipv6,
                                    option=icmpv6.nd_option_sla(hw_src=self.controller_mac)))

        pkt = packet.Packet()
        pkt.add_protocol(eth)
        pkt.add_protocol(v6)
        pkt.add_protocol(ic)
        pkt.serialize()
        self.send_packet_output_port(datapath, port_no, pkt.data)

    def send_ndp_request(self, datapath, port_no, dst_ipv6):
        # 构造ndp请求报文
        eth = ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_IPV6,
            dst='ff:ff:ff:ff:ff:ff',  # 目标MAC地址为广播地址
            src=self.controller_mac  # 控制器的MAC地址
        )
        v6 = ipv6(src=self.controller_ipv6, dst=dst_ipv6, nxt=inet.IPPROTO_ICMPV6)
        ic = icmpv6.icmpv6(
            type_=icmpv6.ND_NEIGHBOR_SOLICIT,
            data=icmpv6.nd_neighbor(dst=dst_ipv6, option=icmpv6.nd_option_sla(hw_src=self.controller_mac)))

        pkt = packet.Packet()
        pkt.add_protocol(eth)
        pkt.add_protocol(v6)
        pkt.add_protocol(ic)
        pkt.serialize()
        self.send_packet_output_port(datapath, port_no, pkt.data)

    def send_arp_request(self, datapath, port_no, dst_ip):
        # 构造arp请求报文
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst='ff:ff:ff:ff:ff:ff',  # 目标MAC地址为广播地址
            src=self.controller_mac  # 控制器的MAC地址
        ))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=self.controller_mac,  # 控制器的MAC地址
            src_ip=self.controller_ip,  # 控制器的IP地址
            dst_mac='00:00:00:00:00:00',  # 目标MAC地址暂时设置为全0
            dst_ip=dst_ip  # 目标IP地址
        ))
        pkt.serialize()
        self.send_packet_output_port(datapath, port_no, pkt.data)

    def send_packet_output_port(self, datapath, port_no, data):
        actions = [datapath.ofproto_parser.OFPActionOutput(port_no)]
        self._send_packet_out(datapath, data, actions)

    def _send_packet_out(self, datapath, data, actions):
        if datapath.ofproto.OFP_VERSION == ofproto_v1_0.OFP_VERSION:
            datapath.send_packet_out(actions=actions, data=data)
        elif datapath.ofproto.OFP_VERSION >= ofproto_v1_4.OFP_VERSION:
            out = datapath.ofproto_parser.OFPPacketOut(
                datapath=datapath, in_port=datapath.ofproto.OFPP_CONTROLLER,
                buffer_id=datapath.ofproto.OFP_NO_BUFFER, actions=actions,
                data=data)
            datapath.send_msg(out)

    def send_slice_stats_request(self, datapath, port_no):
        for i in range(N_SLICES):
            self.send_meter_stats_request(datapath, port_no * N_SLICES + i)

    def send_meter_stats_request(self, datapath, meter_id):
        parser = datapath.ofproto_parser

        req = parser.OFPMeterStatsRequest(datapath, 0, meter_id)
        datapath.send_msg(req)
        with self.meter_stats_cv:
            self.meter_stats_cv.wait(self.WATING_CV)

    @set_ev_cls(ofp_event.EventOFPMeterStatsReply, MAIN_DISPATCHER)
    def meter_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        try:
            for stat in ev.msg.body:
                meter_id = stat.meter_id
                pkts_dropped = 0
                bytes_dropped = 0
                pkts_cnt = stat.packet_in_count
                bytes_cnt = stat.byte_in_count
                duration = stat.duration_sec + stat.duration_nsec * 1e-9

                for band_stat in stat.band_stats:
                    pkts_dropped += band_stat.packet_band_count
                    bytes_dropped += band_stat.byte_band_count

                if self.enable_slice:
                    port_no = int(meter_id / N_SLICES)
                    slice_id = meter_id % N_SLICES
                else:
                    port_no = meter_id
                    slice_id = 0

                port = self.src2link[dpid].get(port_no)
                if port != None:
                    link_info = port['link_info']
                    slice_info = link_info.slices_info[slice_id]
                    slice_info.set_stats(pkts_cnt, bytes_cnt, pkts_dropped, bytes_dropped, duration)

        finally:
            with self.meter_stats_cv:
                self.meter_stats_cv.notify_all()

    def install_push_srv6_tcp_udp_flow(self, datapath, path: list, ipv6_src, ipv6_dst, trans_src, trans_dst,
                                       flow_priority):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        # 添加ipv6_dst到sidlist
        e_ipv6_dst = ipaddress.IPv6Address(ipv6_dst).exploded
        # 将IPv6地址转化为8个元素的列表
        dst_list = [[int(num, 16) for num in e_ipv6_dst.split(':')]]
        # 将dpid转化为ipv6
        new_sidlist = dst_list + [dpid2ipv6(dpid) for dpid in path[0:][::-1]]

        if trans_src == 0 and trans_dst != 0:

            match_udp = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ipv6_src=ipv6_src,
                ipv6_dst=ipv6_dst,
                ip_proto=IPPROTO_UDP,
                udp_dst=trans_dst)

            match_tcp = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ipv6_src=ipv6_src,
                ipv6_dst=ipv6_dst,
                ip_proto=IPPROTO_TCP,
                tcp_dst=trans_dst)
        elif trans_src != 0 and trans_dst == 0:

            match_udp = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ipv6_src=ipv6_src,
                ipv6_dst=ipv6_dst,
                ip_proto=IPPROTO_UDP,
                udp_src=trans_src)

            match_tcp = parser.OFPMatch(
                eth_type=ETH_TYPE_IPV6,
                ipv6_src=ipv6_src,
                ipv6_dst=ipv6_dst,
                ip_proto=IPPROTO_TCP,
                tcp_src=trans_src)
        else:
            return

        push_srv6 = ofproto_v1_4_parser.OFPActionPushSrv6(new_sidlist)
        actions = [push_srv6]

        inst = [ofproto_v1_4_parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions),
                ofproto_v1_4_parser.OFPInstructionGotoTable(table_id=FORWARD_TABLE_ID)]

        for match in [match_udp, match_tcp]:
            mod = ofproto_v1_4_parser.OFPFlowMod(
                datapath=datapath,
                table_id=PUSH_SRV6_TABLE_ID,
                priority=flow_priority,
                match=match,
                instructions=inst
            )
            datapath.send_msg(mod)

    def add_service_flow(self, dpid, in_port, ipv4_src, ipv4_dst, ip_proto, src_trans_port, dst_trans_port,
                         hard_timeout, out_port, slice_id):
        datapath = self.datapaths[dpid]
        if datapath == None:
            with open("error.log", "w") as file:
                file.write(f'None datapth: dpid={dpid}')
            raise Exception

        eth_type = 0x0800
        ofp_parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        if (ip_proto == 0x06):  # TCP
            if src_trans_port == 0 and dst_trans_port != 0:
                match = ofp_parser.OFPMatch(in_port=in_port, eth_type=eth_type, ipv4_src=ipv4_src, ipv4_dst=ipv4_dst,
                                            ip_proto=ip_proto, tcp_dst=dst_trans_port)
            elif src_trans_port != 0 and dst_trans_port == 0:
                match = ofp_parser.OFPMatch(in_port=in_port, eth_type=eth_type, ipv4_src=ipv4_src, ipv4_dst=ipv4_dst,
                                            ip_proto=ip_proto, tcp_src=src_trans_port)

        elif ip_proto == 0X11:  # UDP
            if src_trans_port == 0 and dst_trans_port != 0:
                match = ofp_parser.OFPMatch(in_port=in_port, eth_type=eth_type, ipv4_src=ipv4_src, ipv4_dst=ipv4_dst,
                                            ip_proto=ip_proto, udp_dst=dst_trans_port)
            elif src_trans_port != 0 and dst_trans_port == 0:
                match = ofp_parser.OFPMatch(in_port=in_port, eth_type=eth_type, ipv4_src=ipv4_src, ipv4_dst=ipv4_dst,
                                            ip_proto=ip_proto, udp_src=src_trans_port)
        if (slice_id == -1):
            actions = [ofp_parser.OFPActionOutput(out_port)]
            inst = [ofp_parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        elif slice_id < 4:
            meter_inst = ofp_parser.OFPInstructionMeter(meter_id=(slice_id + out_port * N_SLICES))
            actions = [ofp_parser.OFPActionOutput(out_port)]
            if self.enable_slice:
                inst = [meter_inst, ofp_parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            else:
                inst = [ofp_parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        self.send_flow_mod(datapath, match, inst, hard_timeout=hard_timeout, priority=PRIORITY_SERVICE)

    def get_net_topo(self):
        return self.net_topo

    def get_link_delay(self, sw1, sw2):
        return self.net_topo.edges[sw1, sw2]['link_info'].delay

    def get_link_throughput(self, sw1, sw2):
        print(self.net_topo.edges[sw1, sw2]['link_info'].throughput)
        return self.net_topo.edges[sw1, sw2]['link_info'].throughput

    def get_link_loss(self, sw1, sw2):
        return self.net_topo.edges[sw1, sw2]['link_info'].loss

    def get_link_bandwidth(self, sw1, sw2):
        return self.net_topo.edges[sw1, sw2]['link_info'].bandwidth

    def get_slices_loss(self, sw1, sw2):
        return [slice_info.loss for slice_info in self.net_topo.edges[sw1, sw2]['link_info'].slices_info]

    def get_port_slices_slots(self, port):
        return self.all_ports_slots[(port.dpid, port.port_no)]

    def get_slices_bandwidth(self, sw1, sw2):
        port_no = self.net_topo.edges[sw1, sw2]['link_info'].src_port_no
        return self.ports_slices_bandwidths[(sw1, port_no)]

    def get_slices_throughput(self, sw1, sw2):
        return [slice_info.throughput for slice_info in self.net_topo.edges[sw1, sw2]['link_info'].slices_info]

    def set_slices_config_by_slots(self, sw1, sw2, slots):
        port = self.net_topo.edges[sw1, sw2]['link_info'].src_port_no
        datapath = self.datapaths[sw1]
        self.send_set_slice_config_request(datapath, port, slots)

    def set_port_slices_config_by_slots(self, port, slots):
        datapath = self.datapaths[port.dpid]
        self.send_set_slice_config_request(datapath, port.port_no, slots)

    def get_port_connectto_host(self, ip):
        return self.host_ip2sw_port.get(ip)

    def get_datapath(self, dpid):
        return ofctl_api.get_datapath(self, dpid)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """
        处理端口状态变化事件-----链路中断模拟
        """
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        if msg.reason == ofp.OFPPR_DELETE:
            start_time = time.time()
            self.logger.info(f"事件由控制器接收的时间：{start_time}秒")
            self.failover_start_time = start_time
            # 端口被删除
            port_no = msg.desc.port_no
            port_name = msg.desc.name.decode("utf-8")
            self.logger.info("Port deleted on switch %s: port_no=%s", dp.id, port_no)
            self.logger.warning(f"检测到链路 DOWN: {port_name}")
            # 区间链路故障
            if dp.id == 14:
                backup_segments = self.backup_segments_map[('2000:db8::1', '2000:db8::2')]
                print("检测到链路区间故障，下发全局备份路径流表：", [1, 3, 5, 6])
                self.add_srv6_flow_cross_region(backup_segments, '2000:db8::1', '2000:db8::2', 105)
            if dp.id == 7 and port_name == 'sw7-sw86':
                backup_segments = self.backup_segments_map[('2000:db8::1', '2000:db8::3')]
                print("检测到链路区间故障，下发全局备份路径流表：", [1, 15, 8, 12, 18, 17, 11, 50, 34, 33, 42, 84, 87, 95])
                self.add_srv6_flow_cross_region(backup_segments, '2000:db8::1', '2000:db8::3', 105)

            #区域内故障切换
            if dp.id != 14:
                self.handle_link_failure(port_name)
            # print(self.parse_switch_pair(port_name))


        # elif msg.reason == ofp.OFPPR_ADD:
        #     # 端口被添加
        #     self.logger.info("Port added on switch %s: port_no=%s", dp.id, msg.desc.port_no)
        # elif msg.reason == ofp.OFPPR_MODIFY:
        #     # 端口状态被修改
        #     self.logger.info("Port modified on switch %s: port_no=%s", dp.id, msg.desc.port_no)

    def flatten_sidlist(self, sidlist):
        flat_list = []
        for sid in sidlist:
            for num in sid:
                # 将 16 位整数拆分为高 8 位和低 8 位
                high_byte = (num >> 8) & 0xFF  # 高 8 位
                low_byte = num & 0xFF  # 低 8 位
                flat_list.append(high_byte)
                flat_list.append(low_byte)
        return flat_list

    def split_into_chunks(self, flat_list, chunk_size):
        """
        将扁平列表划分为指定大小的子列表。
        :param flat_list: 扁平列表
        :param chunk_size: 每个子列表的大小
        :return: 嵌套列表
        """
        return [flat_list[i:i + chunk_size] for i in range(0, len(flat_list), chunk_size)]

    def install_srv6_flow(self, datapath, path, ipv6_src, ipv6_dst, priority, node_dst):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        # 匹配条件
        match = parser.OFPMatch(
            eth_type=0x86dd,
            ipv6_src=ipv6_src,
            ipv6_dst=ipv6_dst,
            ip_proto=inet.IPPROTO_ICMPV6
        )
        # 添加ipv6_dst到sidlist
        e_ipv6_dst = ipaddress.IPv6Address(node_dst).exploded
        # 将IPv6地址转化为8个元素的列表
        dst_list = [[int(num, 16) for num in e_ipv6_dst.split(':')]]
        # 将dpid转化为ipv6
        new_sidlist = dst_list + [dpid2ipv6(dpid) for dpid in path[::-1]]

        # 动作和指令
        # 构建动作列表
        actions = []
        push_srv6_action = ofproto_v1_4_parser.OFPActionPushSrv6(new_sidlist)
        actions.append(push_srv6_action)
        # 添加将数据包转发到二级流表的动作
        # 创建流表规则
        inst = [ofproto_v1_4_parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions),
                ofproto_v1_4_parser.OFPInstructionGotoTable(table_id=1)]
        # 流表项
        mod = ofproto_v1_4_parser.OFPFlowMod(
            datapath=datapath,
            table_id=0,
            priority=priority,
            match=match,
            instructions=inst,
            # cookie=cookie
        )
        # # 仅对这两个目标地址启用 Barrier 保护
        # barrier_sensitive_dsts = {"2000:db8::2", "2000:db8::3"}

        # 在你处理每个段或每个 flow-mod 下发时：
        # 发送流表规则给交换机
        install_time = time.time()
        if priority == 105:
            self.logger.info(f"记录下发时间： {install_time}秒")  #控制器处理事件消耗的时间
        datapath.send_msg(mod)
        if priority == 105:
            barrier = parser.OFPBarrierRequest(datapath)
            datapath.send_msg(barrier)

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def barrier_reply_handler(self, ev):
        confirm_time = time.time()
        self.logger.info(f"Flow-Mod confirmed at: {confirm_time}秒")
        self.logger.info(f"路由修复时间：{(confirm_time - self.failover_start_time) * 1000:.3f} 毫秒")


    def delete_flows_by_cookie(self, datapath, cookie, priority=None):
        start_time = time.time()
        """
        删除特定 cookie 的流表项
        :param datapath: 交换机的 datapath 对象
        :param cookie: 要删除的流表项的 cookie 值
        :param priority: 可选参数，指定要删除的流表项的优先级
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 创建 FlowMod 消息，删除特定 cookie 的流表项
        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=0,  # 所有表
            command=ofproto.OFPFC_DELETE,  # 删除命令
            out_port=ofproto.OFPP_ANY,  # 任意端口
            out_group=ofproto.OFPG_ANY,  # 任意组
            cookie=cookie,  # 目标 cookie
            cookie_mask=0xFFFFFFFFFFFFFFFF,  # 匹配完整的 cookie
            priority=priority if priority is not None else 0  # 可选：指定优先级
        )

        # 发送 FlowMod 消息
        datapath.send_msg(mod)
        self.logger.info(f"已删除 cookie={cookie} 的流表项")
        end_time = time.time()
        self.logger.info(f"删除SRv6流表执行时间：{end_time - start_time}秒")
        self.logger.info(f"故障修复时间：{end_time}秒")

    def add_fast_failover_group(self, datapath, group_id, watch_ports_actions):
        """
        添加快速故障转移(FF)组到交换机
        :param datapath: 交换机的datapath对象
        :param group_id: 组ID
        :param watch_ports_actions: 包含监视端口和动作的列表，格式为[(watch_port, output_port), ...]
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 创建bucket列表
        buckets = []
        for watch_port, output_port in watch_ports_actions:
            # 创建动作(输出到指定端口)
            actions = [parser.OFPActionOutput(output_port)]

            # 创建bucket，设置监视端口和动作
            bucket = parser.OFPBucket(
                watch_port=watch_port,
                watch_group=ofproto.OFPG_ANY,
                actions=actions)
            buckets.append(bucket)

        # 创建组修改消息
        req = parser.OFPGroupMod(
            datapath=datapath,
            command=ofproto.OFPGC_ADD,
            type_=ofproto.OFPGT_FF,
            group_id=group_id,
            buckets=buckets)

        # 发送消息到交换机
        datapath.send_msg(req)

    def add_srv6_flow_cross_region(self, segments, ipv6_src, ipv6_dst, priority, seg_id = None):
        #用于备份路径（局部路径修改）
        if len(segments) == 1:
            segment = segments[0]
            total_segments = len(self.main_segments_map[(ipv6_src, ipv6_dst)])
            if seg_id == total_segments - 1:  #最后一段（一般不考虑只有1段的情况）
                self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src,
                                       ":".join(f"{x:04x}" for x in dpid2ipv6(segment[0])), priority, ipv6_dst)
            elif seg_id == 0:
                self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src, ipv6_dst, priority,
                                       ":".join(f"{x:04x}" for x in dpid2ipv6(segment[-1])))
            else:
                self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src,
                                       ":".join(f"{x:04x}" for x in dpid2ipv6(segment[0])), priority,
                                       ":".join(f"{x:04x}" for x in dpid2ipv6(segment[-1])))

        else:
            for i, segment in enumerate(segments):
                if i == len(segments) - 1:
                    self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src,
                                           ":".join(f"{x:04x}" for x in dpid2ipv6(segment[0])), priority, ipv6_dst)
                elif i == 0:
                    self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src, ipv6_dst, priority,
                                           ":".join(f"{x:04x}" for x in dpid2ipv6(segment[-1])))
                else:
                    self.install_srv6_flow(self.datapaths.get(segment[0]), segment[1:-1], ipv6_src,
                                           ":".join(f"{x:04x}" for x in dpid2ipv6(segment[0])), priority,
                                           ":".join(f"{x:04x}" for x in dpid2ipv6(segment[-1])))


    def compute_main_and_backup_paths_with_hosts(self, src_ip, dst_ip):
        if src_ip not in self.host_ip2sw_port or dst_ip not in self.host_ip2sw_port:
            self.logger.warning("IP not found in host_ip2sw_port")
            return None, None
        threshold = 2
        src_sw, _ = self.host_ip2sw_port[src_ip]
        dst_sw, _ = self.host_ip2sw_port[dst_ip]
        paths = []
        gen = nx.shortest_simple_paths(self.net_topo, src_sw, dst_sw)
        for _ in range(4):
            try:
                paths.append(next(gen))
            except StopIteration:
                break
        main_path = paths[0]
        backup_path = paths[1]
        for p in paths:
            overlap = len(set(p) & set(main_path))
            if overlap <= threshold:
                backup_path = p
                break
        # print("主路径:", main_path)
        # print("备份路径:", backup_path)
        return main_path, backup_path

    def compute_main_and_backup_paths_in_local_region(self, src_sw, dst_sw, segment_main_path):
        threshold = 2
        paths = []
        gen = nx.shortest_simple_paths(self.net_topo, src_sw, dst_sw)
        for _ in range(4):
            try:
                paths.append(next(gen))
            except StopIteration:
                break
        main_path = segment_main_path
        backup_path = paths[0]
        for p in paths:
            if p != main_path and len(set(p) & set(main_path)) <= threshold:
                backup_path = p
                break
        # print(f"sw{src_sw}-sw{dst_sw}区域内主路径:", main_path)
        # print(f"sw{src_sw}-sw{dst_sw}区域内备份路径:", backup_path)
        return main_path, backup_path


    def extract_transition_segments(self, path, region_map, dest_ip):
        # 建立 node → 区域名 映射
        node_to_region = {}
        for region, nodes in region_map.items():
            for node in nodes:
                node_to_region[node] = region

        segments = []
        current_region = node_to_region.get(path[0], "未知区域")
        segment_start_idx = 0

        for i in range(1, len(path)):
            node = path[i]
            region = node_to_region.get(node, "未知区域")

            if region != current_region:
                # 从 segment_start_idx 到 i 的段为一个“跨区域段”
                segment = path[segment_start_idx:i + 1]
                segments.append(segment)
                segment_start_idx = i
                current_region = region

        # 最后一段添加目标主机 IP
        last_segment = path[segment_start_idx:]
        if last_segment:
            last_segment = last_segment + [dest_ip]
            segments.append(last_segment)

        return segments

    def compute_and_setup_paths(self, src_ipv6, dst_ipv6, priority=101):
        """
        计算并设置跨区域的主路径和备份路径，以及相关的段映射和本地路径。
        :param src_ipv6: 源 IPv6 地址
        :param dst_ipv6: 目的 IPv6 地址
        :param priority:  SRV6流表优先级，默认为 101
        """
        # 计算主路径与备份路径
        main_path, backup_path = self.compute_main_and_backup_paths_with_hosts(src_ipv6, dst_ipv6)
        # if src_ipv6 == '2000:db8::1' and dst_ipv6 == '2000:db8::2':
        #     main_path = [1, 8, 12, 18, 14, 10, 6]
        #     backup_path = [1, 3, 5, 6]
        #     print("全局主路径：", main_path)
        #     print("全局备份路径：", backup_path)
        if src_ipv6 == '2000:db8::1' and dst_ipv6 == '2000:db8::3':
            main_path = [1, 7, 86, 87, 95]
            #main_path = [1, 15, 8, 12, 18, 17, 11, 50, 34, 33, 42, 84, 87, 95]
            #backup_path = [1, 3, 5, 6]
            backup_path = [1, 15, 7, 86, 87, 95]
            print("全局主路径：", main_path)
            print("全局备份路径：", backup_path)
        # 提取区域转换段
        main_segments = self.extract_transition_segments(main_path, self.region_map, dst_ipv6)
        backup_segments = self.extract_transition_segments(backup_path, self.region_map, dst_ipv6)

        # 保存主路径段映射
        self.main_segments_map[(src_ipv6, dst_ipv6)] = main_segments

        #保存备份路径段映射
        self.backup_segments_map[(src_ipv6, dst_ipv6)] = backup_segments

        # 初始化 link_to_segment_map
        self.link_to_segment_map[(src_ipv6, dst_ipv6)] = {}

        # 构建 link_to_segment_map 和各段的本地路径
        for idx, segment in enumerate(main_segments):
            # print(f"段 {idx}: {segment}")
            # 区域内链路标记
            for i in range(len(segment) - 2):
                u, v = segment[i], segment[i + 1]
                key_uv = f"sw{u}-sw{v}"
                key_vu = f"sw{v}-sw{u}"
                self.link_to_segment_map[(src_ipv6, dst_ipv6)][key_uv] = idx
                self.link_to_segment_map[(src_ipv6, dst_ipv6)][key_vu] = idx

            # 计算本地区域的主/备份路径
            main_local, backup_local = self.compute_main_and_backup_paths_in_local_region(
                segment[0], segment[-2], segment[0:-1]
            )
            if idx == 0 and (src_ipv6 == '2000:db8::1' and dst_ipv6 == '2000:db8::3'):
                main_local = [1, 7]
                backup_local = [1, 15, 7]
            # 存储本地路径
            self.local_paths.setdefault((src_ipv6, dst_ipv6), {})[idx] = {
                "main": main_local,
                "backup": backup_local
            }

        # 添加 SRv6 跨区域流表规则
        self.add_srv6_flow_cross_region(main_segments, src_ipv6, dst_ipv6, priority)

        # 回来的路径，比如host2----->host1
        reverse_backup_path = list(reversed(backup_path))
        reverse_segments = self.extract_transition_segments(reverse_backup_path, self.region_map, src_ipv6)
        # for idx, seg in enumerate(reverse_segments):
        #     #print(f"段 {idx}: {seg}")
        self.add_srv6_flow_cross_region(reverse_segments, dst_ipv6, src_ipv6, 102)

    def handle_link_failure(self, failed_link):
        for (src_ip, dst_ip), link_map in self.link_to_segment_map.items():
            seg_id = link_map.get(failed_link)
            if seg_id is None:
                continue
            backup_path = self.local_paths[(src_ip, dst_ip)][seg_id]["backup"] + [self.main_segments_map[(src_ip, dst_ip)][seg_id][-1]]
            print("故障切换后下发备份路径流表：", backup_path[0:-1])
            self.add_srv6_flow_cross_region([backup_path], src_ip, dst_ip, 105, seg_id)

    def find_inter_region_links_directed(self, links):
        """返回所有有向跨区域链路列表"""
        return [(u, v) for u, v in links if self.inv_region[u] != self.inv_region[v]]

    def count_onehop_hops(self, node, target_region):
        """
        统计 node 到 target_region 的一跳出路：
          direct_hops   : 直接邻居属于 target_region
          fallback_hops : 同一区域邻居自身有直链到 target_region
        """
        direct_hops = []
        fallback_hops = []
        for nbr in self.adj[node]:
            if self.inv_region[nbr] == target_region:
                direct_hops.append(nbr)
            elif (self.inv_region[nbr] == self.inv_region[node] and
                  any(self.inv_region[x] == target_region for x in self.adj[nbr])):
                fallback_hops.append(nbr)
        return direct_hops, fallback_hops

    def init_inter_region_edge_group(self, u: int, v: int):
        """
        为有向链路 u->v 决定组表成员，并把 direct/fallback 分别存入
        self.inter_region_edge_group[(u, v)]：
          1. 若 direct_hops 长度 >= 2，则 members = direct_hops；
          2. 若 direct_hops 长度 == 1 且 fallback_hops 非空，则 members = direct_hops + fallback_hops；
          3. 否则 members = []；
        同时，保证存入的 direct 列表中，u->v 这条链路（即 v）总是在第一位。
        """
        # 1. 计算 direct_hops 和 fallback_hops（列表内元素为下一个跳节点 ID）
        target_region = self.inv_region[v]
        direct, fallback = self.count_onehop_hops(u, target_region)

        # 2. 如果 direct 中包含 v，就把 v 放到最前面
        if v in direct:
            ordered_direct = [v] + [x for x in direct if x != v]
        else:
            ordered_direct = direct.copy()

        # 3. 同理，也可对 fallback 做同样操作（如果有需求）
        if v in fallback:
            ordered_fallback = [v] + [x for x in fallback if x != v]
        else:
            ordered_fallback = fallback.copy()

        # 4. 持久化存储 direct/fallback（已保证 v 优先）
        self.inter_region_edge_group.setdefault((u, v), {})
        self.inter_region_edge_group[(u, v)]["direct"] = ordered_direct
        self.inter_region_edge_group[(u, v)]["fallback"] = ordered_fallback

    def get_direct_ports_from_source(self, u, direct_nodes):
        """根据 (u, v) 获取起点 u 到 direct 路径中每个节点的端口名和编号"""
        sw_from = u
        results = []
        port_nos = []

        for sw_to in direct_nodes:
            port_name_prefix = f"sw{sw_from}-sw{sw_to}"
            # 找 sw_from 交换机
            for sw in self.all_switches:
                if sw.dp.id == sw_from:
                    for port in sw.ports:
                        if port.name.decode().startswith(port_name_prefix):
                            results.append({
                                "from": sw_from,
                                "to": sw_to,
                                "port_name": port.name,
                                "port_no": port.port_no
                            })
                            port_nos.append(port.port_no)

        # 打印结果
        for res in results:
            self.logger.info(
                f"[sw{res['from']} -> sw{res['to']}] "
                f"Port Name: {res['port_name']}, Port No: {res['port_no']}"
            )
        return port_nos

    def clear_all_groups(self, dp):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        msg = parser.OFPGroupMod(
            datapath=dp,
            command=ofproto.OFPGC_DELETE,
            type_=0,             # 忽略
            group_id=ofproto.OFPG_ALL,  # 表示所有组
            buckets=[]
        )
        dp.send_msg(msg)

    def parse_switch_pair(self, s):
        """
        将形如 'sw4-sw6' 的字符串解析为 (4, 6)
        """
        parts = s.replace("sw", "").split("-")
        return tuple(map(int, parts))


