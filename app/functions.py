"""Elasticsearch aggregation tools exposed to the LLM through function calling.

Every tool builds an aggregation request against the NetFlow TSDS, runs it,
and post-processes the buckets into a compact JSON result for the model. The
exact request bodies are recorded in a trace so the UI can show them.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from elasticsearch import ApiError, Elasticsearch, TransportError

INDEX = "metrics-netflow.flows-*"
INVENTORY_INDEX = "flowwatch-inventory"
SITE_TZ = ZoneInfo(os.getenv("SITE_TZ", "UTC"))
DATA_LAG = timedelta(seconds=85)  # collector flushes a minute bucket ~80s after it opens
FINE_HISTORY = timedelta(hours=6)  # older backfilled history is at 5-minute resolution

DIMENSIONS = {
    "application": "application",
    "source_ip": "source.ip",
    "destination_ip": "destination.ip",
    "source_site": "source.site",
    "destination_site": "destination.site",
    "service_port": "service.port",
    "transport": "network.transport",
    "exporter": "exporter.name",
    "interface_in": "interface.in.name",
    "interface_out": "interface.out.name",
    "conversation": None,  # multi_terms on source ip, destination ip, service port
}
FILTER_FIELDS = {k: v for k, v in DIMENSIONS.items() if v}
INTERVALS = [60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400]


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- helpers

_REL = re.compile(r"^now(?:\s*([+-])\s*(\d+)\s*([mhdw]))?(?:\s*/\s*([hd]))?$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_time(value, now: datetime) -> datetime:
    text = str(value).strip()
    m = _REL.match(text.lower())
    if m:
        sign, amount, unit, round_to = m.groups()
        t = now.astimezone(SITE_TZ)
        if amount:
            delta = timedelta(**{_UNIT[unit]: int(amount)})
            t = t - delta if sign == "-" else t + delta
        if round_to == "h":
            t = t.replace(minute=0, second=0, microsecond=0)
        elif round_to == "d":
            t = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return t.astimezone(timezone.utc)
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as err:
        raise ToolError(f"cannot parse time '{value}': use ISO-8601 or 'now-6h' style") from err
    if t.tzinfo is None:
        t = t.replace(tzinfo=SITE_TZ)
    return t.astimezone(timezone.utc)


def parse_interval(value: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value).lower())
    if not m:
        raise ToolError(f"bad interval '{value}', use e.g. 5m, 1h, 1d")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def fmt_interval(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def local(ms_or_dt, fmt="%a %Y-%m-%d %H:%M") -> str:
    dt = ms_or_dt if isinstance(ms_or_dt, datetime) else datetime.fromtimestamp(ms_or_dt / 1000, timezone.utc)
    return dt.astimezone(SITE_TZ).strftime(fmt)


def peak_at(pipeline_result: dict) -> str | None:
    """Format the bucket key returned by a max_bucket pipeline aggregation."""
    keys = pipeline_result.get("keys") or []
    if not keys or not pipeline_result.get("value"):
        return None
    return local(datetime.fromisoformat(str(keys[0]).replace("Z", "+00:00")))


def mbps(nbytes: float, seconds: float) -> float:
    return round(nbytes * 8 / max(seconds, 1) / 1e6, 1)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def kql(filters: dict | None) -> str:
    """KQL equivalent of a tool's filters (for Kibana deep links)."""
    parts = []
    for key, value in (filters or {}).items():
        if value in (None, "", []):
            continue
        values = value if isinstance(value, list) else [value]
        if key == "interface":
            fields = ["interface.in.name", "interface.out.name"]
        elif key == "any_ip":
            fields = ["source.ip", "destination.ip"]
        elif key in FILTER_FIELDS:
            fields = [FILTER_FIELDS[key]]
        else:
            continue
        terms = [f'{f}:{int(v) if key == "service_port" else json.dumps(str(v))}' for f in fields for v in values]
        parts.append(terms[0] if len(terms) == 1 else "(" + " or ".join(terms) + ")")
    return " and ".join(parts)


