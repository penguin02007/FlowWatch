# FlowWatch

## Quick stack health check

From the project root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\check-stack-health.ps1
```

This script checks:
- Elasticsearch cluster health
- Kibana API status
- Docker Compose service status
