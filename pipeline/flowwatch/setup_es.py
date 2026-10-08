"""One-shot bootstrap: TSDS template, inventory index, and history backfill.

Idempotent: on re-run it keeps the original history anchor and setup time, and only
fills the gap between the newest document and now.
"""
from __future__ import annotations

import copy
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from elasticsearch import Elasticsearch, NotFoundError, helpers

from .codec import PROTO_NUM
from .topology import APPLICATIONS, EXPORTERS, SITES
from .traffic import SITE_TZ, anchor_for, history_scenarios, samples_at
from .tsds import DATA_STREAM, INDEX_TEMPLATE, INVENTORY_INDEX, Rollup

log = logging.getLogger("setup")
TEMPLATE_NAME = "flowwatch-flows"

BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "14"))
FINE_HOURS = 6  # most recent hours are written at 1-minute resolution, older at 5 minutes


def wait_for(es: Elasticsearch) -> None:
    for _ in range(90):
        try:
            if es.cluster.health(wait_for_status="yellow", timeout="5s")["status"] in ("green", "yellow"):
                return
        except Exception:
            pass
        time.sleep(2)
    sys.exit("Elasticsearch did not become ready")


def ensure_data_stream(es: Elasticsearch) -> bool:
    """Create the TSDS if needed. Returns True when it was just created for a history load.

    A TSDS write index only accepts documents within index.look_back_time (max 7d), so
    the first backing index is created with an explicit time range covering the whole
    backfill. After loading, the template is reset and the stream rolled over, so the
    new write index starts where the history ends (Elastic's documented TSDS reindex
    pattern).
    """
    try:
        es.indices.get_data_stream(name=DATA_STREAM)
        es.indices.put_index_template(name=TEMPLATE_NAME, **INDEX_TEMPLATE)
        return False
    except NotFoundError:
        pass
    now = datetime.now(timezone.utc)
    history = copy.deepcopy(INDEX_TEMPLATE)
    history["template"]["settings"].update({
        "index.time_series.start_time": (now - timedelta(days=BACKFILL_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "index.time_series.end_time": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    es.indices.put_index_template(name=TEMPLATE_NAME, **history)
    es.indices.create_data_stream(name=DATA_STREAM)
    log.info("created TSDS %s with a %d-day history backing index", DATA_STREAM, BACKFILL_DAYS + 1)
    return True


def finish_history_load(es: Elasticsearch) -> None:
    """Roll over if the write index still has the pinned history time range."""
    ds = es.indices.get_data_stream(name=DATA_STREAM)["data_streams"][0]
    write_index = ds["indices"][-1]["index_name"]
    settings = es.indices.get_settings(index=write_index, name="index.time_series.end_time")
    end = settings[write_index]["settings"]["index"]["time_series"]["end_time"]
    if datetime.fromisoformat(end.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(hours=1):
        return  # a normal write index: its end time keeps moving forward on its own
    es.indices.put_index_template(name=TEMPLATE_NAME, **INDEX_TEMPLATE)
    res = es.indices.rollover(alias=DATA_STREAM)
    log.info("rolled over from history index %s to write index %s", write_index, res["new_index"])


def load_history_times(es: Elasticsearch) -> tuple[datetime, datetime] | None:
    """The history anchor and the time es-setup first ran, or None on a fresh stack."""
    try:
        meta = es.get(index=INVENTORY_INDEX, id="meta")["_source"]
    except NotFoundError:
        return None
    anchor = datetime.fromisoformat(meta["history_anchor"]).astimezone(SITE_TZ)
    # Stacks created before the setup time was stored keep the old 07:40-09:00 incident.
    setup_at = meta.get("history_setup_at")
    setup_at = datetime.fromisoformat(setup_at) if setup_at else anchor + timedelta(hours=9, minutes=30)
    return anchor, setup_at.astimezone(SITE_TZ)


def write_inventory(es: Elasticsearch, anchor: datetime, setup_at: datetime) -> None:
    docs = [{
        "_id": "meta", "kind": "meta", "history_anchor": anchor.isoformat(),
        "history_setup_at": setup_at.isoformat(),
        "site_timezone": str(SITE_TZ), "data_stream": DATA_STREAM,
        "notes": "Each conversation is observed by exactly one exporter, so totals across exporters "
                 "do not double count. Rollups are 1-minute buckets (older backfilled history uses 5 minutes).",
    }]
    for exp in EXPORTERS:
        docs.append({"_id": f"exporter-{exp.name}", "kind": "exporter", "name": exp.name,
                     "protocol": exp.protocol, "site": exp.site})
        for iface in exp.interfaces:
            docs.append({
                "_id": f"if-{exp.name}-{iface.index}", "kind": "interface", "exporter": exp.name,
                "name": iface.name, "index": iface.index, "description": iface.description,
                "role": iface.role, "capacity_mbps": iface.capacity_bps / 1e6,
            })
    for name, cidr in SITES.items():
        docs.append({"_id": f"site-{name}", "kind": "site", "name": name, "network": cidr})
    for app in APPLICATIONS:
        docs.append({"_id": f"app-{app.name}", "kind": "application", "name": app.name,
                     "transport": app.transport, "ports": list(app.ports), "description": app.description})
    es.options(ignore_status=404).indices.delete(index=INVENTORY_INDEX)
    es.indices.create(index=INVENTORY_INDEX, settings={"number_of_replicas": 0})
    helpers.bulk(es, ({"_index": INVENTORY_INDEX, **d} for d in docs), refresh=True)


def newest_timestamp(es: Elasticsearch) -> datetime | None:
    res = es.search(index=DATA_STREAM, size=0, aggs={"max": {"max": {"field": "@timestamp"}}})
    value = res["aggregations"]["max"]["value"]
    return datetime.fromtimestamp(value / 1000, timezone.utc) if value else None


def backfill_actions(start: datetime, end: datetime, anchor: datetime, setup_at: datetime, stats: dict):
    rng = random.Random(int(start.timestamp()))
    t = start
    fine_from = end - timedelta(hours=FINE_HOURS)
    while t < end:
        step = 60 if t >= fine_from else 300
        if step == 300 and t.minute % 5:  # align coarse buckets to 5-minute boundaries
            step = 60
        mid = t + timedelta(seconds=step / 2)
        bucket_ms = int(t.timestamp() * 1000)
        rollup = Rollup()
        for s in samples_at(mid, anchor, history_scenarios(mid, anchor, setup_at), rng):
            nbytes = s.bps * step / 8
            rollup.add(bucket_ms, s.exporter, s.in_if, s.out_if, s.src_ip, s.dst_ip, s.src_port,
                       s.dst_port, PROTO_NUM[s.transport], nbytes, max(1, nbytes // 1100),
                       flows=max(1, step // 60))
        for doc in rollup.pop_documents(bucket_ms):
            stats["docs"] += 1
            yield {"_op_type": "create", "_index": DATA_STREAM, "_source": doc}
        t += timedelta(seconds=step)


def backfill(es: Elasticsearch, anchor: datetime, setup_at: datetime) -> None:
    newest = newest_timestamp(es)
    oldest_allowed = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    start = (newest + timedelta(minutes=1)) if newest else anchor.astimezone(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    start = max(start, oldest_allowed).replace(second=0, microsecond=0)
    # Catch up until the gap is under a minute: the collector owns everything after that.
    while True:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if end - start < timedelta(minutes=1):
            break
        log.info("backfilling %s -> %s", start.isoformat(), end.isoformat())
        stats = {"docs": 0}
        errors = 0
        for ok, item in helpers.streaming_bulk(
            es, backfill_actions(start, end, anchor, setup_at, stats), chunk_size=5000, raise_on_error=False,
        ):
            if not ok:
                errors += 1
                if errors <= 3:
                    log.warning("rejected: %s", item)
        log.info("backfilled %d docs (%d rejected)", stats["docs"], errors)
        start = end
    es.indices.refresh(index=DATA_STREAM)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    es = Elasticsearch(os.getenv("ES_HOST", "http://elasticsearch:9200"), request_timeout=120)
    wait_for(es)
    ensure_data_stream(es)
    now = datetime.now(timezone.utc)
    anchor, setup_at = load_history_times(es) or (anchor_for(now), now.astimezone(SITE_TZ))
    write_inventory(es, anchor, setup_at)
    log.info("history anchor: %s, setup at %s (site tz %s)", anchor.isoformat(), setup_at.isoformat(), SITE_TZ)
    backfill(es, anchor, setup_at)
    finish_history_load(es)
    log.info("setup complete")


if __name__ == "__main__":
    main()
