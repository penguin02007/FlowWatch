"""Time series data stream definition and document building.

Each TSDS document is one bucket (1 min live, up to 5 min for older history) of
traffic for one unique combination of dimensions. The same builder is used by
the live collector and the backfill so both produce identical documents.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .codec import PROTO_NAME
from .topology import EXPORTERS_BY_NAME, classify, service_port, site_of

DATA_STREAM = "metrics-netflow.flows-default"
INVENTORY_INDEX = "flowwatch-inventory"

INDEX_TEMPLATE = {
    "index_patterns": ["metrics-netflow.flows-*"],
    "data_stream": {},
    "priority": 500,
    "_meta": {"description": "FlowWatch NetFlow/IPFIX bandwidth rollups (TSDS)"},
    "template": {
        "settings": {
            "index.mode": "time_series",
            "index.routing_path": ["exporter.name", "application", "source.site", "destination.site"],
            "index.number_of_replicas": 0,
        },
        "mappings": {
            "dynamic": False,
            "properties": {
                "@timestamp": {"type": "date"},
                "exporter": {
                    "properties": {
                        "name": {"type": "keyword", "time_series_dimension": True},
                        "ip": {"type": "ip"},
                        "type": {"type": "keyword"},
                    }
                },
                "interface": {
                    "properties": {
                        "in": {"properties": {
                            "name": {"type": "keyword", "time_series_dimension": True},
                            "index": {"type": "integer"},
                        }},
                        "out": {"properties": {
                            "name": {"type": "keyword", "time_series_dimension": True},
                            "index": {"type": "integer"},
                        }},
                    }
                },
                "source": {"properties": {
                    "ip": {"type": "ip", "time_series_dimension": True},
                    "site": {"type": "keyword", "time_series_dimension": True},
                }},
                "destination": {"properties": {
                    "ip": {"type": "ip", "time_series_dimension": True},
                    "site": {"type": "keyword", "time_series_dimension": True},
                }},
                "service": {"properties": {
                    "port": {"type": "integer", "time_series_dimension": True},
                }},
                "application": {"type": "keyword", "time_series_dimension": True},
                "network": {"properties": {
                    "transport": {"type": "keyword", "time_series_dimension": True},
                    "bytes": {"type": "long", "time_series_metric": "gauge"},
                    "packets": {"type": "long", "time_series_metric": "gauge"},
                }},
                "flow": {"properties": {
                    "count": {"type": "long", "time_series_metric": "gauge"},
                }},
            },
        },
    },
}


def _iface_name(exporter, index: int) -> str:
    iface = exporter.interface(index) if exporter else None
    return iface.name if iface else f"ifIndex-{index}"


class Rollup:
    """Accumulates unidirectional flows into per-bucket dimension keys."""

    def __init__(self) -> None:
        self.buckets: dict[int, dict[tuple, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))

    def add(self, bucket_ms, exporter, in_if, out_if, src_ip, dst_ip, src_port, dst_port, protocol,
            nbytes, packets, flows=1, exporter_ip="", exporter_type=""):
        key = (exporter, exporter_ip, exporter_type, in_if, out_if, src_ip, dst_ip,
               service_port(src_port, dst_port), protocol)
        acc = self.buckets[bucket_ms][key]
        acc[0] += int(nbytes)
        acc[1] += int(packets)
        acc[2] += flows

    def pop_documents(self, bucket_ms: int) -> list[dict]:
        rows = self.buckets.pop(bucket_ms, {})
        return [document(bucket_ms, key, *acc) for key, acc in rows.items()]


def document(bucket_ms: int, key: tuple, nbytes: int, packets: int, flows: int) -> dict:
    exporter_name, exporter_ip, exporter_type, in_if, out_if, src, dst, port, proto = key
    exporter = EXPORTERS_BY_NAME.get(exporter_name)
    transport = PROTO_NAME.get(proto, str(proto))
    doc = {
        "@timestamp": datetime.fromtimestamp(bucket_ms / 1000, timezone.utc).isoformat(),
        "exporter": {"name": exporter_name, "type": exporter_type or (exporter.protocol if exporter else "")},
        "interface": {
            "in": {"name": _iface_name(exporter, in_if), "index": in_if},
            "out": {"name": _iface_name(exporter, out_if), "index": out_if},
        },
        "source": {"ip": src, "site": site_of(src)},
        "destination": {"ip": dst, "site": site_of(dst)},
        "service": {"port": port},
        "application": classify(transport, port, src, dst),
        "network": {"transport": transport, "bytes": nbytes, "packets": packets},
        "flow": {"count": flows},
    }
    if exporter_ip:
        doc["exporter"]["ip"] = exporter_ip
    return doc
