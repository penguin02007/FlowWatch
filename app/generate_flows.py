import os
from datetime import datetime, timezone

from elasticsearch import Elasticsearch


es = Elasticsearch(
    os.getenv("ES_HOST", "http://elasticsearch:9200"),
    api_version="8.14",
    request_timeout=30,
)


def inject_traffic_anomaly(target_host: str = "filebeat") -> dict:
    """Insert a synthetic NetFlow anomaly burst for a target host."""
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    docs = [
        {
            "@timestamp": timestamp,
            "netflow": {
                "source_ipv4_address": "10.0.0.42",
                "destination_ipv4_address": target_host,
                "destination_transport_port": 873,
                "in_bytes": 420000000,
                "out_bytes": 1200000,
                "protocol_identifier": 6,
                "flow_direction": "ingress",
            },
            "host": {"name": target_host},
            "event": {"dataset": "netflow"},
            "network": {"transport": "tcp"},
        },
        {
            "@timestamp": timestamp,
            "netflow": {
                "source_ipv4_address": "10.0.0.42",
                "destination_ipv4_address": target_host,
                "destination_transport_port": 873,
                "in_bytes": 390000000,
                "out_bytes": 1500000,
                "protocol_identifier": 6,
                "flow_direction": "ingress",
            },
            "host": {"name": target_host},
            "event": {"dataset": "netflow"},
            "network": {"transport": "tcp"},
        },
        {
            "@timestamp": timestamp,
            "netflow": {
                "source_ipv4_address": "10.0.0.42",
                "destination_ipv4_address": target_host,
                "destination_transport_port": 873,
                "in_bytes": 510000000,
                "out_bytes": 1700000,
                "protocol_identifier": 6,
                "flow_direction": "ingress",
            },
            "host": {"name": target_host},
            "event": {"dataset": "netflow"},
            "network": {"transport": "tcp"},
        },
    ]

    indexed = []
    for doc in docs:
        result = es.index(index="netflow-demo-2026.09.11", document=doc)
        indexed.append(result.get("_id"))

    return {"status": "ok", "target_host": target_host, "indexed_ids": indexed}
