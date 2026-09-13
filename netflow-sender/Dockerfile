FROM python:3.11-slim

WORKDIR /app
COPY send_netflow_v5.py /app/send_netflow_v5.py
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "/app/send_netflow_v5.py"]
