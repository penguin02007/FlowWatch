"""Static description of the demo network.

Shared by the generator (live NetFlow v9 / IPFIX export), the collector
(enrichment) and the backfill (synthetic history), so all three agree on
sites, interfaces, applications and traffic patterns.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

MBPS = 1_000_000


@dataclass(frozen=True)
class Interface:
    index: int
    name: str
    description: str
    capacity_bps: int
    role: str


@dataclass(frozen=True)
class Exporter:
    name: str
    protocol: str  # "netflow_v9" | "ipfix"
    domain_id: int  # v9 source_id / IPFIX observation domain id
    site: str
    interfaces: tuple[Interface, ...]

    def interface(self, index: int) -> Interface | None:
        return next((i for i in self.interfaces if i.index == index), None)


EXPORTERS: tuple[Exporter, ...] = (
    Exporter(
        name="edge-rtr-01",
        protocol="netflow_v9",
        domain_id=101,
        site="HQ",
        interfaces=(
            Interface(1, "Gi0/0/0", "WAN-ISP-A internet uplink", 1000 * MBPS, "wan-internet"),
            Interface(2, "Gi0/0/1", "MPLS circuit to DC", 500 * MBPS, "wan-mpls"),
            Interface(3, "Te0/1/0", "HQ LAN core", 10_000 * MBPS, "lan"),
        ),
    ),
    Exporter(
        name="dc-core-01",
        protocol="ipfix",
        domain_id=201,
        site="DC",
        interfaces=(
            Interface(10, "Eth1/1", "DCI link to DR site", 1000 * MBPS, "dci"),
            Interface(12, "Eth1/10", "DC server farm", 10_000 * MBPS, "lan"),
        ),
    ),
)
EXPORTERS_BY_NAME = {e.name: e for e in EXPORTERS}
EXPORTERS_BY_DOMAIN = {e.domain_id: e for e in EXPORTERS}

SITES: dict[str, str] = {
    "HQ": "10.10.0.0/16",
    "DC": "10.30.0.0/16",
    "DR": "10.40.0.0/16",
}
_SITE_NETS = [(name, ipaddress.ip_network(cidr)) for name, cidr in SITES.items()]


def site_of(ip: str) -> str:
    addr = ipaddress.ip_address(ip)
    for name, net in _SITE_NETS:
        if addr in net:
            return name
    return "internet"


@dataclass(frozen=True)
class AppRule:
    name: str
    transport: str
    ports: tuple[int, ...]
    networks: tuple[str, ...] = ()  # when set, one endpoint must be inside these networks
    description: str = ""


# Ordered: first match wins, so specific (port + network) rules come first.
APPLICATIONS: tuple[AppRule, ...] = (
    AppRule("microsoft-365", "tcp", (443,), ("52.96.0.0/14",), "Exchange Online / SharePoint / OneDrive"),
    AppRule("salesforce-crm", "tcp", (443,), ("13.110.0.0/16",), "SaaS CRM"),
    AppRule("windows-update", "tcp", (80,), ("13.107.4.0/24",), "Microsoft update CDN, pulled by WSUS server 10.10.0.80"),
    AppRule("video-conferencing", "udp", (3478, 3479, 3480, 3481), (), "Teams/Zoom real-time media"),
    AppRule("erp-database", "tcp", (1433,), (), "ERP SQL Server in DC (10.30.1.10)"),
    AppRule("erp-web", "tcp", (8443,), (), "ERP web tier in DC (10.30.1.20)"),
    AppRule("file-share-smb", "tcp", (445,), (), "Windows file server in DC (10.30.2.10)"),
    AppRule("gpu-checkpoint-nfs", "tcp", (2049,), (),
            "NFS on the DC storage array (10.30.3.10). The HQ GPU training cluster (10.10.9.11-14) writes "
            "model checkpoints there around the clock; training stalls when those writes slow down"),
    AppRule("directory-ldap", "tcp", (389, 636), (), "Active Directory"),
    AppRule("backup-rsync", "tcp", (873,), (), "Nightly rsync backup DC->DR, change calendar: 01:00-03:00 site time"),
    AppRule("sql-replication", "tcp", (5022,), (), "SQL AlwaysOn replication DC->DR (RPO depends on it)"),
    AppRule("storage-replication", "tcp", (3260,), (), "iSCSI storage replication DC->DR"),
    AppRule("dns", "udp", (53,), (), "DNS"),
    AppRule("web-https", "tcp", (443,), (), "General web / HTTPS"),
    AppRule("web-http", "tcp", (80,), (), "General web / HTTP"),
)
_APP_NETS = {a.name: [ipaddress.ip_network(n) for n in a.networks] for a in APPLICATIONS}
KNOWN_PORTS = {p for a in APPLICATIONS for p in a.ports}


def service_port(src_port: int, dst_port: int) -> int:
    if dst_port in KNOWN_PORTS:
        return dst_port
    if src_port in KNOWN_PORTS:
        return src_port
    return min(src_port, dst_port)


def classify(transport: str, port: int, src_ip: str, dst_ip: str) -> str:
    for app in APPLICATIONS:
        if app.transport != transport or port not in app.ports:
            continue
        nets = _APP_NETS[app.name]
        if nets and not any(
            ipaddress.ip_address(ip) in net for ip in (src_ip, dst_ip) for net in nets
        ):
            continue
        return app.name
    return f"other-{transport}-{port}"


@dataclass(frozen=True)
class Conversation:
    """A client/server pair observed by one exporter.

    ``up`` is client->server, ``down`` is server->client. ``client_if`` is the
    exporter interface facing the client, ``server_if`` the one facing the server.
    """

    client: str
    server: str
    port: int
    transport: str
    down_mbps: float
    up_mbps: float
    profile: str  # business | flat | nightly
    exporter: str
    client_if: int
    server_if: int
    growth_per_day: float = 0.0


def _hq_internet(client, server, port, down, up, profile="business", transport="tcp", growth=0.0):
    return Conversation(client, server, port, transport, down, up, profile, "edge-rtr-01", 3, 1, growth)


def _hq_dc(client, server, port, down, up, profile="business"):
    return Conversation(client, server, port, "tcp", down, up, profile, "edge-rtr-01", 3, 2)


def _dc_dr(client, server, port, down, up, profile="flat"):
    return Conversation(client, server, port, "tcp", down, up, profile, "dc-core-01", 12, 10)


BASELINE: tuple[Conversation, ...] = (
    # HQ <-> Internet (edge-rtr-01 Gi0/0/0, 1 Gbps)
    _hq_internet("10.10.1.21", "142.250.72.14", 443, 30, 2),
    _hq_internet("10.10.1.22", "151.101.1.69", 443, 30, 2),
    _hq_internet("10.10.1.23", "104.16.132.229", 443, 30, 2),
    _hq_internet("10.10.1.24", "142.250.72.14", 443, 30, 2),
    _hq_internet("10.10.1.25", "151.101.1.69", 443, 30, 2),
    _hq_internet("10.10.1.26", "104.16.132.229", 443, 30, 2),
    _hq_internet("10.10.3.15", "142.250.64.78", 443, 20, 1),
    _hq_internet("10.10.1.30", "52.96.12.10", 443, 45, 8),
    _hq_internet("10.10.1.31", "52.96.12.10", 443, 45, 8),
    _hq_internet("10.10.2.40", "52.96.12.10", 443, 45, 8),
    _hq_internet("10.10.2.70", "13.110.5.5", 443, 12, 3),
    _hq_internet("10.10.1.50", "52.112.4.5", 3478, 25, 18, transport="udp", growth=0.04),
    _hq_internet("10.10.1.51", "52.112.4.5", 3478, 25, 18, transport="udp", growth=0.04),
    _hq_internet("10.10.1.52", "52.112.4.5", 3478, 25, 18, transport="udp", growth=0.04),
    _hq_internet("10.10.2.60", "52.112.4.5", 3478, 25, 18, transport="udp", growth=0.04),
    _hq_internet("10.10.0.53", "8.8.8.8", 53, 0.6, 0.4, profile="flat", transport="udp"),
    _hq_internet("10.10.0.80", "13.107.4.50", 80, 6, 0.3, profile="flat"),
    # HQ <-> DC over MPLS (edge-rtr-01 Gi0/0/1, 500 Mbps)
    _hq_dc("10.10.1.21", "10.30.1.10", 1433, 18, 4),
    _hq_dc("10.10.1.30", "10.30.1.10", 1433, 18, 4),
    _hq_dc("10.10.2.40", "10.30.1.10", 1433, 18, 4),
    _hq_dc("10.10.1.22", "10.30.1.20", 8443, 25, 5),
    _hq_dc("10.10.2.70", "10.30.1.20", 8443, 25, 5),
    _hq_dc("10.10.1.30", "10.30.2.10", 445, 30, 12),
    _hq_dc("10.10.2.40", "10.30.2.10", 445, 30, 12),
    _hq_dc("10.10.1.24", "10.30.2.10", 445, 30, 12),
    _hq_dc("10.10.0.53", "10.30.0.5", 389, 1.5, 1, profile="flat"),
    # HQ GPU training cluster -> DC NFS checkpoints, 24x7
    _hq_dc("10.10.9.11", "10.30.3.10", 2049, 4, 35, profile="flat"),
    _hq_dc("10.10.9.12", "10.30.3.10", 2049, 4, 35, profile="flat"),
    _hq_dc("10.10.9.13", "10.30.3.10", 2049, 4, 35, profile="flat"),
    _hq_dc("10.10.9.14", "10.30.3.10", 2049, 4, 35, profile="flat"),
    # DC -> DR over DCI (dc-core-01 Eth1/1, 1 Gbps)
    _dc_dr("10.30.5.20", "10.40.5.20", 873, 5, 850, profile="nightly"),
    _dc_dr("10.30.1.10", "10.40.1.10", 5022, 3, 45),
    _dc_dr("10.30.2.10", "10.40.2.10", 3260, 4, 60),
)


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    conversations: tuple[Conversation, ...]


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "smb-bulk-copy",
            "SMB bulk copy saturates MPLS",
            "Engineering workstation 10.10.8.77 pushes a huge dataset to the DC file server over the 500 Mbps MPLS link.",
            (Conversation("10.10.8.77", "10.30.2.10", 445, "tcp", 6, 460, "flat", "edge-rtr-01", 3, 2),),
        ),
        Scenario(
            "backup-overrun",
            "Backup job overruns into business hours",
            "The rsync backup DC->DR runs outside its 01:00-03:00 window and saturates the DCI link.",
            (Conversation("10.30.5.20", "10.40.5.20", 873, "tcp", 5, 850, "flat", "dc-core-01", 12, 10),),
        ),
        Scenario(
            "update-storm",
            "Windows update storm",
            "WSUS 10.10.0.80 pulls a large patch release from the Microsoft CDN over the internet link.",
            (Conversation("10.10.0.80", "13.107.4.50", 80, "tcp", 780, 15, "flat", "edge-rtr-01", 3, 1),),
        ),
        Scenario(
            "data-exfiltration",
            "Suspicious upload to unknown host",
            "HQ host 10.10.3.45 uploads hundreds of Mbps over HTTPS to an unfamiliar external IP.",
            (Conversation("10.10.3.45", "185.220.101.7", 443, "tcp", 4, 320, "flat", "edge-rtr-01", 3, 1),),
        ),
    )
}


@dataclass(frozen=True)
class Incident:
    """A scenario that ran in the past.

    It starts at ``hour``:``minute`` site time, ``days_ago`` days before the backfill
    anchor day. If ``ends_min_before_setup`` is set, it instead ends that many minutes
    before ``es-setup`` first ran, so it is always fully backfilled.
    """

    scenario: str
    duration_min: int
    days_ago: int = 0
    hour: int = 0
    minute: int = 0
    scale: float = 1.0
    ends_min_before_setup: int | None = None


HISTORY_INCIDENTS: tuple[Incident, ...] = (
    # Earlier today: the bulk copy saturates MPLS and starves the GPU cluster's checkpoint writes.
    Incident("smb-bulk-copy", duration_min=80, ends_min_before_setup=30),
    Incident("backup-overrun", duration_min=390, days_ago=3, hour=3, minute=0),
    Incident("update-storm", duration_min=70, days_ago=5, hour=16, minute=0),
    Incident("data-exfiltration", duration_min=25, days_ago=8, hour=10, minute=40, scale=0.4),
)
