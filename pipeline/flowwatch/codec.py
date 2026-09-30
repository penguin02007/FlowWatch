"""Minimal NetFlow v9 (RFC 3954) and IPFIX (RFC 7011) encoder / decoder.

Covers the IPv4 flow fields this demo needs. The decoder is template driven:
it learns templates per (exporter address, domain id) and skips sets it
cannot decode (options data, unknown templates).
"""
from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass

# Information element ids (identical numbering in v9 and IPFIX).
BYTES, PACKETS, PROTOCOL, TCP_FLAGS = 1, 2, 4, 6
SRC_PORT, SRC_ADDR, IN_IF, DST_PORT, DST_ADDR, OUT_IF = 7, 8, 10, 11, 12, 14
V9_LAST_SWITCHED, V9_FIRST_SWITCHED = 21, 22
FLOW_START_MS, FLOW_END_MS = 152, 153

V9_TEMPLATE = (
    (BYTES, 4), (PACKETS, 4), (PROTOCOL, 1), (TCP_FLAGS, 1),
    (SRC_PORT, 2), (SRC_ADDR, 4), (IN_IF, 2), (DST_PORT, 2),
    (DST_ADDR, 4), (OUT_IF, 2), (V9_LAST_SWITCHED, 4), (V9_FIRST_SWITCHED, 4),
)
IPFIX_TEMPLATE = (
    (BYTES, 8), (PACKETS, 8), (PROTOCOL, 1), (TCP_FLAGS, 1),
    (SRC_PORT, 2), (SRC_ADDR, 4), (IN_IF, 4), (DST_PORT, 2),
    (DST_ADDR, 4), (OUT_IF, 4), (FLOW_START_MS, 8), (FLOW_END_MS, 8),
)
TEMPLATE_ID = 256
PROTO_NUM = {"tcp": 6, "udp": 17, "icmp": 1}
PROTO_NAME = {v: k for k, v in PROTO_NUM.items()}


@dataclass
class FlowRecord:
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol: int
    bytes: int
    packets: int
    in_if: int
    out_if: int
    start_ms: int
    end_ms: int
    tcp_flags: int = 0


def _template_set(set_id: int, fields) -> bytes:
    body = struct.pack("!HH", TEMPLATE_ID, len(fields))
    body += b"".join(struct.pack("!HH", fid, flen) for fid, flen in fields)
    return struct.pack("!HH", set_id, 4 + len(body)) + body


def _data_set(fields, records: list[dict]) -> bytes:
    body = b""
    for rec in records:
        for fid, flen in fields:
            value = rec[fid]
            if fid in (SRC_ADDR, DST_ADDR):
                body += ipaddress.IPv4Address(value).packed
            else:
                body += int(value).to_bytes(flen, "big")
    body += b"\x00" * (-(4 + len(body)) % 4)
    return struct.pack("!HH", TEMPLATE_ID, 4 + len(body)) + body


def _record_values(r: FlowRecord) -> dict:
    return {
        BYTES: r.bytes, PACKETS: r.packets, PROTOCOL: r.protocol, TCP_FLAGS: r.tcp_flags,
        SRC_PORT: r.src_port, SRC_ADDR: r.src_ip, IN_IF: r.in_if, DST_PORT: r.dst_port,
        DST_ADDR: r.dst_ip, OUT_IF: r.out_if,
    }


def encode_v9(records, *, source_id, sequence, unix_secs, sys_uptime_ms, with_template) -> bytes:
    rows = []
    for r in records:
        values = _record_values(r)
        # v9 timestamps are sysUptime-relative (ms since the exporter booted).
        now_ms = unix_secs * 1000
        values[V9_LAST_SWITCHED] = (sys_uptime_ms - (now_ms - r.end_ms)) & 0xFFFFFFFF
        values[V9_FIRST_SWITCHED] = (sys_uptime_ms - (now_ms - r.start_ms)) & 0xFFFFFFFF
        rows.append(values)
    sets = (_template_set(0, V9_TEMPLATE) if with_template else b"") + _data_set(V9_TEMPLATE, rows)
    count = len(rows) + (1 if with_template else 0)
    header = struct.pack("!HHIIII", 9, count, sys_uptime_ms & 0xFFFFFFFF, unix_secs, sequence, source_id)
    return header + sets


def encode_ipfix(records, *, domain_id, sequence, export_secs, with_template) -> bytes:
    rows = []
    for r in records:
        values = _record_values(r)
        values[FLOW_START_MS] = r.start_ms
        values[FLOW_END_MS] = r.end_ms
        rows.append(values)
    sets = (_template_set(2, IPFIX_TEMPLATE) if with_template else b"") + _data_set(IPFIX_TEMPLATE, rows)
    header = struct.pack("!HHIII", 10, 16 + len(sets), export_secs, sequence, domain_id)
    return header + sets


