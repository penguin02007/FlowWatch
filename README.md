# FlowWatch

## Verification

Use the official ElastiFlow saved objects from the docs:
https://docs.elastiflow.com/flowcoll/configuration/outputs/output_elasticsearch

```powershell
powershell -ExecutionPolicy Bypass -File .\check-stack-health.ps1
curl.exe -sS "http://localhost:9200/_cat/indices"
curl.exe -sS "http://localhost:9200/elastiflow-*/_search?size=1&pretty"
cmd.exe /c "cd /d H:\Document\github\penguin02007\FlowWatch && python kibana\import_elastiflow_saved_objects.py"
```

Open Kibana: http://localhost:5601
