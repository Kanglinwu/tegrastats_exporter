FROM arm64v8/python:3.10-slim

# 安裝 pip, prometheus_client (如果 python:3.10-slim 已有 python3-pip 就不用裝)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install prometheus_client

WORKDIR /app
COPY tegrastats_exporter.py /app/

ENTRYPOINT ["python3", "/app/tegrastats_exporter.py"]
