# FlowWatch: LLM + Elasticsearch + NetFlow

A docker-compose stack which an LLM answers network performance questions as they come up. It works out the answers by
correlating historical flow trends and bandwidth use in Elasticsearch.

![FlowWatch demo: the ERP slowdown traced to host 10.10.8.77, its graphs opened in Kibana, then a live upload incident spotted](docs/demo.gif)

## Stack

Routers export NetFlow v9 and IPFIX to a collector, which stores one-minute rollups in an
Elasticsearch time series data stream (TSDS). An LLM then answers questions by calling functional calls, and each
call queries that data through the Elasticsearch Aggregations API.

```text
┌──────────────────────┐   NetFlow v9 / IPFIX   ┌──────────────────────┐
│    flow-generator    │ ───── UDP 2055 ──────> │    flow-collector    │
│  edge-rtr-01    v9   │                        │   template decode    │
│  dc-core-01  IPFIX   │                        │  enrich site/app/if  │
│  incident API :8000  │                        └──────────┬───────────┘
└──────────^───────────┘                                   │ _bulk, 1-min rollups
           │ start / stop incidents                        V
┌──────────┴───────────┐   aggregations         ┌──────────────────────────────────┐
│    flowwatch-app     │ ─────────────────────> │          Elasticsearch           │
│   Streamlit :8501    │ <────── buckets ────── │   TSDS metrics-netflow.flows-*   │
│ agent loop, 8 tools  │                        │     index.mode: time_series      │
│                      │                        │       flowwatch-inventory        │
└──────┬───────────┬───┘                        └──────────────────────────────────┘
 prompt│   ^ tool  │                                             ^ Lens queries
       │   │ calls └── deep links (KQL + time) ──────┐           │
       V   │                                         V           │
┌──────────┴───────────┐                        ┌────────────────┴─────────────────┐
│   Gemini 3.8 Flash   │                        │           Kibana :5601           │
│   function calling   │                        │    FlowWatch traffic explorer    │
└──────────────────────┘                        └──────────────────────────────────┘
```

| Service | What it does |
|---|---|
| `flow-generator` | Simulates `edge-rtr-01` (**NetFlow v9**) and `dc-core-01` (**IPFIX**) exporting real UDP packets every 5s. Traffic follows business hours, weekends, a nightly backup and link capacity limits. HTTP API on `:8000` injects incidents. |
| `flow-collector` | Template-aware v9/IPFIX decoder. Adds site, application and interface names to each flow, rolls flows up per minute per dimension set, and writes to the TSDS. |
| `es-setup` | Creates TSDS index template, the `flowwatch-inventory` index (interfaces, capacities, applications), and **14 days of backfilled history**. Re-runs are idempotent. |
| `flowwatch-app` | Streamlit chat on [localhost:8501](http://localhost:8501). Gemini picks functional calls, and each call runs Elasticsearch aggregations. Every request body is shown in the UI. |
| `kibana` | [localhost:5601](http://localhost:5601), for exploring `metrics-netflow.flows-*` directly. |
| `kibana-setup` | Creates the `FlowWatch flows (TSDS)` data view and the **FlowWatch traffic explorer** dashboard (throughput by application, conversation and egress interface, plus a top-conversations table). |

**Kibana Dashboard:** We know LLM sometimes hallucinate. Under each answer, the app links every host IP mentions to
the traffic explorer dashboard. The link carries a KQL query (`source.ip:"10.10.8.77" or
destination.ip:"10.10.8.77"`) and the time window of the tool call that found the host. Each
tool call in the evidence panel also has an *open this slice in Kibana* link that applies the
same filters. If Kibana isn't reachable at `http://localhost:5601` from the browser, set
`KIBANA_PUBLIC_URL`.

## Quick start

```powershell
copy .env.example .env      # set GEMINI_API_KEY
docker compose up -d --build
docker compose logs -f es-setup   # backfill takes ~30s, then the collector starts
```

Open http://localhost:8501 and click a sample question.

## Dev container

The repo includes a VS Code dev container that attaches to the `flowwatch-app` service. Your
working copy is mounted at `/workspace`, and Streamlit reloads when you save a file.

1. **Install Docker and Docker Compose v2.** The easiest option is
   [Docker Desktop](https://www.docker.com/products/docker-desktop/), which includes both. If
   you use Homebrew's `docker` instead, run `brew install docker-compose`, then add this to
   `~/.docker/config.json` so `docker compose` works:

   ```json
   "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
   ```

   Install the **Dev Containers** extension (`ms-vscode-remote.remote-containers`). VS Code
   suggests it when you open the repo.

2. **Set `GEMINI_API_KEY`.** Copy `.env.example` to `.env` in the repo root and fill in the key.

3. **Validate the config and open the container.** Check that the compose files merge cleanly:

   ```sh
   docker compose -f docker-compose.yaml -f .devcontainer/docker-compose.devcontainer.yml config
   ```

   Then run **Dev Containers: Reopen in Container** from the Command Palette. The first start
   builds the images and waits for Elasticsearch and `es-setup`, so it takes a few minutes.
   Inside the container, run the pipeline tests from the Testing panel or with
   `cd pipeline && python -m unittest discover -s tests`.

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

## LLM

LLM uses "functional call" and the model can ask the app to run. FlowWatch describes each functional call to
Gemini by name, purpose and a JSON schema of its parameters (time window, group-by dimension,
filters). When a question comes in, Gemini doesn't read raw data, and apart from the
`run_aggregation` escape hatch it doesn't write queries itself. Instead it replies with
*function calls*, for example `top_talkers(start="2026-09-29T13:00",
dimension="conversation", filters={interface_out: "Gi0/0/1"})`.

The app turns each call
into an Elasticsearch aggregation request, runs it against the TSDS, and sends back a compact
JSON summary (Mbps, % of link capacity, peak times, baselines). Gemini can chain several calls - 
for example, first finding the congested link, then who used it, then comparing with normal
days, and it writes the answer only from those results. This keeps answers grounded in real
telemetry, keeps the questions to Elasticsearch efficient, and makes every step auditable: the
UI shows each tool call, the exact request body and the result the model saw.

| Tool | Aggregations used |
|---|---|
| `get_network_inventory` | Reads the inventory index, and uses `min` and `max` on `@timestamp` to report the available data range |
| `bandwidth_timeseries` | Groups by `terms`, splits each group into time buckets with `date_histogram`, and adds up bytes with `sum` |
| `top_talkers` | Ranks groups with `terms` (or `multi_terms` for conversations) by summed bytes, and finds each group's peak with `date_histogram` and `max_bucket` |
| `interface_utilization` | Splits traffic per interface and direction with `terms`, buckets it over time with `date_histogram`, takes the peak with `max_bucket` and the 95th percentile with `percentiles_bucket`, then compares both with link capacity |
| `compare_to_baseline` | Uses `filters` to pull the current window and the same time on previous comparable days in one request, groups each with `terms`, and compares the current value with the median of the earlier days |
| `traffic_trend` | Buckets traffic by calendar day in the site time zone with `date_histogram`, finds each day's busiest hour with a nested hourly `date_histogram` and `max_bucket`, then calculates the week-over-week change and a growth rate |
| `detect_anomalies` | Groups with `terms` and buckets with `date_histogram` across the window and the baseline days, then compares each bucket with the same time of day on earlier days |
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

## Reference

1. What is [functional call](https://medium.com/@jamestang/llm-function-calling-explained-a-deep-dive-into-the-request-and-response-payloads-894800fcad75)?