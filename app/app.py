"""FlowWatch: chat with LLM that queries NetFlow data in Elasticsearch."""
import json
import os
import re
from urllib.parse import quote

import altair as alt
import pandas as pd
import requests
import streamlit as st
from elasticsearch import Elasticsearch

from agent import NetOpsAgent
from functions import SITE_TZ, FlowFunctions, host_kql

GENERATOR_URL = os.getenv("GENERATOR_URL", "http://flow-generator:8000")
# Browser-facing Kibana address (links are opened by the viewer, not the container).
KIBANA_URL = os.getenv("KIBANA_PUBLIC_URL", "http://localhost:5601")
KIBANA_DASHBOARD = "flowwatch-traffic-explorer"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Categorical hues in fixed order (validated for CVD separation); never cycled.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SAMPLE_QUESTIONS = [
    "What is using the internet link right now, and is that normal for this time of day?",
    "Why was the GPU cluster slow earlier today?",
    "Did the nightly backup behave differently at any point in the last week?",
    "Is video conferencing traffic growing? When would the internet uplink hit 80% at peak?",
    "Has host 10.10.3.45 ever sent unusual amounts of data to the internet?",
    "Find the biggest traffic anomalies in the last 7 days and explain each one.",
]

st.set_page_config(page_title="FlowWatch", page_icon="📡", layout="wide")


@st.cache_resource
def get_functions() -> FlowFunctions:
    es = Elasticsearch(os.getenv("ES_HOST", "http://elasticsearch:9200"), request_timeout=30)
    return FlowFunctions(es)


@st.cache_resource
def get_agent() -> NetOpsAgent:
    return NetOpsAgent(get_functions())


def chart(spec: dict) -> None:
    rows = [
        {"time": pd.Timestamp(t, unit="ms", tz="UTC").tz_convert(SITE_TZ).tz_localize(None), "series": name, "value": v}
        for name, points in spec["series"].items() for t, v in points
    ]
    if not rows:
        return
    df = pd.DataFrame(rows)
    names = list(spec["series"])[: len(SERIES_COLORS)]
    df = df[df["series"].isin(names)]
    color = alt.Color("series:N", title=None, scale=alt.Scale(domain=names, range=SERIES_COLORS[: len(names)]),
                      legend=alt.Legend(orient="top") if len(names) > 1 else None)
    base = alt.Chart(df).encode(x=alt.X("time:T", title=None))
    lines = base.mark_line(strokeWidth=2, interpolate="monotone").encode(
        y=alt.Y("value:Q", title=spec["unit"]), color=color)
    hover = alt.selection_point(fields=["time"], nearest=True, on="pointerover", empty=False)
    points = base.mark_circle(size=64).encode(
        y="value:Q", color=color, opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("time:T", format="%a %m-%d %H:%M"), alt.Tooltip("series:N"),
                 alt.Tooltip("value:Q", format=",.1f", title=spec["unit"])],
    ).add_params(hover)
    rule = base.mark_rule(color="#8a8a86").encode(opacity=alt.condition(hover, alt.value(0.6), alt.value(0)))
    layers = [lines, rule, points]
    if spec.get("capacity"):
        cap = pd.DataFrame({"y": [spec["capacity"]], "label": [f"capacity {spec['capacity']:,.0f} Mbps"]})
        layers.append(alt.Chart(cap).mark_rule(strokeDash=[4, 4], color="#8a8a86").encode(y="y:Q"))
        layers.append(alt.Chart(cap).mark_text(align="left", dx=4, dy=-6, color="#52514e").encode(
            y="y:Q", x=alt.value(0), text="label:N"))
    st.caption(spec["title"])
    st.altair_chart(alt.layer(*layers).properties(height=240), width="stretch")


def rison(text: str) -> str:
    return "'" + text.replace("!", "!!").replace("'", "!'") + "'"


def kibana_url(kql: str, start: str, end: str) -> str:
    g = f"(time:(from:{rison(start)},to:{rison(end)}))"
    a = f"(query:(language:kuery,query:{rison(kql)}))"
    safe = "(),:'!*"
    return f"{KIBANA_URL}/app/dashboards#/view/{KIBANA_DASHBOARD}?_g={quote(g, safe=safe)}&_a={quote(a, safe=safe)}"


def _host_window(ip: str, steps: list[dict]) -> tuple[str, str]:
    """Narrowest function-call window whose result mentions the host, padded for context."""
    windows = (
        [s["kibana"] for s in steps if s.get("kibana") and ip in json.dumps(s["result"])]
        or [s["kibana"] for s in steps if s.get("kibana")]
    )
    w = min(windows, key=lambda k: pd.Timestamp(k["to"]) - pd.Timestamp(k["from"]))
    start, end = pd.Timestamp(w["from"]), pd.Timestamp(w["to"])
    pad = max((end - start) / 2, pd.Timedelta(hours=1))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start - pad).strftime(fmt), min(end + pad, pd.Timestamp.now(tz="UTC")).strftime(fmt)