def zulu(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_kql(ip: str) -> str:
    return f'source.ip:"{ip}" or destination.ip:"{ip}"'


class FlowTools:
    def __init__(self, es: Elasticsearch):
        self.es = es
        self._inventory: list[dict] | None = None
        self._trace: dict = {}

    # ---------------------------------------------------------- plumbing

    def call(self, name: str, args: dict) -> tuple[dict, dict]:
        """Run tool ``name``; returns (result for the model, trace for the UI)."""
        self._trace = {"queries": [], "charts": []}
        fn = getattr(self, f"tool_{name}", None)
        try:
            if fn is None:
                raise ToolError(f"unknown tool {name}")
            result = fn(**args)
        except ToolError as err:
            result = {"error": str(err)}
        except ApiError as err:
            result = {"error": f"Elasticsearch rejected the request: {err.message} {json.dumps(err.body)[:800]}"}
        except TransportError as err:
            result = {"error": f"Elasticsearch unavailable: {err}"}
        except TypeError as err:
            result = {"error": f"bad arguments: {err}"}
        return result, self._trace

    def _search(self, body: dict, index: str = INDEX) -> dict:
        body = {"size": 0, "timeout": "20s", **body}
        res = self.es.search(index=index, **body)
        self._trace["queries"].append({"index": index, "body": body, "took_ms": res.get("took")})
        return res

    def _chart(self, title: str, series: dict, unit: str = "Mbps", capacity: float | None = None):
        self._trace["charts"].append({"title": title, "unit": unit, "series": series, "capacity": capacity})

    def inventory(self) -> list[dict]:
        if self._inventory is None:
            res = self.es.search(index=INVENTORY_INDEX, size=500)
            self._inventory = [h["_source"] for h in res["hits"]["hits"]]
        return self._inventory

    def _window(self, start, end, now: datetime | None = None) -> tuple[datetime, datetime]:
        now = now or datetime.now(timezone.utc)
        data_end = (now - DATA_LAG).replace(second=0, microsecond=0) + timedelta(minutes=1)
        s = parse_time(start, now)
        e = min(parse_time(end or "now", now), data_end)
        if e <= s:
            raise ToolError(f"empty time window {local(s)} -> {local(e)} (data is complete up to {local(data_end)})")
        return s, e

    def _interval(self, s: datetime, e: datetime, requested=None, max_points: int = 60) -> int:
        resolution = 60 if s >= datetime.now(timezone.utc) - FINE_HISTORY else 300
        if requested:
            return max(parse_interval(requested), resolution)
        span = (e - s).total_seconds()
        return next((i for i in INTERVALS if i >= resolution and span / i <= max_points), INTERVALS[-1])

    def _query(self, s: datetime, e: datetime, filters: dict | None) -> dict:
        # Remember window + filters as KQL so the UI can open the same slice in Kibana.
        self._trace["kibana"] = {"from": zulu(s), "to": zulu(e), "kql": kql(filters)}
        clauses = [{"range": {"@timestamp": {"gte": s.isoformat(), "lt": e.isoformat()}}}]
        for key, value in (filters or {}).items():
            if value in (None, "", []):
                continue
            values = value if isinstance(value, list) else [value]
            if key == "service_port":
                values = [int(v) for v in values]
            if key in ("interface", "any_ip"):
                fields = ("interface.in.name", "interface.out.name") if key == "interface" else ("source.ip", "destination.ip")
                clauses.append({"bool": {"should": [{"terms": {f: values}} for f in fields], "minimum_should_match": 1}})
            elif key in FILTER_FIELDS:
                clauses.append({"terms": {FILTER_FIELDS[key]: values}})
            else:
                raise ToolError(f"unknown filter '{key}', valid: {sorted([*FILTER_FIELDS, 'interface', 'any_ip'])}")
        return {"bool": {"filter": clauses}}

    @staticmethod
    def _group_agg(dimension: str, size: int, sub: dict) -> dict:
        if dimension not in DIMENSIONS:
            raise ToolError(f"unknown dimension '{dimension}', valid: {list(DIMENSIONS)}")
        order = {"bytes": "desc"}
        if dimension == "conversation":
            terms = [{"field": "source.ip"}, {"field": "destination.ip"}, {"field": "service.port"}]
            return {"multi_terms": {"terms": terms, "size": size, "order": order}, "aggs": sub}
        return {"terms": {"field": DIMENSIONS[dimension], "size": size, "order": order}, "aggs": sub}

    @staticmethod
    def _key(bucket: dict, dimension: str) -> str:
        if dimension == "conversation":
            src, dst, port = bucket["key"]
            return f"{src} -> {dst}:{port}"
        return str(bucket.get("key_as_string", bucket["key"]))

    @staticmethod
    def _histogram(s: datetime, e: datetime, interval: int) -> dict:
        start = ms(s) // (interval * 1000) * interval * 1000
        return {
            "date_histogram": {
                "field": "@timestamp", "fixed_interval": fmt_interval(interval), "min_doc_count": 0,
                "extended_bounds": {"min": start, "max": ms(e) - 1},
            },
            "aggs": {"bytes": {"sum": {"field": "network.bytes"}}},
        }

    @staticmethod
    def _series(buckets: list, s: datetime, e: datetime, interval: int) -> list[tuple[int, float]]:
        """Convert histogram buckets to (epoch ms, Mbps), accounting for partial edge buckets."""
        out = []
        for b in buckets:
            b0, b1 = b["key"], b["key"] + interval * 1000
            seconds = (min(b1, ms(e)) - max(b0, ms(s))) / 1000
            if seconds > 0:
                out.append((b0, mbps(b["bytes"]["value"], seconds)))
        return out

    @staticmethod
    def _points(series: list[tuple[int, float]], max_points: int = 60) -> list:
        step = max(1, -(-len(series) // max_points))
        return [[local(t, "%m-%d %H:%M"), v] for t, v in series[::step]]

    @staticmethod
    def _stats(series: list[tuple[int, float]]) -> dict:
        if not series:
            return {"avg_mbps": 0, "peak_mbps": 0}
        peak_t, peak = max(series, key=lambda p: p[1])
        values = [v for _, v in series]
        return {"avg_mbps": round(statistics.fmean(values), 1), "peak_mbps": peak, "peak_at": local(peak_t)}

    # ---------------------------------------------------------- tools

    def tool_get_network_inventory(self) -> dict:
        inv = self.inventory()
        res = self._search({"aggs": {"first": {"min": {"field": "@timestamp"}}, "last": {"max": {"field": "@timestamp"}}}})
        aggs = res["aggregations"]
        exporters = []
        for exp in (d for d in inv if d["kind"] == "exporter"):
            ifaces = [
                {k: d[k] for k in ("name", "description", "role", "capacity_mbps")}
                for d in inv if d["kind"] == "interface" and d["exporter"] == exp["name"]
            ]
            exporters.append({"name": exp["name"], "protocol": exp["protocol"], "site": exp["site"], "interfaces": ifaces})
        meta = next((d for d in inv if d["kind"] == "meta"), {})
        return {
            "site_timezone": str(SITE_TZ),
            "data_available": {
                "from": local(aggs["first"]["value"]) if aggs["first"]["value"] else None,
                "to": local(aggs["last"]["value"]) if aggs["last"]["value"] else None,
            },
            "exporters": exporters,
            "sites": {d["name"]: d["network"] for d in inv if d["kind"] == "site"} | {"internet": "everything else"},
            "applications": {d["name"]: d["description"] for d in inv if d["kind"] == "application"},
            "group_by_dimensions": list(DIMENSIONS),
            "notes": meta.get("notes", ""),
        }

    def tool_bandwidth_timeseries(self, start="now-24h", end="now", interval=None, group_by=None,
                                  top_n=5, filters=None) -> dict:
        s, e = self._window(start, end)
        step = self._interval(s, e, interval)
        top_n = min(int(top_n or 5), 8)
        aggs = {"total": {"sum": {"field": "network.bytes"}}, "timeline": self._histogram(s, e, step)}
        if group_by:
            aggs["groups"] = self._group_agg(group_by, top_n, {
                "bytes": {"sum": {"field": "network.bytes"}}, "timeline": self._histogram(s, e, step),
            })
        res = self._search({"query": self._query(s, e, filters), "aggs": aggs})["aggregations"]
        seconds = (e - s).total_seconds()
        total_series = self._series(res["timeline"]["buckets"], s, e, step)
        out = {
            "window": f"{local(s)} -> {local(e)}", "interval": fmt_interval(step), "unit": "Mbps",
            "total": {"avg_mbps": mbps(res["total"]["value"], seconds), **{
                k: v for k, v in self._stats(total_series).items() if k != "avg_mbps"}},
        }
        if not group_by:
            out["total"]["points"] = self._points(total_series)
            self._chart("Total bandwidth", {"total": total_series})
            return out
        groups, chart = [], {}
        for b in res["groups"]["buckets"]:
            key = self._key(b, group_by)
            series = self._series(b["timeline"]["buckets"], s, e, step)
            chart[key] = series
            stats = self._stats(series)
            stats["avg_mbps"] = mbps(b["bytes"]["value"], seconds)
            groups.append({
                "key": key, "share_pct": round(100 * b["bytes"]["value"] / max(res["total"]["value"], 1), 1),
                **stats, "points": self._points(series, 40 if len(res["groups"]["buckets"]) > 3 else 60),
            })
        out["groups"] = groups
        self._chart(f"Bandwidth by {group_by}", chart)
        return out

    def tool_top_talkers(self, start="now-1h", end="now", dimension="conversation", size=10, filters=None) -> dict:
        s, e = self._window(start, end)
        step = self._interval(s, e, None, max_points=120)
        size = min(int(size or 10), 25)
        sub = {
            "bytes": {"sum": {"field": "network.bytes"}},
            "packets": {"sum": {"field": "network.packets"}},
            "timeline": self._histogram(s, e, step),
            "peak": {"max_bucket": {"buckets_path": "timeline>bytes"}},
        }
        if dimension != "application":
            sub["apps"] = {"terms": {"field": "application", "size": 2, "order": {"b": "desc"}},
                           "aggs": {"b": {"sum": {"field": "network.bytes"}}}}
        aggs = {"total": {"sum": {"field": "network.bytes"}}, "groups": self._group_agg(dimension, size, sub)}
        res = self._search({"query": self._query(s, e, filters), "aggs": aggs})["aggregations"]
        total = res["total"]["value"]
        seconds = (e - s).total_seconds()
        rows = []
        for b in res["groups"]["buckets"]:
            row = {
                "key": self._key(b, dimension),
                "total_gb": round(b["bytes"]["value"] / 1e9, 2),
                "share_pct": round(100 * b["bytes"]["value"] / max(total, 1), 1),
                "avg_mbps": mbps(b["bytes"]["value"], seconds),
                "peak_mbps": mbps(b["peak"]["value"] or 0, step),
                "peak_at": peak_at(b["peak"]),
            }
            if "apps" in b:
                row["applications"] = [a["key"] for a in b["apps"]["buckets"]]
            rows.append(row)
        return {
            "window": f"{local(s)} -> {local(e)}", "dimension": dimension,
            "total_gb": round(total / 1e9, 2), "total_avg_mbps": mbps(total, seconds),
            "peak_resolution": fmt_interval(step), "rows": rows,
        }

    def tool_interface_utilization(self, start="now-24h", end="now", exporter=None, interface=None,
                                   interval=None, threshold_pct=80) -> dict:
        s, e = self._window(start, end)
        step = self._interval(s, e, interval, max_points=96)
        ifaces = [d for d in self.inventory() if d["kind"] == "interface"
                  and (not exporter or d["exporter"] == exporter) and (not interface or d["name"] == interface)]
        if not ifaces:
            raise ToolError("no matching interface; call get_network_inventory for valid names")
        per_dir = {
            "bytes": {"sum": {"field": "network.bytes"}},
            "timeline": self._histogram(s, e, step),
            "peak": {"max_bucket": {"buckets_path": "timeline>bytes"}},
            "p95": {"percentiles_bucket": {"buckets_path": "timeline>bytes", "percents": [95]}},
        }
        aggs = {"exporters": {"terms": {"field": "exporter.name", "size": 20}, "aggs": {
            "inbound": {"terms": {"field": "interface.in.name", "size": 50}, "aggs": per_dir},
            "outbound": {"terms": {"field": "interface.out.name", "size": 50}, "aggs": per_dir},
        }}}
        filters = {"exporter": exporter} if exporter else None
        res = self._search({"query": self._query(s, e, filters), "aggs": aggs})["aggregations"]
        found = {
            (x["key"], direction, b["key"]): b
            for x in res["exporters"]["buckets"] for direction in ("inbound", "outbound")
            for b in x[direction]["buckets"]
        }
        seconds = (e - s).total_seconds()
        out = []
        for iface in ifaces:
            cap = iface["capacity_mbps"]
            entry = {"exporter": iface["exporter"], "interface": iface["name"], "description": iface["description"],
                     "capacity_mbps": cap}
            chart = {}
            for direction in ("inbound", "outbound"):
                b = found.get((iface["exporter"], direction, iface["name"]))
                if not b:
                    entry[direction] = {"avg_mbps": 0, "avg_util_pct": 0}
                    continue
                series = self._series(b["timeline"]["buckets"], s, e, step)
                chart[direction] = series
                peak = mbps(b["peak"]["value"] or 0, step)
                p95 = mbps(b["p95"]["values"]["95.0"] or 0, step)
                avg = mbps(b["bytes"]["value"], seconds)
                entry[direction] = {
                    "avg_mbps": avg, "avg_util_pct": round(100 * avg / cap, 1),
                    "p95_mbps": p95, "p95_util_pct": round(100 * p95 / cap, 1),
                    "peak_mbps": peak, "peak_util_pct": round(100 * peak / cap, 1),
                    "peak_at": peak_at(b["peak"]),
                    "congested_periods": self._periods(series, cap * threshold_pct / 100, step, cap),
                }
                if interface:
                    entry[direction]["points"] = self._points(series, 48)
            out.append(entry)
            if chart and (interface or len(ifaces) <= 2 or iface["role"] != "lan"):
                self._chart(f"{iface['exporter']} {iface['name']} ({iface['description']})", chart, capacity=cap)
        return {"window": f"{local(s)} -> {local(e)}", "interval": fmt_interval(step),
                "threshold_pct": threshold_pct, "interfaces": out}

    @staticmethod
    def _periods(series, threshold_mbps, step, cap) -> list[dict]:
        periods, current = [], None
        for t, v in series:
            if v >= threshold_mbps:
                if current and t == current["end_ms"]:
                    current["end_ms"] = t + step * 1000
                    current["peak"] = max(current["peak"], v)
                else:
                    current = {"start_ms": t, "end_ms": t + step * 1000, "peak": v}
                    periods.append(current)
        return [{
            "from": local(p["start_ms"]), "to": local(p["end_ms"], "%H:%M"),
            "minutes": (p["end_ms"] - p["start_ms"]) // 60000,
            "peak_util_pct": round(100 * p["peak"] / cap, 1),
        } for p in periods][:10]

    def tool_compare_to_baseline(self, start="now-1h", end="now", dimension="application",
                                 baseline="same_time_previous_days", days=5, size=10, filters=None) -> dict:
        s, e = self._window(start, end)
        days = max(1, min(int(days or 5), 13))
        windows = {"current": (s, e)}
        if baseline == "previous_period":
            windows["previous"] = (s - (e - s), s)
        elif baseline == "same_time_last_week":
            windows["last_week"] = (s - timedelta(days=7), e - timedelta(days=7))
        elif baseline == "same_time_previous_days":
            weekend = s.astimezone(SITE_TZ).weekday() >= 5
            k = 1
            while len(windows) <= days and k <= 13:
                d = s - timedelta(days=k)
                if (d.astimezone(SITE_TZ).weekday() >= 5) == weekend:
                    windows[f"d-{k}"] = (d, e - timedelta(days=k))
                k += 1
        else:
            raise ToolError("baseline must be same_time_previous_days, same_time_last_week or previous_period")
        range_filters = {
            name: {"range": {"@timestamp": {"gte": a.isoformat(), "lt": b.isoformat()}}} for name, (a, b) in windows.items()
        }
        query = self._query(min(a for a, _ in windows.values()), e, filters)
        self._trace["kibana"]["from"] = zulu(s)  # link to the window in question, not the baseline days
        aggs = {"windows": {"filters": {"filters": range_filters}, "aggs": {
            "bytes": {"sum": {"field": "network.bytes"}},
            "groups": self._group_agg(dimension, 50, {"bytes": {"sum": {"field": "network.bytes"}}}),
        }}}
        res = self._search({"query": query, "aggs": aggs})["aggregations"]["windows"]["buckets"]
        seconds = (e - s).total_seconds()
        rates: dict[str, dict[str, float]] = {}
        for name, bucket in res.items():
            for g in bucket["groups"]["buckets"]:
                rates.setdefault(self._key(g, dimension), {})[name] = g["bytes"]["value"] * 8 / seconds / 1e6
        baseline_names = [n for n in windows if n != "current"]

        def summarize(values: dict) -> dict:
            cur = values.get("current", 0.0)
            base = statistics.median([values.get(n, 0.0) for n in baseline_names])
            return {
                "current_mbps": round(cur, 1), "baseline_mbps": round(base, 1),
                "delta_mbps": round(cur - base, 1),
                "change_pct": round(100 * (cur - base) / base, 1) if base >= 0.1 else None,
            }

        rows = [{"key": k, **summarize(v)} for k, v in rates.items()]
        rows.sort(key=lambda r: abs(r["delta_mbps"]), reverse=True)
        totals = {n: b["bytes"]["value"] * 8 / seconds / 1e6 for n, b in res.items()}
        return {
            "current_window": f"{local(s)} -> {local(e)}",
            "baseline": baseline, "baseline_windows": [f"{local(windows[n][0])} -> {local(windows[n][1], '%H:%M')}" for n in baseline_names],
            "baseline_method": "median of baseline windows" if len(baseline_names) > 1 else "single window",
            "total": summarize(totals), "dimension": dimension, "rows": rows[: min(int(size or 10), 25)],
        }

    def tool_traffic_trend(self, days=14, dimension=None, size=6, filters=None) -> dict:
        days = max(2, min(int(days or 14), 20))
        now = datetime.now(timezone.utc)
        s = parse_time(f"now-{days - 1}d/d", now)
        s, e = self._window(s.isoformat(), "now", now)
        daily = {
            "date_histogram": {"field": "@timestamp", "calendar_interval": "1d", "time_zone": str(SITE_TZ),
                               "min_doc_count": 0},
            "aggs": {
                "bytes": {"sum": {"field": "network.bytes"}},
                "hourly": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"},
                           "aggs": {"bytes": {"sum": {"field": "network.bytes"}}}},
                "peak_hour": {"max_bucket": {"buckets_path": "hourly>bytes"}},
            },
        }
        aggs = {"groups": self._group_agg(dimension, min(int(size or 6), 10), {"bytes": {"sum": {"field": "network.bytes"}}, "daily": daily})} \
            if dimension else {"daily": daily}
        res = self._search({"query": self._query(s, e, filters), "aggs": aggs})["aggregations"]
        groups = [(self._key(b, dimension), b["daily"]["buckets"]) for b in res["groups"]["buckets"]] \
            if dimension else [("total", res["daily"]["buckets"])]
        out, chart = [], {}
        for key, buckets in groups:
            rows = []
            for b in buckets:
                day_start = datetime.fromtimestamp(b["key"] / 1000, timezone.utc)
                elapsed = (min(day_start + timedelta(days=1), e) - day_start).total_seconds()
                if elapsed <= 0:
                    continue
                rows.append({
                    "ts": b["key"], "day": local(day_start, "%a %m-%d"), "partial": elapsed < 86000,
                    "weekend": day_start.astimezone(SITE_TZ).weekday() >= 5,
                    "avg_mbps": mbps(b["bytes"]["value"], elapsed),
                    "peak_hour_mbps": mbps(b["peak_hour"]["value"] or 0, 3600),
                })
            full = [r for r in rows if not r["partial"]]
            chart[key] = [(r["ts"], r["peak_hour_mbps"]) for r in rows]
            entry = {"key": key, "daily": [[r["day"], r["avg_mbps"], r["peak_hour_mbps"]] for r in rows]}
            # Week over week: each of the last 7 full days vs the same weekday one week earlier.
            by_day = {r["day"][4:]: r for r in full}
            pairs = [(r, by_day.get(local(r["ts"] - 7 * 86_400_000, "%m-%d"))) for r in full[-7:]]
            pairs = [(a, b) for a, b in pairs if b]
            if len(pairs) >= 4:
                a, b = sum(x["avg_mbps"] for x, _ in pairs), sum(y["avg_mbps"] for _, y in pairs)
                entry["week_over_week_change_pct"] = round(100 * (a - b) / b, 1) if b else None
            weekdays = [r for r in full if not r["weekend"]]
            if len(weekdays) >= 4:
                xs = [(r["ts"] - weekdays[0]["ts"]) / 86_400_000 for r in weekdays]
                ys = [r["peak_hour_mbps"] for r in weekdays]
                slope = statistics.linear_regression(xs, ys).slope
                entry["weekday_peak_trend_mbps_per_day"] = round(slope, 2)
                entry["weekday_peak_trend_pct_per_day"] = round(100 * slope / max(statistics.fmean(ys), 0.1), 2)
            out.append(entry)
        self._chart("Daily peak-hour Mbps", chart)
        return {"columns": ["day", "avg_mbps", "peak_hour_mbps"], "days": days,
                "note": "last day is partial; weekend days are naturally low", "groups": out}

    def tool_detect_anomalies(self, start="now-24h", end="now", dimension="application", interval="15m",
                              baseline_days=7, min_delta_mbps=25, filters=None) -> dict:
        s, e = self._window(start, end)
        baseline_days = max(1, min(int(baseline_days or 7), 13))
        step = max(parse_interval(interval or "15m"), 300)
        while ((e - s).total_seconds() + baseline_days * 86400) / step * 25 > 50_000:
            step = next(i for i in INTERVALS if i > step)
        if 86400 % step:
            raise ToolError("interval must divide a day evenly (e.g. 5m, 15m, 30m, 1h)")
        q_start = s - timedelta(days=baseline_days)
        aggs = {"groups": self._group_agg(dimension, 25, {
            "bytes": {"sum": {"field": "network.bytes"}}, "timeline": self._histogram(q_start, e, step),
        })}
        res = self._search({"query": self._query(q_start, e, filters), "aggs": aggs})["aggregations"]
        self._trace["kibana"]["from"] = zulu(s)
        events = []
        day_ms = 86_400_000
        series = {self._key(g, dimension): dict(self._series(g["timeline"]["buckets"], q_start, e, step))
                  for g in res["groups"]["buckets"]}
        # Baseline days before the data begins would read as zero traffic: ignore them.
        data_start = min((t for v in series.values() for t, x in v.items() if x > 0), default=ms(q_start))
        for key, values in series.items():
            current = None
            for t in sorted(values):
                if t + step * 1000 <= ms(s):
                    continue
                weekend = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(SITE_TZ).weekday() >= 5
                past = [(t - k * day_ms) for k in range(1, baseline_days + 1) if t - k * day_ms >= data_start]
                if not past:
                    continue
                same_type = [p for p in past if (datetime.fromtimestamp(p / 1000, timezone.utc)
                                                 .astimezone(SITE_TZ).weekday() >= 5) == weekend]
                refs = [values.get(p, 0.0) for p in (same_type if len(same_type) >= 2 else past)]
                base, cur = statistics.median(refs), values[t]
                kind = None
                if cur - base >= min_delta_mbps and cur >= 2 * base:
                    kind = "spike"
                elif base - cur >= min_delta_mbps and cur <= 0.5 * base:
                    kind = "drop"
                if kind and current and current["type"] == kind and current["end_ms"] == t:
                    current["end_ms"] = t + step * 1000
                    if abs(cur - base) > abs(current["peak_mbps"] - current["baseline_mbps"]):
                        current.update(peak_mbps=cur, baseline_mbps=round(base, 1), peak_ms=t)
                elif kind:
                    current = {"key": key, "type": kind, "start_ms": t, "end_ms": t + step * 1000,
                               "peak_mbps": cur, "baseline_mbps": round(base, 1), "peak_ms": t}
                    events.append(current)
                else:
                    current = None
        for ev in events:
            ev["score"] = abs(ev["peak_mbps"] - ev["baseline_mbps"]) * (ev["end_ms"] - ev["start_ms"]) / 60000
        events.sort(key=lambda ev: ev["score"], reverse=True)
        return {
            "window": f"{local(s)} -> {local(e)}", "dimension": dimension, "interval": fmt_interval(step),
            "method": f"each bucket vs median of the same time of day over the previous {baseline_days} days "
                      f"(same weekday/weekend type); spike >= 2x and +{min_delta_mbps} Mbps, drop <= 0.5x",
            "events": [{
                "key": ev["key"], "type": ev["type"], "from": local(ev["start_ms"]), "to": local(ev["end_ms"], "%H:%M"),
                "minutes": (ev["end_ms"] - ev["start_ms"]) // 60000, "peak_mbps": ev["peak_mbps"],
                "baseline_mbps": ev["baseline_mbps"], "peak_at": local(ev["peak_ms"]),
            } for ev in events[:15]],
        }

    def tool_run_aggregation(self, aggregations: str, start="now-24h", end="now", filters=None) -> dict:
        s, e = self._window(start, end)
        try:
            aggs = json.loads(aggregations) if isinstance(aggregations, str) else aggregations
        except json.JSONDecodeError as err:
            raise ToolError(f"aggregations is not valid JSON: {err}") from err
        if not isinstance(aggs, dict) or not aggs:
            raise ToolError("aggregations must be a JSON object of named aggregations")
        if "scripted_metric" in json.dumps(aggs) or '"script"' in json.dumps(aggs):
            raise ToolError("scripts are not allowed")
        res = self._search({"query": self._query(s, e, filters), "aggs": aggs})
        text = json.dumps(res.get("aggregations", {}), separators=(",", ":"))
        return {
            "window": f"{local(s)} -> {local(e)}",
            "aggregations": json.loads(text) if len(text) <= 12000 else text[:12000] + "...(truncated)",
        }

    # ---------------------------------------------------------- UI helpers

    def pipeline_status(self) -> dict:
        now = datetime.now(timezone.utc)
        res = self.es.search(index=INDEX, size=0, aggs={
            "last": {"max": {"field": "@timestamp"}}, "first": {"min": {"field": "@timestamp"}},
            "recent": {"filter": {"range": {"@timestamp": {"gte": "now-5m"}}},
                       "aggs": {"bytes": {"sum": {"field": "network.bytes"}}}},
        }, track_total_hits=True)
        aggs = res["aggregations"]
        last = aggs["last"]["value"]
        return {
            "docs": res["hits"]["total"]["value"],
            "first": local(aggs["first"]["value"]) if aggs["first"]["value"] else None,
            "last_age_s": int(now.timestamp() - last / 1000) if last else None,
            "recent_mbps": mbps(aggs["recent"]["bytes"]["value"], 300),
        }


# ---------------------------------------------------------- declarations

_TIME = "ISO-8601 (site local time when no offset) or relative: 'now', 'now-6h', 'now-2d', 'now-1d/d' (start of yesterday)."
_FILTERS = {
    "type": "OBJECT",
    "description": "Optional filters, combined with AND.",
    "properties": {
        "application": {"type": "STRING"},
        "source_ip": {"type": "STRING", "description": "IP or CIDR"},
        "destination_ip": {"type": "STRING", "description": "IP or CIDR"},
        "any_ip": {"type": "STRING", "description": "IP or CIDR on either side of the flow"},
        "source_site": {"type": "STRING", "description": "HQ, DC, DR or internet"},
        "destination_site": {"type": "STRING", "description": "HQ, DC, DR or internet"},
        "service_port": {"type": "INTEGER"},
        "transport": {"type": "STRING", "description": "tcp or udp"},
        "exporter": {"type": "STRING"},
        "interface": {"type": "STRING", "description": "Interface name, either direction"},
        "interface_in": {"type": "STRING", "description": "Traffic received on this interface"},
        "interface_out": {"type": "STRING", "description": "Traffic transmitted on this interface"},
    },
}
_DIMENSION = {"type": "STRING", "enum": list(DIMENSIONS)}


def _fn(name, description, properties=None, required=None):
    params = {"type": "OBJECT", "properties": properties or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


TOOL_DECLARATIONS = [
    _fn("get_network_inventory",
        "Exporters, interfaces (with capacity), sites, applications, valid group-by dimensions and the time range of available data. Call first when you need names."),
    _fn("bandwidth_timeseries",
        "Bandwidth over time (Mbps) via a date_histogram, optionally split into the top N groups of a dimension. Use for 'what did traffic look like' questions.",
        {"start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "interval": {"type": "STRING", "description": "Optional bucket size like 5m, 1h. Auto-chosen if omitted."},
         "group_by": {**_DIMENSION, "description": "Optional dimension to split by"},
         "top_n": {"type": "INTEGER", "description": "Groups to return (max 8)"}, "filters": _FILTERS},
        ["start"]),
    _fn("top_talkers",
        "Rank hosts, conversations, applications, ports or sites by bytes in a window: total GB, share, average and peak Mbps (terms/multi_terms + max_bucket).",
        {"start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "dimension": {**_DIMENSION, "description": "What to rank; 'conversation' = source ip -> destination ip:port"},
         "size": {"type": "INTEGER"}, "filters": _FILTERS},
        ["start", "dimension"]),
    _fn("interface_utilization",
        "Link utilization vs capacity for each interface and direction: avg / p95 / peak Mbps and %, and periods above a congestion threshold (percentiles_bucket + max_bucket).",
        {"start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "exporter": {"type": "STRING"}, "interface": {"type": "STRING", "description": "Interface name, e.g. Gi0/0/1"},
         "interval": {"type": "STRING"}, "threshold_pct": {"type": "NUMBER", "description": "Congestion threshold, default 80"}},
        ["start"]),
    _fn("compare_to_baseline",
        "Is this normal? Compares average Mbps per group in a window against a baseline (median of the same time on previous comparable days, the same time last week, or the previous period).",
        {"start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "dimension": _DIMENSION,
         "baseline": {"type": "STRING", "enum": ["same_time_previous_days", "same_time_last_week", "previous_period"]},
         "days": {"type": "INTEGER", "description": "Baseline days for same_time_previous_days (default 5)"},
         "size": {"type": "INTEGER"}, "filters": _FILTERS},
        ["start"]),
    _fn("traffic_trend",
        "Multi-day trend: per-day average and peak-hour Mbps, week-over-week change and weekday peak growth rate, optionally per group. Use for growth / capacity planning.",
        {"days": {"type": "INTEGER", "description": "Days of history (max 20)"},
         "dimension": {**_DIMENSION, "description": "Optional dimension to split by"},
         "size": {"type": "INTEGER"}, "filters": _FILTERS}),
    _fn("detect_anomalies",
        "Find spikes and drops per group in a window compared with the same time of day on previous days. Use to discover incidents without knowing what to look for.",
        {"start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "dimension": _DIMENSION, "interval": {"type": "STRING", "description": "Bucket size, default 15m"},
         "baseline_days": {"type": "INTEGER"}, "min_delta_mbps": {"type": "NUMBER"}, "filters": _FILTERS},
        ["start"]),
    _fn("run_aggregation",
        "Escape hatch: run a custom Elasticsearch aggregation (size 0) on the flows data stream. Fields: @timestamp, exporter.name, interface.in.name, interface.out.name, source.ip, source.site, destination.ip, destination.site, service.port, application, network.transport, network.bytes, network.packets, flow.count.",
        {"aggregations": {"type": "STRING", "description": "JSON object of named aggregations, e.g. {\"x\":{\"terms\":{\"field\":\"application\"}}}"},
         "start": {"type": "STRING", "description": _TIME}, "end": {"type": "STRING", "description": _TIME},
         "filters": _FILTERS},
        ["aggregations"]),
]
