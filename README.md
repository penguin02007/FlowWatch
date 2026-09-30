# FlowWatch: LLM + Elasticsearch + NetFlow

A docker-compose demo in which an LLM answers network performance questions as they come up
("why was the ERP slow yesterday?", "is video traffic growing?"). It works out the answers by
correlating historical flow trends and bandwidth use in Elasticsearch.

![FlowWatch demo: the ERP slowdown traced to host 10.10.8.77, its graphs opened in Kibana, then a live upload incident spotted](docs/demo.gif)

*Demo 1: "Why was the ERP app slow yesterday afternoon?" → the MPLS link hit 97%, caused by
`10.10.8.77`, with the host's graphs opened in Kibana. Demo 2: a live incident injected through
the generator → "What is using the internet link right now?" (sped up; 28s).*

## Architecture

**NetFlow v9 / IPFIX → collector → Elasticsearch time series data stream → LLM function calling over the Aggregations API**

```
 flow-generator ──UDP──▶ flow-collector ──bulk──▶ Elasticsearch TSDS ◀──aggregations── flowwatch-app ◀──▶ Gemini
 (NetFlow v9 +           (v9/IPFIX decode,        metrics-netflow.        (8 function-calling tools,       (function
  IPFIX exporters,        enrich, 1-min           flows-default           Streamlit chat UI)                calling)
  scenario API :8000)     rollups)                (index.mode: time_series)
```

