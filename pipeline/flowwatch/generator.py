"""Live traffic generator.

Simulates two exporters (edge-rtr-01 speaking NetFlow v9, dc-core-01 speaking
IPFIX) and sends real UDP export packets every EXPORT_INTERVAL seconds.
A small HTTP API lets the demo UI switch incident scenarios on and off.
"""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .codec import PROTO_NUM, FlowRecord, encode_ipfix, encode_v9
from .topology import EXPORTERS_BY_NAME, SCENARIOS
from .traffic import anchor_for, samples_at

log = logging.getLogger("generator")

INTERVAL = float(os.getenv("EXPORT_INTERVAL", "5"))
TARGETS = [t.strip() for t in os.getenv("FLOW_TARGETS", "flow-collector:2055").split(",") if t.strip()]
RECORDS_PER_PACKET = 30
TEMPLATE_EVERY = 10  # packets

_lock = threading.Lock()
_active: dict[str, tuple[float, float]] = {}  # scenario -> (expires_at, scale)


def active_scenarios() -> list[tuple[str, float]]:
    now = time.time()
    with _lock:
        for name in [n for n, (exp, _) in _active.items() if exp <= now]:
            del _active[name]
            log.info("scenario %s expired", name)
        return [(n, scale) for n, (_, scale) in _active.items()]


class Exporter:
    def __init__(self, name: str):
        self.cfg = EXPORTERS_BY_NAME[name]
        self.sequence = 0
        self.packets = 0
        # Pretend the device booted a few days ago so sysUptime math stays positive.
        self.boot_ms = int(time.time() * 1000) - random.randint(2, 9) * 86_400_000

    def packets_for(self, records: list[FlowRecord]) -> list[bytes]:
        out = []
        now = time.time()
        for i in range(0, len(records), RECORDS_PER_PACKET):
            chunk = records[i : i + RECORDS_PER_PACKET]
            with_template = self.packets % TEMPLATE_EVERY == 0
            if self.cfg.protocol == "netflow_v9":
                pkt = encode_v9(
                    chunk, source_id=self.cfg.domain_id, sequence=self.packets,
                    unix_secs=int(now), sys_uptime_ms=int(now) * 1000 - self.boot_ms,
                    with_template=with_template,
                )
            else:
                pkt = encode_ipfix(
                    chunk, domain_id=self.cfg.domain_id, sequence=self.sequence,
                    export_secs=int(now), with_template=with_template,
                )
            self.sequence += len(chunk)
            self.packets += 1
            out.append(pkt)
        return out


def run_exporter_loop() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    exporters = {name: Exporter(name) for name in EXPORTERS_BY_NAME}
    rng = random.Random()
    log.info("exporting every %.0fs to %s", INTERVAL, ", ".join(TARGETS))
    next_tick = time.time()
    while True:
        next_tick += INTERVAL
        now = datetime.now(timezone.utc)
        end_ms = int(now.timestamp() * 1000)
        start_ms = end_ms - int(INTERVAL * 1000)
        by_exporter: dict[str, list[FlowRecord]] = {name: [] for name in exporters}
        for s in samples_at(now, anchor_for(now), active_scenarios(), rng):
            nbytes = int(s.bps * INTERVAL / 8)
            by_exporter[s.exporter].append(FlowRecord(
                src_ip=s.src_ip, dst_ip=s.dst_ip, src_port=s.src_port, dst_port=s.dst_port,
                protocol=PROTO_NUM[s.transport], bytes=nbytes, packets=max(1, nbytes // 1100),
                in_if=s.in_if, out_if=s.out_if, start_ms=start_ms, end_ms=end_ms,
                tcp_flags=0x18 if s.transport == "tcp" else 0,
            ))
        for name, records in by_exporter.items():
            for pkt in exporters[name].packets_for(records):
                for target in TARGETS:
                    host, port = target.rsplit(":", 1)
                    try:
                        sock.sendto(pkt, (host, int(port)))
                    except OSError as err:  # optional targets (e.g. elastiflow) may not exist
                        log.debug("send to %s failed: %s", target, err)
        time.sleep(max(0.0, next_tick - time.time()))


class ControlAPI(BaseHTTPRequestHandler):
    def _reply(self, code: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _state(self):
        active = dict(_active)
        return [
            {
                "name": s.name, "title": s.title, "description": s.description,
                "active": s.name in active,
                "ends_at": datetime.fromtimestamp(active[s.name][0], timezone.utc).isoformat()
                if s.name in active else None,
            }
            for s in SCENARIOS.values()
        ]

    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") == "/scenarios":
            active_scenarios()
            return self._reply(200, self._state())
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        name = url.path.rstrip("/").rsplit("/", 1)[-1]
        if not url.path.startswith("/scenarios/") or name not in SCENARIOS:
            return self._reply(404, {"error": f"unknown scenario {name}"})
        qs = parse_qs(url.query)
        minutes = float(qs.get("minutes", ["15"])[0])
        scale = float(qs.get("scale", ["1.0"])[0])
        with _lock:
            _active[name] = (time.time() + minutes * 60, scale)
        log.info("scenario %s started for %.0f min (scale %.2f)", name, minutes, scale)
        self._reply(200, self._state())

    def do_DELETE(self):
        name = urlparse(self.path).path.rstrip("/").rsplit("/", 1)[-1]
        with _lock:
            if name == "scenarios":
                _active.clear()
            else:
                _active.pop(name, None)
        log.info("scenario %s stopped", name)
        self._reply(200, self._state())

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("CONTROL_PORT", "8000"))), ControlAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    run_exporter_loop()


if __name__ == "__main__":
    main()
