#!/usr/bin/env python3

import re
import subprocess
import time
import os
from collections import defaultdict
from prometheus_client import start_http_server, Gauge
from dotenv import load_dotenv  # 用於讀取 .env

load_dotenv()
MY_HOSTNAME = os.getenv("HOSTNAME", "unknown")

########################################
# 1) 定義 Prometheus 指標
########################################

# CPU usage (per core)
CPU_USAGE_GAUGE = Gauge("jetson_cpu_usage_percent",
                        "Average CPU usage in percent over interval",
                        ["core", "Hostname"])

# GPU usage
GPU_USAGE_GAUGE = Gauge("jetson_gpu_usage_percent_max",
                        "Max GPU usage in percent over interval",
                        ["Hostname"])

# GPU freq
GPU_FREQ_GAUGE = Gauge("jetson_gpu_freq_mhz_avg",
                       "Average GPU frequency in MHz over interval",
                       ["Hostname"])

# RAM / SWAP
RAM_USED_GAUGE = Gauge("jetson_ram_used_mb_avg",
                       "Average used RAM in MB over interval",
                       ["Hostname"])
RAM_TOTAL_GAUGE = Gauge("jetson_ram_total_mb_avg",
                        "Average total RAM in MB over interval",
                        ["Hostname"])
SWAP_USED_GAUGE = Gauge("jetson_swap_used_mb_avg",
                        "Average used SWAP in MB over interval",
                        ["Hostname"])
SWAP_TOTAL_GAUGE = Gauge("jetson_swap_total_mb_avg",
                         "Average total SWAP in MB over interval",
                         ["Hostname"])

# === 新增：GPU 溫度、各 Rail 功耗 (取平均) ===
JETSON_GPU_TEMP_C = Gauge("jetson_gpu_temp_c_avg",
                          "Average GPU temperature in Celsius over interval",
                          ["Hostname"])

JETSON_POWER_VDD_IN_W = Gauge("jetson_power_vdd_in_w_avg",
                              "Average power usage of VDD_IN (W) over interval",
                              ["Hostname"])
JETSON_POWER_VDD_CPU_GPU_CV_W = Gauge("jetson_power_vdd_cpu_gpu_cv_w_avg",
                                      "Average power usage of VDD_CPU_GPU_CV (W) over interval",
                                      ["Hostname"])
JETSON_POWER_VDD_SOC_W = Gauge("jetson_power_vdd_soc_w_avg",
                               "Average power usage of VDD_SOC (W) over interval",
                               ["Hostname"])


########################################
# 2) Aggregator，存放區間資料並在 flush 時做平均 / 最大值
########################################
class MetricsAggregator:
    def __init__(self, interval=5.0):
        self.interval = interval
        self.reset()

    def reset(self):
        self.start_time = time.time()

        self.cpu_usage_records = defaultdict(list)
        self.gpu_usage_records = []
        self.gpu_freq_records = []

        self.ram_used_records = []
        self.ram_total_records = []
        self.swap_used_records = []
        self.swap_total_records = []

        # 新增：暫存 GPU Temp、電源功耗
        self.gpu_temp_records = []
        self.vdd_in_records = []
        self.vdd_cpu_gpu_cv_records = []
        self.vdd_soc_records = []

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

    # 新增：方法以記錄 GPU Temp, VDD_IN, ...
    def add_gpu_temp(self, temp_c):
        self.gpu_temp_records.append(temp_c)

    def add_vdd_in(self, w):
        self.vdd_in_records.append(w)

    def add_vdd_cpu_gpu_cv(self, w):
        self.vdd_cpu_gpu_cv_records.append(w)

    def add_vdd_soc(self, w):
        self.vdd_soc_records.append(w)

    def should_flush(self):
        return (time.time() - self.start_time) >= self.interval

    def flush_to_prometheus(self):
        # 1) CPU usage => 取平均
        for core_idx, usage_list in self.cpu_usage_records.items():
            avg_usage = sum(usage_list)/len(usage_list) if usage_list else 0
            CPU_USAGE_GAUGE.labels(core=str(core_idx), Hostname=MY_HOSTNAME).set(avg_usage)

        # 2) GPU usage => 最大值
        max_gpu_usage = max(self.gpu_usage_records) if self.gpu_usage_records else 0
        GPU_USAGE_GAUGE.labels(Hostname=MY_HOSTNAME).set(max_gpu_usage)

        # 3) GPU freq => 平均
        avg_gpu_freq = sum(self.gpu_freq_records)/len(self.gpu_freq_records) if self.gpu_freq_records else 0
        GPU_FREQ_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_gpu_freq)

        # 4) RAM / SWAP => 平均
        def avg_list(lst):
            return sum(lst)/len(lst) if lst else 0

        RAM_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.ram_used_records))
        RAM_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.ram_total_records))
        SWAP_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.swap_used_records))
        SWAP_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.swap_total_records))

        # 新增： GPU Temp => 平均
        avg_temp = avg_list(self.gpu_temp_records)
        JETSON_GPU_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_temp)

        # 新增： VDD_IN / VDD_CPU_GPU_CV / VDD_SOC => 平均
        JETSON_POWER_VDD_IN_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_in_records))
        JETSON_POWER_VDD_CPU_GPU_CV_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_cpu_gpu_cv_records))
        JETSON_POWER_VDD_SOC_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_soc_records))

        # reset for next interval
        self.reset()


