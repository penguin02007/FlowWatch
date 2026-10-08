#!/usr/bin/env bash
# FlowWatch stack health: Elasticsearch, Kibana, flow freshness, and container status.
set -u

ES=http://localhost:9200
KIBANA=http://localhost:5601

green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

echo '=== FlowWatch stack health ==='

es_status=$(curl -s -m 20 "$ES/_cat/health?h=status" | tr -d '[:space:]')
case "$es_status" in
  green|yellow) green "ELASTICSEARCH: OK ($es_status)" ;;
  "") red "ELASTICSEARCH: FAIL (no response from $ES)" ;;
  *) yellow "ELASTICSEARCH: WARNING ($es_status)" ;;
esac

kibana_level=$(curl -s -m 20 "$KIBANA/api/status" | grep -o '"overall":{"level":"[a-z]*"' | sed 's/.*"level":"//; s/"$//')
case "$kibana_level" in
  available) green "KIBANA: OK ($kibana_level)" ;;
  "") red "KIBANA: FAIL (no response from $KIBANA)" ;;
  *) yellow "KIBANA: WARNING ($kibana_level)" ;;
esac

last=$(curl -s -m 20 -H 'Content-Type: application/json' \
  "$ES/metrics-netflow.flows-*/_search?size=0&filter_path=aggregations.last.value_as_string" \
  -d '{"aggs":{"last":{"max":{"field":"@timestamp"}}}}' | sed -n 's/.*"value_as_string":"\([^"]*\)".*/\1/p')
if [ -z "$last" ]; then
  red "FLOW TSDS: FAIL (no flow data found)"
else
  ts=${last%%.*}; ts=${ts%Z}
  # BSD date (macOS) first, then GNU date (Linux).
  epoch=$(date -j -u -f '%Y-%m-%dT%H:%M:%S' "$ts" +%s 2>/dev/null || date -u -d "$ts" +%s)
  age=$(( $(date -u +%s) - epoch ))
  if [ "$age" -lt 180 ]; then
    green "FLOW TSDS: OK (newest bucket $last, ${age}s ago)"
  else
    yellow "FLOW TSDS: STALE (newest bucket $last, ${age}s ago) - check flow-collector logs"
  fi
fi

echo '---'
echo 'Docker services:'
docker compose ps -a --format 'table {{.Name}}\t{{.State}}\t{{.Status}}'
