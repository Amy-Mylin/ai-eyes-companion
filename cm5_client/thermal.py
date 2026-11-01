"""本次采播进程的温度保护；75℃是软件保护设置，不代表贴肤温度或硬件规格。"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import threading


class ThermalGuard:
    TEMPERATURE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
    SHUTDOWN_COMMAND = ("sudo", "-n", "/usr/sbin/shutdown", "-h", "now")
    CLOSE_WAIT_SECONDS = 0.5
    TRIGGERED_WAIT_SECONDS = 7.0

    def __init__(self, runtime, on_hot, shutdown_command=None, interval=2, threshold_c=75,
                 allow_shutdown=False):
        self.runtime = Path(runtime)
        self.on_hot = on_hot
        self.allow_shutdown = allow_shutdown
        self.interval = float(interval)
        self.threshold_c = float(threshold_c)
        if not callable(on_hot):
            raise TypeError("温度保护需要一个停止本项目采播的回调")
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("温度检查间隔必须是有限正数")
        if not math.isfinite(self.threshold_c) or self.threshold_c <= 0:
            raise ValueError("温度保护阈值必须是有限正数")
        command = self.SHUTDOWN_COMMAND if shutdown_command is None else shutdown_command
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("关机命令必须使用独立参数列表")
        self.shutdown_command = tuple(command)
        if any(not isinstance(part, str) or not part for part in self.shutdown_command):
            raise ValueError("关机命令包含无效参数")
        self.log_path = self.runtime / "thermal-guard.jsonl"
        self.last_status = None
        self.last_temperature = None
        self.log_failed = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._triggered = False

    def start(self):
        """每个实例只启动一次；关闭后的实例不能重新监测或发起关机。"""
        with self._lock:
            if self._closed or self._stop.is_set() or self._thread is not None:
                return False
            # 启动前暴露不可写的记录目录，不能假称保护已经正常运行。
            self.runtime.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8"):
                pass
            self._thread = threading.Thread(target=self._run, name="cm5-thermal-guard", daemon=True)
            self._thread.start()
            return True

    def close(self):
        """正常退出撤销监测；已触发的过热处理留出一次停采播和关机请求时间。"""
        with self._lock:
            self._closed = True
            thread = self._thread
            triggered = self._triggered
            if not triggered:
                self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.TRIGGERED_WAIT_SECONDS if triggered else self.CLOSE_WAIT_SECONDS)

    def _read_temperature(self):
        temperature = float(self.TEMPERATURE_PATH.read_text(encoding="ascii").strip()) / 1000
        if not math.isfinite(temperature):
            raise ValueError("温度读数不是有限数值")
        return temperature

    def _record(self, status, temperature=None):
        with self._lock:
            self.last_status = status
            if temperature is not None:
                self.last_temperature = temperature
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "temperature_c": None if temperature is None else round(temperature, 3),
            "status": status,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # 记录失败不能阻止停采播；不把异常正文或系统输出写入日志。
            self.log_failed = True

    def _run(self):
        while not self._stop.is_set():
            try:
                temperature = self._read_temperature()
            except (OSError, ValueError, UnicodeError):
                self._trip(None, shutdown=False)
                return
            if self._stop.is_set():
                return
            if temperature >= self.threshold_c:
                self._trip(temperature, shutdown=True)
                return
            self._record("temperature_ok", temperature)
            if self._stop.wait(self.interval):
                return

    def _trip(self, temperature, *, shutdown):
        with self._lock:
            if self._closed or self._stop.is_set() or self._triggered:
                return
            self._triggered = True
        self._record("threshold_reached" if shutdown else "temperature_read_failed", temperature)
        if self._stop.is_set():
            return
        try:
            # 先停止本项目采音、相机与播放，再考虑权限可能不足的系统关机。
            self.on_hot()
            self._record("capture_stop_requested", temperature)
        except Exception:
            self._record("capture_stop_failed", temperature)
        if not shutdown or self._stop.is_set():
            return
        if not self.allow_shutdown:
            self._record("shutdown_disabled", temperature)
            return
        self._record("shutdown_request_started", temperature)
        if self._stop.is_set():
            return
        try:
            result = subprocess.run(
                self.shutdown_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            status = "shutdown_requested" if result.returncode == 0 else "shutdown_not_authorized_or_failed"
        except (OSError, subprocess.TimeoutExpired):
            status = "shutdown_not_authorized_or_failed"
        # 命令被接受也不等于已经关机；任何情况均不记录密码、输出或对话内容。
        self._record(status, temperature)
