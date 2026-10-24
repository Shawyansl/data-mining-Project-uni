FROM docker.arvancloud.ir/library/python:3.12-slim
WORKDIR /app
COPY pipeline/requirements.txt /app/requirements.txt
RUN pip install --index-url=https://package-mirror.liara.ir/repository/pypi/simple --timeout=30 --retries 2 --no-cache-dir -r /app/requirements.txt
COPY pipeline/run.py /app/pipeline/run.py
COPY cars /app/cars
COPY real_estate /app/real_estate
ENTRYPOINT ["python", "/app/pipeline/run.py"]
