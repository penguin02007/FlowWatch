import ipaddress
import os
import socket
import struct
import time

HOST = os.getenv("FLOW_COLLECTOR_HOST", "flow-collector")
PORT = int(os.getenv("FLOW_COLLECTOR_PORT", "2055"))
FLOW_COUNT = int(os.getenv("FLOW_COUNT", "1"))
SEND_INTERVAL = float(os.getenv("SEND_INTERVAL", "1.0"))


def ip_to_int(value: str) -> int:
    ip = ipaddress.ip_address(value)
    return int(ip)


def build_record(idx: int):
    now = int(time.time())
    src_ip = "10.0.0.1"
    dst_ip = "10.0.0.2"
    next_hop = "192.168.0.1"
    return struct.pack(
        "!IIIHHIIIIHHBBBBHHBBH",
        ip_to_int(src_ip),
        ip_to_int(dst_ip),
        ip_to_int(next_hop),
        1 + idx,
        2 + idx,
        10 + idx,
        100 + idx,
        now - 60,
        now,
        12345 + idx,
        443 + idx,
        0,
        0x18,
        6,
        0,
        64512,
        64513,
        24,
        24,
        0,
    )


def build_packet():
    now = int(time.time())
    header = struct.pack(
        "!HHIIII BBH",
        5,
        FLOW_COUNT,
        0,
        now,
        0,
        0,
        0,
        0,
        0,
    )
    records = b"".join(build_record(i) for i in range(FLOW_COUNT))
    packet = header + records
    if len(packet) != 24 + (48 * FLOW_COUNT):
        raise ValueError(f"Invalid NetFlow v5 packet size: {len(packet)} bytes for {FLOW_COUNT} records")
    return packet


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"sending NetFlow v5 packets to {HOST}:{PORT} every {SEND_INTERVAL}s")
    while True:
        packet = build_packet()
        sock.sendto(packet, (HOST, PORT))
        time.sleep(SEND_INTERVAL)


if __name__ == "__main__":
    main()
