"""One-shot: Kibana data view + "FlowWatch traffic explorer" dashboard for the flows TSDS.

The chat app deep-links into this dashboard with a KQL query and time range, so
the evidence behind an answer (e.g. one host's traffic) opens as graphs.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("kibana-setup")

KIBANA = os.getenv("KIBANA_HOST", "http://kibana:5601")
DATA_VIEW_ID = "flowwatch-flows"
DASHBOARD_ID = "flowwatch-traffic-explorer"


def request(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{KIBANA}{path}", data=data, method=method,
                                 headers={"kbn-xsrf": "flowwatch", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def wait_for_kibana() -> None:
    for _ in range(120):
        try:
            if request("GET", "/api/status")["status"]["overall"]["level"] == "available":
                return
        except (urllib.error.URLError, KeyError, ValueError, OSError):
            pass
        time.sleep(5)
    sys.exit("Kibana did not become available")


def throughput_column(field: str = "network.bits") -> dict:
    return {
        "label": "Throughput", "customLabel": True, "dataType": "number", "operationType": "sum",
        "sourceField": field, "isBucketed": False, "scale": "ratio", "timeScale": "s",
        "params": {"emptyAsNull": True, "format": {"id": "bits", "params": {"decimals": 1}}},
    }


def terms_column(field: str, label: str, size: int, order_by: str, secondary: list[str] | None = None) -> dict:
    params = {"size": size, "orderBy": {"type": "column", "columnId": order_by}, "orderDirection": "desc",
              "otherBucket": False, "missingBucket": False, "parentFormat": {"id": "terms"}}
    if secondary:
        params["secondaryFields"] = secondary
        params["parentFormat"] = {"id": "multi_terms"}
    return {"label": label, "customLabel": True, "dataType": "string", "operationType": "terms",
            "sourceField": field, "isBucketed": True, "scale": "ordinal", "params": params}


def line_chart(obj_id: str, title: str, split_field: str, split_label: str, secondary=None) -> dict:
    columns = {
        "split": terms_column(split_field, split_label, 8, "bps", secondary),
        "time": {"label": "@timestamp", "dataType": "date", "operationType": "date_histogram",
                 "sourceField": "@timestamp", "isBucketed": True, "scale": "interval",
                 "params": {"interval": "auto", "includeEmptyRows": True, "dropPartials": True}},
        "bps": throughput_column(),
    }
    return lens(obj_id, title, "lnsXY", ["split", "time", "bps"], columns, {
        "legend": {"isVisible": True, "position": "right"},
        "valueLabels": "hide",
        "preferredSeriesType": "line",
        "layers": [{"layerId": "layer1", "layerType": "data", "seriesType": "line", "xAccessor": "time",
                    "splitAccessor": "split", "accessors": ["bps"]}],
    })


def conversation_table() -> dict:
    columns = {
        "src": terms_column("source.ip", "Source", 25, "bytes"),
        "dst": terms_column("destination.ip", "Destination", 5, "bytes"),
        "port": terms_column("service.port", "Port", 3, "bytes"),
        "app": terms_column("application", "Application", 2, "bytes"),
        "bytes": {"label": "Bytes", "customLabel": True, "dataType": "number", "operationType": "sum",
                  "sourceField": "network.bytes", "isBucketed": False, "scale": "ratio",
                  "params": {"emptyAsNull": True, "format": {"id": "bytes", "params": {"decimals": 1}}}},
        "peak": {"label": "Avg throughput", "customLabel": True, "dataType": "number", "operationType": "sum",
                 "sourceField": "network.bits", "isBucketed": False, "scale": "ratio", "timeScale": "s",
                 "params": {"emptyAsNull": True, "format": {"id": "bits", "params": {"decimals": 1}}}},
    }
    for key in ("src", "dst", "port", "app"):
        columns[key]["dataType"] = "number" if key == "port" else "ip" if key in ("src", "dst") else "string"
    order = ["src", "dst", "port", "app", "bytes", "peak"]
    return lens("flowwatch-top-conversations", "Top conversations", "lnsDatatable", order, columns, {
        "layerId": "layer1", "layerType": "data",
        "columns": [{"columnId": c, "isTransposed": False} for c in order],
        "sorting": {"columnId": "bytes", "direction": "desc"},
    })


def lens(obj_id, title, vis_type, order, columns, visualization) -> dict:
    return {
        "type": "lens", "id": obj_id,
        "attributes": {
            "title": title, "description": "", "visualizationType": vis_type,
            "state": {
                "datasourceStates": {"formBased": {"layers": {"layer1": {
                    "columnOrder": order, "columns": columns, "incompleteColumns": {},
                }}}},
                "visualization": visualization,
                "query": {"query": "", "language": "kuery"},
                "filters": [],
                "internalReferences": [], "adHocDataViews": {},
            },
        },
        "references": [{"type": "index-pattern", "id": DATA_VIEW_ID, "name": "indexpattern-datasource-layer-layer1"}],
    }


def dashboard(panels: list[tuple[dict, dict]]) -> dict:
    panels_json, refs = [], []
    for i, (obj, grid) in enumerate(panels, 1):
        pid = f"p{i}"
        panels_json.append({"type": "lens", "panelIndex": pid, "gridData": {**grid, "i": pid},
                            "embeddableConfig": {"enhancements": {}}, "panelRefName": f"panel_{pid}"})
        refs.append({"name": f"{pid}:panel_{pid}", "type": "lens", "id": obj["id"]})
    return {
        "type": "dashboard", "id": DASHBOARD_ID,
        "attributes": {
            "title": "FlowWatch traffic explorer",
            "description": "NetFlow v9 / IPFIX rollups from the metrics-netflow.flows TSDS. "
                           "The FlowWatch copilot links here with a KQL query for the host or filters it cited.",
            "panelsJSON": json.dumps(panels_json),
            "optionsJSON": json.dumps({"useMargins": True, "syncColors": True, "syncCursor": True,
                                       "syncTooltips": True, "hidePanelTitles": False}),
            "timeRestore": False,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
                {"query": {"query": "", "language": "kuery"}, "filter": []})},
        },
        "references": refs,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    wait_for_kibana()
    request("POST", "/api/data_views/data_view", {"override": True, "data_view": {
        "id": DATA_VIEW_ID, "name": "FlowWatch flows (TSDS)", "title": "metrics-netflow.flows-*",
        "timeFieldName": "@timestamp",
        "runtimeFieldMap": {"network.bits": {"type": "long", "script": {
            "source": "emit(doc['network.bytes'].value * 8)"}}},
        "fieldFormats": {"network.bytes": {"id": "bytes"}},
    }})
    by_app = line_chart("flowwatch-throughput-by-app", "Throughput by application", "application", "Application")
    by_conv = line_chart("flowwatch-throughput-by-conversation", "Throughput by conversation",
                         "source.ip", "Conversation", ["destination.ip"])
    by_link = line_chart("flowwatch-throughput-by-interface", "Throughput by egress interface",
                         "interface.out.name", "Egress interface", ["exporter.name"])
    table = conversation_table()
    dash = dashboard([
        (by_app, {"x": 0, "y": 0, "w": 24, "h": 14}),
        (by_conv, {"x": 24, "y": 0, "w": 24, "h": 14}),
        (by_link, {"x": 0, "y": 14, "w": 24, "h": 14}),
        (table, {"x": 24, "y": 14, "w": 24, "h": 14}),
    ])
    result = request("POST", "/api/saved_objects/_bulk_create?overwrite=true", [
        {"type": o["type"], "id": o["id"], "attributes": o["attributes"], "references": o["references"]}
        for o in (by_app, by_conv, by_link, table, dash)
    ])
    errors = [o for o in result["saved_objects"] if "error" in o]
    if errors:
        sys.exit(f"saved object errors: {errors}")
    log.info("dashboard ready: %s/app/dashboards#/view/%s", KIBANA, DASHBOARD_ID)


if __name__ == "__main__":
    main()
