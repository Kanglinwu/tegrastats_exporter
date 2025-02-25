#!/usr/bin/env python3

import time
from prometheus_client import start_http_server, Gauge

# 建立一個測試用的 gauge
test_gauge = Gauge("test_metric", "Just a test metric")

if __name__ == "__main__":
    # 1) 啟動 Prometheus 伺服器，監聽 port 8000
    start_http_server(8000)
    print("Tegrastats Exporter is running on port 8000...")

    # 2) 持續更新這個測試用 gauge
    while True:
        # 設定一個隨機/固定數值，這邊先簡單用 123
        test_gauge.set(123)
        # 休息 5 秒再更新一次
        time.sleep(5)