class Decoder:
    """Stateful decoder: remembers templates per exporter/domain."""

    def __init__(self) -> None:
        self.templates: dict[tuple, list[tuple[int, int]]] = {}
        self.dropped_no_template = 0

    def decode(self, packet: bytes, exporter_addr: str) -> tuple[str, int, list[FlowRecord]]:
        """Return (protocol, domain_id, records)."""
        (version,) = struct.unpack_from("!H", packet)
        if version == 9:
            return self._decode_v9(packet, exporter_addr)
        if version == 10:
            return self._decode_ipfix(packet, exporter_addr)
        raise ValueError(f"unsupported flow export version {version}")

    def _decode_v9(self, pkt: bytes, addr: str):
        _, _, uptime, unix_secs, _, source_id = struct.unpack_from("!HHIIII", pkt)
        records = []
        offset = 20
        while offset + 4 <= len(pkt):
            set_id, length = struct.unpack_from("!HH", pkt, offset)
            if length < 4:
                break
            body = pkt[offset + 4 : offset + length]
            if set_id == 0:
                self._learn_templates(body, (addr, 9, source_id))
            elif set_id > 255:
                for values in self._data(body, (addr, 9, source_id, set_id)):
                    # Convert sysUptime-relative switched times to epoch ms.
                    base = unix_secs * 1000 - uptime
                    values["start_ms"] = base + values.get(V9_FIRST_SWITCHED, uptime)
                    values["end_ms"] = base + values.get(V9_LAST_SWITCHED, uptime)
                    records.append(_to_record(values))
            offset += length
        return "netflow_v9", source_id, records

    def _decode_ipfix(self, pkt: bytes, addr: str):
        _, total, export_secs, _, domain = struct.unpack_from("!HHIII", pkt)
        records = []
        offset = 16
        while offset + 4 <= min(total, len(pkt)):
            set_id, length = struct.unpack_from("!HH", pkt, offset)
            if length < 4:
                break
            body = pkt[offset + 4 : offset + length]
            if set_id == 2:
                self._learn_templates(body, (addr, 10, domain))
            elif set_id > 255:
                for values in self._data(body, (addr, 10, domain, set_id)):
                    values["start_ms"] = values.get(FLOW_START_MS, export_secs * 1000)
                    values["end_ms"] = values.get(FLOW_END_MS, export_secs * 1000)
                    records.append(_to_record(values))
            offset += length
        return "ipfix", domain, records

    def _learn_templates(self, body: bytes, scope: tuple) -> None:
        pos = 0
        while pos + 4 <= len(body):
            template_id, count = struct.unpack_from("!HH", body, pos)
            pos += 4
            fields = []
            for _ in range(count):
                fid, flen = struct.unpack_from("!HH", body, pos)
                pos += 4
                if fid & 0x8000:  # IPFIX enterprise-specific element
                    pos += 4
                    fid = -1
                fields.append((fid, flen))
            self.templates[scope + (template_id,)] = fields

    def _data(self, body: bytes, key: tuple):
        fields = self.templates.get(key)
        if fields is None:
            self.dropped_no_template += 1
            return
        pos = 0
        while True:
            values = {}
            start = pos
            for fid, flen in fields:
                if flen == 0xFFFF:  # IPFIX variable length
                    flen = body[pos]
                    pos += 1
                    if flen == 255:
                        flen = struct.unpack_from("!H", body, pos)[0]
                        pos += 2
                if pos + flen > len(body):
                    return
                raw = body[pos : pos + flen]
                pos += flen
                if fid in (SRC_ADDR, DST_ADDR):
                    values[fid] = str(ipaddress.IPv4Address(raw))
                elif fid >= 0 and flen <= 8:
                    values[fid] = int.from_bytes(raw, "big")
            if pos == start:
                return
            yield values


def _to_record(v: dict) -> FlowRecord:
    return FlowRecord(
        src_ip=v.get(SRC_ADDR, "0.0.0.0"),
        dst_ip=v.get(DST_ADDR, "0.0.0.0"),
        src_port=v.get(SRC_PORT, 0),
        dst_port=v.get(DST_PORT, 0),
        protocol=v.get(PROTOCOL, 0),
        bytes=v.get(BYTES, 0),
        packets=v.get(PACKETS, 0),
        in_if=v.get(IN_IF, 0),
        out_if=v.get(OUT_IF, 0),
        start_ms=v["start_ms"],
        end_ms=v["end_ms"],
        tcp_flags=v.get(TCP_FLAGS, 0),
    )