| Service | What it does |
|---|---|
| `flow-generator` | Simulates `edge-rtr-01` (**NetFlow v9**) and `dc-core-01` (**IPFIX**) exporting real UDP packets every 5s. Traffic follows business hours, weekends, a nightly backup and link capacity limits. HTTP API on `:8000` injects incidents. |
| `flow-collector` | Template-aware v9/IPFIX decoder. Adds site, application and interface names to each flow, rolls flows up per minute per dimension set, and writes to the TSDS. |
| `es-setup` | One-shot job. Creates the TSDS index template, the `flowwatch-inventory` index (interfaces, capacities, applications), and **14 days of backfilled history**. Re-runs are idempotent. |
| `flowwatch-app` | Streamlit chat on [localhost:8501](http://localhost:8501). Gemini picks tools, and each tool runs Elasticsearch aggregations. Every request body is shown in the UI. |
| `kibana` | [localhost:5601](http://localhost:5601), for exploring `metrics-netflow.flows-*` directly. |
| `kibana-setup` | One-shot job. Creates the `FlowWatch flows (TSDS)` data view and the **FlowWatch traffic explorer** dashboard (throughput by application, conversation and egress interface, plus a top-conversations table). |

**Evidence in Kibana:** under each answer, the app links every host IP the answer mentions to
the traffic explorer dashboard. The link carries a KQL query (`source.ip:"10.10.8.77" or
destination.ip:"10.10.8.77"`) and the time window of the tool call that found the host. Each
tool call in the evidence panel also has an *open this slice in Kibana* link that applies the
same filters. If Kibana isn't reachable at `http://localhost:5601` from the browser, set
`KIBANA_PUBLIC_URL`.

## Quick start

```powershell
copy .env.example .env      # then set GEMINI_API_KEY
docker compose up -d --build
docker compose logs -f es-setup   # backfill takes ~30s, then the collector starts
```

Open http://localhost:8501 and click a sample question.

## Data model (TSDS)

`metrics-netflow.flows-default` is a time series data stream. Each document is one bucket
(1 minute live, 5 minutes for older backfill) for one set of dimensions:

- **Dimensions:** `exporter.name`, `interface.in.name`, `interface.out.name`, `source.ip`, `source.site`, `destination.ip`, `destination.site`, `service.port`, `network.transport`, `application`
- **Metrics (gauge):** `network.bytes`, `network.packets`, `flow.count`

A TSDS write index only accepts data within `index.look_back_time` (7 days at most). To load
14 days of history, the first backing index is therefore created with an explicit
`index.time_series.start_time`/`end_time`. After loading, the stream is rolled over so live
data goes to a normal write index. This is the same technique as Elastic's "reindex a TSDS"
guide; see `pipeline/flowwatch/setup_es.py`.

## LLM tools (all Elasticsearch aggregations)

| Tool | Aggregations used |
|---|---|
| `get_network_inventory` | inventory index + `min`/`max` on `@timestamp` |
| `bandwidth_timeseries` | `terms` → `date_histogram` → `sum` |
| `top_talkers` | `terms`/`multi_terms` → `sum`, `date_histogram` + `max_bucket` |
| `interface_utilization` | `terms` per direction → `date_histogram` + `max_bucket` + `percentiles_bucket` (p95), compared with link capacity |
| `compare_to_baseline` | `filters` (current vs same time on previous comparable days) → `terms` → median |
| `traffic_trend` | `date_histogram` (calendar day, site TZ) → hourly `date_histogram` + `max_bucket`, then week-over-week change and a regression |
| `detect_anomalies` | `terms` → `date_histogram` over window + baseline days, each bucket compared with the same time of day on earlier days |
| `run_aggregation` | escape hatch: the LLM writes its own aggregation DSL (no scripts) |

## Demo script

The backfilled history contains these planted events, relative to the day `es-setup` first ran:

| When | What happened | Question to ask |
|---|---|---|
| Yesterday 13:05–14:35 | `10.10.8.77` SMB bulk copy saturates the 500 Mbps MPLS link (97%) | "Users said the ERP app was slow yesterday afternoon. Was there a network cause?" |
| 3 days ago 03:00–09:30 | rsync backup overran its 01:00–03:00 window on the DCI link | "Did the nightly backup behave differently in the last week?" |
| 5 days ago 16:00–17:10 | Windows update storm, about 780 Mbps on the internet uplink | "Find the biggest anomalies in the last 7 days." |
| 8 days ago 10:40 | `10.10.3.45` uploads to `185.220.101.7` | "Has 10.10.3.45 ever sent unusual amounts of data to the internet?" |
| Whole 14 days | Video conferencing grows about 4% a day | "Is video traffic growing? When would the uplink hit 80% at peak?" |

**Live incidents:** in the sidebar, click *Start* on a scenario. Wait about 2 minutes for
the collector to flush, then ask "What is using the internet link right now, and is it normal?"
You can also trigger incidents from a shell:

```powershell
curl.exe -X POST "http://localhost:8000/scenarios/data-exfiltration?minutes=15"
curl.exe -X DELETE http://localhost:8000/scenarios
```

Scenarios: `smb-bulk-copy`, `backup-overrun`, `update-storm`, `data-exfiltration`.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | (none) | required for the chat |
| `GEMINI_MODEL` | `gemini-3.8-flash` | any function-calling Gemini model |
| `SITE_TZ` | `UTC` | business-hours and answer time zone, e.g. `America/Los_Angeles`. Set it before the first `up`, because it shapes the backfill. |
| `BACKFILL_DAYS` | `14` | |

**Gemini free tier:** one answer takes about 4–8 model requests, and free-tier keys allow only
a few requests per minute and about 20 per day **per Google Cloud project**. A new key from the
same project shares that quota. For a live demo, use a key with billing enabled.

## Operations

```powershell
powershell -ExecutionPolicy Bypass -File .\check-stack-health.ps1
docker compose logs -f flow-collector          # one line per flushed minute
docker compose run --rm flow-collector python -m unittest discover -s tests   # codec/model tests
docker compose down -v                          # wipe everything, including history
```

**Optional ElastiFlow:** to also feed ElastiFlow (for its Kibana dashboards in `kibana/`), run:

```powershell
$env:FLOW_TARGETS="flow-collector:2055,elastiflow:2055"; docker compose --profile elastiflow up -d
```
