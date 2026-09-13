import os
import uuid
import urllib.request
from pathlib import Path

ROOT = Path(r'H:\Document\github\penguin02007\FlowWatch')
OUT_PATH = ROOT / 'kibana' / 'elastiflow-8.14.x-flow-codex.ndjson'
URL = 'https://raw.githubusercontent.com/elastiflow/elastiflow_for_elasticsearch/master/kibana/flow/kibana-8.14.x-flow-codex.ndjson'


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f'Downloading ElastiFlow saved objects from {url}')
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    target.write_bytes(data)
    print(f'Saved to {target} ({len(data)} bytes)')


def import_saved_objects(file_path: Path, kibana_url: str = 'http://localhost:5601') -> str:
    boundary = f'----WebKitFormBoundary{uuid.uuid4().hex}'
    body = []
    body.append(f'--{boundary}\r\n'.encode())
    body.append(b'Content-Disposition: form-data; name="file"; filename="elastiflow-8.14.x-flow-codex.ndjson"\r\n')
    body.append(b'Content-Type: application/octet-stream\r\n\r\n')
    body.append(file_path.read_bytes())
    body.append(f'\r\n--{boundary}--\r\n'.encode())
    payload = b''.join(body)

    req = urllib.request.Request(
        f'{kibana_url}/api/saved_objects/_import?overwrite=true',
        data=payload,
        headers={
            'kbn-xsrf': 'true',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        response_text = resp.read().decode('utf-8', 'replace')
        print(f'HTTP_STATUS {resp.status}')
        print(response_text)
        return response_text


if __name__ == '__main__':
    if not OUT_PATH.exists():
        download_file(URL, OUT_PATH)

    kibana_url = os.environ.get('KIBANA_URL', 'http://localhost:5601')
    import_saved_objects(OUT_PATH, kibana_url)
