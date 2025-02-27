#!/usr/bin/env python3

import re
import subprocess
import time
import os
import psutil  # 用於獲取磁碟資訊
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

# =========== 新增多種溫度 ===========
JETSON_GPU_TEMP_C = Gauge("jetson_gpu_temp_c_avg",
                          "Average GPU temperature in Celsius over interval",
                          ["Hostname"])
JETSON_CPU_TEMP_C = Gauge("jetson_cpu_temp_c_avg",
                          "Average CPU temperature in Celsius over interval",
                          ["Hostname"])
JETSON_SOC0_TEMP_C = Gauge("jetson_soc0_temp_c_avg",
                           "Average soc0 temperature in Celsius over interval",
                           ["Hostname"])
JETSON_SOC1_TEMP_C = Gauge("jetson_soc1_temp_c_avg",
                           "Average soc1 temperature in Celsius over interval",
                           ["Hostname"])
JETSON_SOC2_TEMP_C = Gauge("jetson_soc2_temp_c_avg",
                           "Average soc2 temperature in Celsius over interval",
                           ["Hostname"])
JETSON_TJ_TEMP_C = Gauge("jetson_tj_temp_c_avg",
                         "Average tj temperature in Celsius over interval",
                         ["Hostname"])

# Rail 功耗
JETSON_POWER_VDD_IN_W = Gauge("jetson_power_vdd_in_w_avg",
                              "Average power usage of VDD_IN (W) over interval",
                              ["Hostname"])
JETSON_POWER_VDD_CPU_GPU_CV_W = Gauge("jetson_power_vdd_cpu_gpu_cv_w_avg",
                                      "Average power usage of VDD_CPU_GPU_CV (W) over interval",
                                      ["Hostname"])
JETSON_POWER_VDD_SOC_W = Gauge("jetson_power_vdd_soc_w_avg",
                               "Average power usage of VDD_SOC (W) over interval",
                               ["Hostname"])

# 磁碟使用率
DISK_USAGE_GAUGE = Gauge("jetson_disk_usage_percent",
                         "Disk usage in percent",
                         ["mount", "Hostname"])
DISK_USED_GAUGE = Gauge("jetson_disk_used_mb",
                        "Disk used space in MB",
                        ["mount", "Hostname"])
DISK_TOTAL_GAUGE = Gauge("jetson_disk_total_mb",
                         "Total disk space in MB",
                         ["mount", "Hostname"])

########################################
# 2) Aggregator: 每個傳感器 / 欄位都對應一個 list
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

        # =========== 新增：暫存各種溫度 ===========
        self.gpu_temp_records = []
        self.cpu_temp_records = []
        self.soc0_temp_records = []
        self.soc1_temp_records = []
        self.soc2_temp_records = []
        self.tj_temp_records = []

        # Rail Power
        self.vdd_in_records = []
        self.vdd_cpu_gpu_cv_records = []
        self.vdd_soc_records = []

    # ---- Usage / freq / ram / swap ----
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

    # ---- 溫度 ----
    def add_gpu_temp(self, temp_c):
        self.gpu_temp_records.append(temp_c)

    def add_cpu_temp(self, temp_c):
        self.cpu_temp_records.append(temp_c)

    def add_soc0_temp(self, temp_c):
        self.soc0_temp_records.append(temp_c)

    def add_soc1_temp(self, temp_c):
        self.soc1_temp_records.append(temp_c)

    def add_soc2_temp(self, temp_c):
        self.soc2_temp_records.append(temp_c)

    def add_tj_temp(self, temp_c):
        self.tj_temp_records.append(temp_c)

    # ---- Power ----
    def add_vdd_in(self, w):
        self.vdd_in_records.append(w)

    def add_vdd_cpu_gpu_cv(self, w):
        self.vdd_cpu_gpu_cv_records.append(w)

    def add_vdd_soc(self, w):
        self.vdd_soc_records.append(w)

    def should_flush(self):
        return (time.time() - self.start_time) >= self.interval

    def flush_to_prometheus(self):
        def avg_list(lst):
            return sum(lst) / len(lst) if lst else 0

        # 1-1) CPU usage => 取平均
        total_cpu_usage = []  # 用來存儲所有 core 的數據

        # 1) CPU usage => 取平均
        for core_idx, usage_list in self.cpu_usage_records.items():
            avg_usage = avg_list(usage_list)
            CPU_USAGE_GAUGE.labels(core=str(core_idx), Hostname=MY_HOSTNAME).set(avg_usage)
            total_cpu_usage.extend(usage_list)  # 把每個 core 的使用率加入總體計算

        # 計算整體 CPU 平均使用率
        avg_total_cpu_usage = avg_list(total_cpu_usage)
        CPU_USAGE_GAUGE.labels(core="total", Hostname=MY_HOSTNAME).set(avg_total_cpu_usage)

        # 2) GPU usage => 最大值
        max_gpu_usage = max(self.gpu_usage_records) if self.gpu_usage_records else 0
        GPU_USAGE_GAUGE.labels(Hostname=MY_HOSTNAME).set(max_gpu_usage)

        # 3) GPU freq => 平均
        avg_gpu_freq = avg_list(self.gpu_freq_records)
        GPU_FREQ_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_gpu_freq)

        # 4) RAM / SWAP => 平均
        RAM_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.ram_used_records))
        RAM_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.ram_total_records))
        SWAP_USED_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.swap_used_records))
        SWAP_TOTAL_GAUGE.labels(Hostname=MY_HOSTNAME).set(avg_list(self.swap_total_records))

        # =========== Flush 多種溫度 ===========
        JETSON_GPU_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.gpu_temp_records))
        JETSON_CPU_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.cpu_temp_records))
        JETSON_SOC0_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.soc0_temp_records))
        JETSON_SOC1_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.soc1_temp_records))
        JETSON_SOC2_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.soc2_temp_records))
        JETSON_TJ_TEMP_C.labels(Hostname=MY_HOSTNAME).set(avg_list(self.tj_temp_records))

        # Rails
        JETSON_POWER_VDD_IN_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_in_records))
        JETSON_POWER_VDD_CPU_GPU_CV_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_cpu_gpu_cv_records))
        JETSON_POWER_VDD_SOC_W.labels(Hostname=MY_HOSTNAME).set(avg_list(self.vdd_soc_records))

        # 4) 磁碟資訊 - 只監測 /host
        try:
            usage = psutil.disk_usage("/host")  # 監測宿主機的 /
            DISK_USAGE_GAUGE.labels(mount="/", Hostname=MY_HOSTNAME).set(usage.percent)
            DISK_USED_GAUGE.labels(mount="/", Hostname=MY_HOSTNAME).set(usage.used / 1e6)  # 轉 MB
            DISK_TOTAL_GAUGE.labels(mount="/", Hostname=MY_HOSTNAME).set(usage.total / 1e6)  # 轉 MB
        except PermissionError:
            print("無法存取 /host，請確認是否正確掛載！")

        self.reset()


