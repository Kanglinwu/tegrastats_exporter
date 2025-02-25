#!/usr/bin/env python3

import re
import subprocess
import time
from collections import defaultdict, deque
from prometheus_client import start_http_server, Gauge
# 讀取 .env 用
from dotenv import load_dotenv  

# 讀取當前目錄下 .env 的內容
load_dotenv()
# 取得 .env 裡的 HOSTNAME 值，若不存在就用 "unknown"
MY_HOSTNAME = os.getenv("HOSTNAME", "unknown")

########################################
# 1) 定義我們要對外暴露的 Prometheus 指標
########################################

# CPU 使用率，帶 core label。因為最終我們會每 5 秒更新一次(平均值)。
CPU_USAGE_GAUGE = Gauge("jetson_cpu_usage_percent", "Average CPU usage in percent over interval", ["core", "Hostname"])

# GPU 使用率，取區間「最高值」
GPU_USAGE_GAUGE = Gauge("jetson_gpu_usage_percent_max", "Max GPU usage in percent over interval", ["Hostname"])

# GPU 頻率，這裡示範取平均（也可改成區間最大/最後一筆等）
GPU_FREQ_GAUGE  = Gauge("jetson_gpu_freq_mhz_avg", "Average GPU frequency in MHz over interval", ["Hostname"])

# RAM/Swap，示範取平均使用量
RAM_USED_GAUGE = Gauge("jetson_ram_used_mb_avg", "Average used RAM in MB over interval", ["Hostname"])
RAM_TOTAL_GAUGE = Gauge("jetson_ram_total_mb_avg", "Average total RAM in MB over interval", ["Hostname"])
SWAP_USED_GAUGE = Gauge("jetson_swap_used_mb_avg", "Average used SWAP in MB over interval", ["Hostname"])
SWAP_TOTAL_GAUGE= Gauge("jetson_swap_total_mb_avg", "Average total SWAP in MB over interval", ["Hostname"])


########################################
# 2) 建立一個暫存結構，存放區間內(例如 5 秒)的資料
########################################
class MetricsAggregator:
    def __init__(self, interval=5.0):
        """
        interval: 幾秒做一次彙整/更新 Prometheus
        """
        self.interval = interval
        self.reset()

    def reset(self):
        # 記錄開始時間
        self.start_time = time.time()

        # CPU usage: 用 {core_id: [usage, usage, ...]} 來暫存
        self.cpu_usage_records = defaultdict(list)
        # GPU usage 與 freq：各是一個 list，可能之後要取 max / avg
        self.gpu_usage_records = []
        self.gpu_freq_records = []
        # RAM / SWAP
        self.ram_used_records = []
        self.ram_total_records = []
        self.swap_used_records = []
        self.swap_total_records = []

    def add_cpu_usage(self, core_idx, usage):
        self.cpu_usage_records[core_idx].append(usage)

    def add_gpu_usage(self, usage):
        self.gpu_usage_records.append(usage)

    def add_gpu_freq(self, freq):
        self.gpu_freq_records.append(freq)

    def add_ram(self, used, total):
        self.ram_used_records.append(used)
        self.ram_total_records.append(total)

    def add_swap(self, used, total):
        self.swap_used_records.append(used)
        self.swap_total_records.append(total)

    def should_flush(self):
        """檢查是否已到達 interval，若超過就回傳 True"""
        return (time.time() - self.start_time) >= self.interval

    def flush_to_prometheus(self):
        """
        每次到達 interval 時，做彙整 (平均 / 最大 / 其他邏輯)，
        然後更新到對應的 Prometheus Gauge。
        """
        # 1) CPU usage: 取該段時間的「平均」
        for core_idx, usage_list in self.cpu_usage_records.items():
            if usage_list:
                avg_usage = sum(usage_list) / len(usage_list)
            else:
                avg_usage = 0
            CPU_USAGE_GAUGE.labels(core=str(core_idx), Hostname=MY_HOSTNAME).set(avg_usage)

        # 2) GPU usage: 這裡示範取「最高值」
        if self.gpu_usage_records:
            max_gpu_usage = max(self.gpu_usage_records)
        else:
            max_gpu_usage = 0
        GPU_USAGE_GAUGE.labels(Hostname=MY_HOSTNAME).set(max_gpu_usage)

        # 3) GPU freq: 取「平均」
        if self.gpu_freq_records:
            avg_gpu_freq = sum(self.gpu_freq_records) / len(self.gpu_freq_records)
        else:
            avg_gpu_freq = 0
        GPU_FREQ_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_gpu_freq)

        # 4) RAM/Swap: 取「平均」
        def average_of_list(lst):
            return sum(lst) / len(lst) if lst else 0

        RAM_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(average_of_list(self.ram_used_records))
        RAM_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(average_of_list(self.ram_total_records))
        SWAP_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(average_of_list(self.swap_used_records))
        SWAP_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(average_of_list(self.swap_total_records))

        # 最後重置，以便進入下一個 interval
        self.reset()


