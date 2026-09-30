"""NetFlow v9 / IPFIX collector that writes 1-minute rollups into the TSDS."""
from __future__ import annotations

import logging
import os
import socket
import time

from elasticsearch import Elasticsearch, helpers

from .codec import Decoder
from .topology import EXPORTERS_BY_DOMAIN
from .tsds import DATA_STREAM, Rollup

log = logging.getLogger("collector")

BUCKET_MS = 60_000
GRACE_MS = int(float(os.getenv("FLUSH_GRACE_SECONDS", "20")) * 1000)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    es = Elasticsearch(os.getenv("ES_HOST", "http://elasticsearch:9200"), request_timeout=30)
    port = int(os.getenv("LISTEN_PORT", "2055"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)
    log.info("listening for NetFlow v9 / IPFIX on udp/%d, writing to %s", port, DATA_STREAM)

    decoder = Decoder()
    rollup = Rollup()
    # Buckets at or before this have been flushed; late flows roll forward.
    flushed_until = (int(time.time() * 1000) // BUCKET_MS - 1) * BUCKET_MS
    stats = {"packets": 0, "records": 0}

    while True:
        try:
            packet, (addr, _) = sock.recvfrom(65535)
        except socket.timeout:
            packet = None
        if packet:
            try:
                proto, domain, records = decoder.decode(packet, addr)
            except Exception as err:  # malformed packet: log and keep going
                log.warning("decode error from %s: %s", addr, err)
                records = []
            stats["packets"] += 1
            exporter = EXPORTERS_BY_DOMAIN.get(domain)
            name = exporter.name if exporter else f"{addr}/{domain}"
            for r in records:
                bucket = max(r.end_ms // BUCKET_MS * BUCKET_MS, flushed_until + BUCKET_MS)
                rollup.add(bucket, name, r.in_if, r.out_if, r.src_ip, r.dst_ip, r.src_port, r.dst_port,
                           r.protocol, r.bytes, r.packets, exporter_ip=addr, exporter_type=proto)
            stats["records"] += len(records)

        now_ms = int(time.time() * 1000)
        while flushed_until + BUCKET_MS + BUCKET_MS + GRACE_MS <= now_ms:
            bucket = flushed_until + BUCKET_MS
            flush(es, rollup.pop_documents(bucket), bucket, stats, decoder)
            flushed_until = bucket


def flush(es: Elasticsearch, docs: list[dict], bucket_ms: int, stats: dict, decoder: Decoder) -> None:
    if docs:
        actions = ({"_op_type": "create", "_index": DATA_STREAM, "_source": d} for d in docs)
        ok, errors = helpers.bulk(es, actions, raise_on_error=False, stats_only=False)
        if errors:
            log.warning("bucket %d: %d docs rejected, first: %s", bucket_ms, len(errors), errors[0])
    else:
        ok = 0
    log.info(
        "bucket %s: wrote %d docs (packets=%d records=%d no_template=%d)",
        time.strftime("%H:%M", time.gmtime(bucket_ms / 1000)), ok,
        stats["packets"], stats["records"], decoder.dropped_no_template,
    )
    stats["packets"] = stats["records"] = 0


if __name__ == "__main__":
    main()
