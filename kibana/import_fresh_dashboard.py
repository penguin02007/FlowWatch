import json
import uuid
import urllib.request
from pathlib import Path

root = Path(r'H:\Document\github\penguin02007\FlowWatch')
file_path = root / 'kibana' / 'fresh-netflow-dashboard.ndjson'

if not file_path.exists():
    raise FileNotFoundError(file_path)

boundary = f'----WebKitFormBoundary{uuid.uuid4().hex}'
body = []
body.append(f'--{boundary}\r\n'.encode())
body.append(b'Content-Disposition: form-data; name="file"; filename="fresh-netflow-dashboard.ndjson"\r\n')
body.append(b'Content-Type: application/octet-stream\r\n\r\n')
body.append(file_path.read_bytes())
body.append(f'\r\n--{boundary}--\r\n'.encode())
payload = b''.join(body)

req = urllib.request.Request(
    'http://localhost:5601/api/saved_objects/_import?overwrite=true',
    data=payload,
    headers={
        'kbn-xsrf': 'true',
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    },
    method='POST',
)

with urllib.request.urlopen(req, timeout=120) as resp:
    response_text = resp.read().decode('utf-8', 'replace')
    print('HTTP_STATUS', resp.status)
    print(response_text)