########################################
# 3) 解析 tegrastats 每行輸出，存入 MetricsAggregator
########################################
def parse_tegrastats_line(line, aggregator: MetricsAggregator):
    """
    line 範例:
      RAM 3164/7620MB (lfb 29x4MB) SWAP 51/3810MB (cached 0MB) CPU [37%@1344,34%@1344,25%@1344,64%@1344,99%@1344,15%@1344] GR3D_FREQ 0%
      cpu@57.125C soc2@55.156C ...
    """

    # 1) RAM
    m_ram = re.search(r"RAM\s+(\d+)/(\d+)MB", line)
    if m_ram:
        used = float(m_ram.group(1))
        total = float(m_ram.group(2))
        aggregator.add_ram(used, total)

    # 2) SWAP
    m_swap = re.search(r"SWAP\s+(\d+)/(\d+)MB", line)
    if m_swap:
        used = float(m_swap.group(1))
        total = float(m_swap.group(2))
        aggregator.add_swap(used, total)

    # 3) CPU usage e.g. "CPU [37%@1344,34%@1344,25%@1344,...]"
    cpu_block = re.search(r"CPU \[(.*?)\]", line)
    if cpu_block:
        cores_str = cpu_block.group(1).split(",")
        for idx, core_info in enumerate(cores_str):
            core_info = core_info.strip()
            if core_info.startswith("off"):
                aggregator.add_cpu_usage(idx, 0)
            else:
                m_core = re.match(r"(\d+)%@(\d+)", core_info)
                if m_core:
                    usage = float(m_core.group(1))
                    aggregator.add_cpu_usage(idx, usage)

    # 4) GPU usage & freq e.g. "GR3D_FREQ 89%"
    # 有時還會帶 "@998" 頻率： "GR3D_FREQ 89%@998"
    m_gpu = re.search(r"GR3D_FREQ\s+(\d+)%(@(\d+))?", line)
    if m_gpu:
        usage = float(m_gpu.group(1))
        aggregator.add_gpu_usage(usage)
        # 如果有擷取到頻率
        freq_str = m_gpu.group(3)  # 第三個 capture group
        if freq_str:
            aggregator.add_gpu_freq(float(freq_str))
        else:
            # 也可能這行沒印 freq，你可視情況決定是否保留舊值或不紀錄
            pass


########################################
# 4) 主程式：啟動 Prometheus server, 執行 tegrastats, 每秒讀一行
########################################
def main():
    # (a) 啟動 Prometheus HTTP server
    start_http_server(8000)
    print("Tegrastats Exporter (with aggregator) is running on port 8000...")

    # (b) 建立 aggregator，設定「每 5 秒 flush 一次」
    aggregator = MetricsAggregator(interval=10.0)

    # (c) 以 subprocess 執行 tegrastats，1秒間隔
    popen = subprocess.Popen(["tegrastats", "--interval", "1000"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)

    try:
        for line in popen.stdout:
            line = line.strip()
            # 解析並暫存
            parse_tegrastats_line(line, aggregator)

            # 每行都檢查是否到達 flush 時間
            if aggregator.should_flush():
                aggregator.flush_to_prometheus()

    except KeyboardInterrupt:
        pass
    finally:
        popen.terminate()
        popen.wait()

if __name__ == "__main__":
    main()