import os
import json
import streamlit as st
from elasticsearch import Elasticsearch
from google import genai
from generate_flows import inject_traffic_anomaly

es = Elasticsearch(
    os.getenv("ES_HOST", "http://elasticsearch:9200"),
    api_version="8.17",
    request_timeout=30,
)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))

st.set_page_config(page_title="NetOps Copilot", layout="wide")
st.title("Network Operations Copilot")

# Sidebar to trigger the 5-min demo injection
with st.sidebar:
    st.header("Demo Simulation Controls")
    if st.button("Inject Rsync Backup Anomaly"):
        inject_traffic_anomaly(target_host="filebeat")
        st.success("NetFlow spike injected into Filebeat!")

def fetch_top_flow_metrics():
    query = {
        "size": 0,
        "aggs": {
            "top_talkers": {
                "terms": {"field": "flow.src.ip.addr.keyword", "size": 3, "order": {"total_bytes": "desc"}},
                "aggs": {
                    "total_bytes": {"sum": {"field": "flow.in.bytes"}},
                    "top_dest_port": {"terms": {"field": "flow.dst.l4.port.id", "size": 1}}
                }
            }
        }
    }
    try:
        res = es.search(index="elastiflow-*", body=query)
        buckets = res.get("aggregations", {}).get("top_talkers", {}).get("buckets", [])
        return [
            {
                "source_ip": b['key'],
                "total_bytes": b['total_bytes']['value'],
                "dest_port": b['top_dest_port']['buckets'][0]['key'] if b['top_dest_port']['buckets'] else "Unknown"
            }
            for b in buckets
        ]
    except Exception as err:
        return f"Error retrieving flows: {err}"

user_query = st.chat_input("Ask a question about network congestion...")

if user_query:
    st.chat_message("user").write(user_query)
    
    flow_data = fetch_top_flow_metrics()
    
    prompt = f"""
    You are an expert NetOps Copilot. Use the following aggregated Elasticsearch NetFlow telemetry to diagnose the user's issue:
    Telemetry:
    {json.dumps(flow_data)}

    Question: '{user_query}'
    
    Answer concisely in 2 sentences. Identify the offending host, the application/port involved, and provide a direct root cause.
    """
    
    with st.spinner("Analyzing telemetry with Gemini..."):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            st.chat_message("assistant").write(response.text)
        except Exception as e:
            st.error(f"Inference error: {e}")
