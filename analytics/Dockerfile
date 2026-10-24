FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip --default-timeout=120 install --retries 10 --no-cache-dir -r requirements.txt
COPY ingest.py .
ENTRYPOINT ["python", "/app/ingest.py"]