########################################
# 3) 解析 tegrastats 輸出，匹配溫度
########################################
def parse_tegrastats_line(line, aggregator: MetricsAggregator):
    # e.g:
    # cpu@58.875C soc2@56.781C soc0@57.75C gpu@58.843C tj@58.875C soc1@58.875C
    # ...
    # 你也可以把 parse 拆成好幾個 re.search()

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
        if freq_str:
            aggregator.add_gpu_freq(float(freq_str))

    # 5) 各種溫度: cpu@xxC, gpu@xxC, soc0@xxC, soc1@xxC, soc2@xxC, tj@xxC
    m_cpu_temp = re.search(r"cpu@(\d+(\.\d+)?)C", line)
    if m_cpu_temp:
        aggregator.add_cpu_temp(float(m_cpu_temp.group(1)))

    m_gpu_temp = re.search(r"gpu@(\d+(\.\d+)?)C", line)
    if m_gpu_temp:
        aggregator.add_gpu_temp(float(m_gpu_temp.group(1)))

    m_soc0_temp = re.search(r"soc0@(\d+(\.\d+)?)C", line)
    if m_soc0_temp:
        aggregator.add_soc0_temp(float(m_soc0_temp.group(1)))

    m_soc1_temp = re.search(r"soc1@(\d+(\.\d+)?)C", line)
    if m_soc1_temp:
        aggregator.add_soc1_temp(float(m_soc1_temp.group(1)))

    m_soc2_temp = re.search(r"soc2@(\d+(\.\d+)?)C", line)
    if m_soc2_temp:
        aggregator.add_soc2_temp(float(m_soc2_temp.group(1)))

    m_tj_temp = re.search(r"tj@(\d+(\.\d+)?)C", line)
    if m_tj_temp:
        aggregator.add_tj_temp(float(m_tj_temp.group(1)))

    # 6) Rail Power
    m_vdd_in = re.search(r"VDD_IN\s+(\d+)mW/", line)
    if m_vdd_in:
        aggregator.add_vdd_in(float(m_vdd_in.group(1)) / 1000.0)

    m_vdd_cpu_gpu_cv = re.search(r"VDD_CPU_GPU_CV\s+(\d+)mW/", line)
    if m_vdd_cpu_gpu_cv:
        aggregator.add_vdd_cpu_gpu_cv(float(m_vdd_cpu_gpu_cv.group(1)) / 1000.0)

    m_vdd_soc = re.search(r"VDD_SOC\s+(\d+)mW/", line)
    if m_vdd_soc:
        aggregator.add_vdd_soc(float(m_vdd_soc.group(1)) / 1000.0)


########################################
# 4) 主程式
########################################
def main():
    start_http_server(8000)
    print("Tegrastats Exporter (with aggregator) is running on port 8000...")

    aggregator = MetricsAggregator(interval=5.0)

    # tegrastats 每 1 秒輸出一行
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