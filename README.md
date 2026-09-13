# FlowWatch

## Verification

```powershell
powershell -ExecutionPolicy Bypass -File .\check-stack-health.ps1
curl.exe -sS "http://localhost:9200/_cat/indices"
curl.exe -sS "http://localhost:9200/elastiflow-*/_search?size=1&pretty"
cmd.exe /c "cd /d H:\Document\github\penguin02007\FlowWatch && python kibana\import_fresh_dashboard.py"
```

Open Kibana: http://localhost:5601
