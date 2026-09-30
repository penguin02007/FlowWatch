FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY flowwatch ./flowwatch
COPY tests ./tests
CMD ["python", "-m", "flowwatch.collector"]