########################################
# 3) parse tegrastats line，新增溫度 & 電源解析
########################################
def parse_tegrastats_line(line, aggregator: MetricsAggregator):
    # e.g.:
    # RAM 3121/7620MB ... CPU [xx%@xx,...] GR3D_FREQ 0% cpu@57.625C ... gpu@57.906C ...
    # VDD_IN 9349mW/9349mW VDD_CPU_GPU_CV 2764mW/2764mW VDD_SOC 2768mW/2768mW

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

    # 3) CPU usage
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

    # 4) GPU usage & freq => e.g. "GR3D_FREQ 0%" or "GR3D_FREQ 89%@998"
    m_gpu = re.search(r"GR3D_FREQ\s+(\d+)%(@(\d+))?", line)
    if m_gpu:
        usage = float(m_gpu.group(1))
        aggregator.add_gpu_usage(usage)
        freq_str = m_gpu.group(3)
        if freq_str:  # means there's a number after '@'
            aggregator.add_gpu_freq(float(freq_str))

    # 5) GPU temp => parse e.g. "gpu@57.906C"
    m_gpu_temp = re.search(r"gpu@(\d+(\.\d+)?)C", line)
    if m_gpu_temp:
        temp_c = float(m_gpu_temp.group(1))
        aggregator.add_gpu_temp(temp_c)

    # 6) VDD_IN => parse e.g. "VDD_IN 9349mW/9349mW"
    #    只取第一個 (即時值) => 9349 -> 9.349W
    m_vdd_in = re.search(r"VDD_IN\s+(\d+)mW/", line)
    if m_vdd_in:
        w_in = float(m_vdd_in.group(1)) / 1000.0
        aggregator.add_vdd_in(w_in)

    # 7) VDD_CPU_GPU_CV => parse e.g. "VDD_CPU_GPU_CV 2764mW/2764mW"
    m_vdd_cpu_gpu_cv = re.search(r"VDD_CPU_GPU_CV\s+(\d+)mW/", line)
    if m_vdd_cpu_gpu_cv:
        w_cpu_gpu_cv = float(m_vdd_cpu_gpu_cv.group(1)) / 1000.0
        aggregator.add_vdd_cpu_gpu_cv(w_cpu_gpu_cv)

    # 8) VDD_SOC => parse e.g. "VDD_SOC 2768mW/2768mW"
    m_vdd_soc = re.search(r"VDD_SOC\s+(\d+)mW/", line)
    if m_vdd_soc:
        w_soc = float(m_vdd_soc.group(1)) / 1000.0
        aggregator.add_vdd_soc(w_soc)


########################################
# 4) 主程式
########################################
def main():
    start_http_server(8000)
    print("Tegrastats Exporter (with aggregator) is running on port 8000...")

    aggregator = MetricsAggregator(interval=5.0)

    popen = subprocess.Popen(["tegrastats", "--interval", "1000"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             universal_newlines=True)
    try:
        for line in popen.stdout:
            line = line.strip()
            parse_tegrastats_line(line, aggregator)

            if aggregator.should_flush():
                aggregator.flush_to_prometheus()

    except KeyboardInterrupt:
        pass
    finally:
        popen.terminate()
        popen.wait()

if __name__ == "__main__":
    main()