def render_kibana_links(answer: str, steps: list[dict]) -> None:
    """Deep-link every host the answer mentions into the Kibana traffic explorer."""
    if not any(s.get("kibana") for s in steps):
        return
    links = []
    for ip in list(dict.fromkeys(IPV4.findall(answer)))[:6]:
        start, end = _host_window(ip, steps)
        links.append(f"[{ip}](<{kibana_url(host_kql(ip), start, end)}>)")
    start, end = _host_window("", steps)
    links.append(f"[all traffic](<{kibana_url('', start, end)}>)")
    st.markdown("📈 **Graphs in Kibana:** " + " · ".join(links))


def render_steps(steps: list[dict]) -> None:
    for spec in [c for s in steps for c in s.get("charts", [])][-2:]:
        chart(spec)
    if not steps:
        return
    n_queries = sum(len(s.get("queries", [])) for s in steps)
    with st.expander(f"🔎 {len(steps)} function calls · {n_queries} Elasticsearch aggregation requests"):
        for i, step in enumerate(steps, 1):
            took = sum(q.get("took_ms") or 0 for q in step.get("queries", []))
            link = ""
            if step.get("kibana"):
                k = step["kibana"]
                link = f" · [open this slice in Kibana](<{kibana_url(k['kql'], k['from'], k['to'])}>)"
            st.markdown(f"**{i}. `{step['function']}`** · {took} ms in Elasticsearch{link}")
            st.code(json.dumps(step["args"], indent=2), language="json")
            tabs = st.tabs(["Result sent to the LLM", "Elasticsearch request"])
            with tabs[0]:
                st.json(step["result"], expanded=False)
            with tabs[1]:
                for q in step.get("queries", []):
                    st.code(f"GET {q['index']}/_search\n{json.dumps(q['body'], indent=2)}", language="json")


def sidebar() -> None:
    with st.sidebar:
        st.subheader("Pipeline")
        try:
            status = get_functions().pipeline_status()
            age = status["last_age_s"]
            col1, col2 = st.columns(2)
            col1.metric("Rollup docs", f"{status['docs']:,}")
            col2.metric("Last 5 min", f"{status['recent_mbps']:,.0f} Mbps")
            live = age is not None and age < 180
            st.caption(f"{'🟢 Live' if live else '🟠 Stale'} · newest bucket {age}s ago · history from {status['first']}")
        except Exception as err:
            st.warning(f"Elasticsearch not ready: {err}")

        st.subheader("Inject a live incident")
        st.caption("Sends real NetFlow/IPFIX through the pipeline. Data shows up after about 2 minutes.")
        try:
            scenarios = requests.get(f"{GENERATOR_URL}/scenarios", timeout=3).json()
        except requests.RequestException:
            scenarios = []
            st.info("Generator control API unreachable.")
        for sc in scenarios:
            with st.container(border=True):
                st.markdown(f"**{sc['title']}**")
                st.caption(sc["description"])
                if sc["active"]:
                    ends = pd.Timestamp(sc["ends_at"]).tz_convert(SITE_TZ).strftime("%H:%M")
                    st.markdown(f"▶️ **Running** until {ends}")
                    if st.button("Stop", key=f"stop-{sc['name']}"):
                        requests.delete(f"{GENERATOR_URL}/scenarios/{sc['name']}", timeout=3)
                        st.rerun()
                elif st.button("Start for 15 min", key=f"start-{sc['name']}"):
                    requests.post(f"{GENERATOR_URL}/scenarios/{sc['name']}?minutes=15", timeout=3)
                    st.rerun()

        st.subheader("Try asking")
        for q in SAMPLE_QUESTIONS:
            if st.button(q, key=f"q-{q}", width="stretch"):
                st.session_state.pending = q
        if st.button("🧹 New conversation", width="stretch"):
            st.session_state.history, st.session_state.messages = [], []
            st.rerun()


def main() -> None:
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("messages", [])
    sidebar()

    st.title("📡 FlowWatch")
    st.caption(f"Site time zone: {SITE_TZ}")
    if not os.getenv("GEMINI_API_KEY"):
        st.error("GEMINI_API_KEY is not set. Add it to .env and restart flowwatch-app.")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                render_steps(msg.get("steps", []))
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                render_kibana_links(msg["content"], msg.get("steps", []))

    question = st.chat_input("Ask about bandwidth, congestion, top talkers, trends…") or st.session_state.pop("pending", None)
    if not question:
        return
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        status = st.status("Thinking…", expanded=True)

        def on_step(step: dict) -> None:
            args = ", ".join(f"{k}={json.dumps(v)}" for k, v in step["args"].items())
            error = step["result"].get("error") if isinstance(step["result"], dict) else None
            status.write(f"{'⚠️' if error else '🔧'} `{step['function']}({args})`" + (f" → {error}" if error else ""))
            status.update(label=f"Querying Elasticsearch… ({step['function']})")

        try:
            answer, steps = get_agent().ask(st.session_state.history, question, on_step)
        except Exception as err:
            status.update(label="Failed", state="error")
            st.error(f"LLM error: {err}")
            return
        status.update(label=f"Done: {len(steps)} function calls", state="complete", expanded=False)
        render_steps(steps)
        st.markdown(answer)
        render_kibana_links(answer, steps)
    st.session_state.messages.append({"role": "assistant", "content": answer, "steps": steps})


main()
