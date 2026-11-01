"""可选的 Picamera2 采图适配；相机库由系统 Python 子进程加载。"""

import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from .io import stop_process


class PicameraCamera:
    PROFILES = {"general": (960, 720), "text": (1920, 1440)}
    WAIT_SECONDS = 4.0
    MAX_REPLY = 4_300_000

    def __init__(self, runtime, worker_python="/usr/bin/python3"):
        self.runtime = Path(runtime)
        self.worker_python = str(worker_python)
        self.condition = threading.Condition()
        self.lifecycle = threading.RLock()
        self.requests = threading.Lock()
        self.process = None
        self.thread = None
        self.generation = 0
        self.failure = None
        self.pending = None
        self.reply = None

    def _start_locked(self):
        with self.condition:
            if self.process is not None and self.process.poll() is None and self.failure is None:
                return self.generation
        self._stop_locked()
        self.runtime.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        # 语音虚拟环境的路径不能污染系统 Python 的 libcamera 依赖。
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env["PYTHONPYCACHEPREFIX"] = str(self.runtime / "picamera-pycache")
        worker = Path(__file__).with_name("picamera_worker.py")
        with (self.runtime / "picamera.log").open("ab") as log:
            process = subprocess.Popen(
                [self.worker_python, "-u", str(worker), "--worker"],
                # JSON 中包含 JPEG；缓冲按行读取，避免逐字节读取大帧。
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=65536, env=env,
            )
        with self.condition:
            self.process = process
            self.failure = None
            generation = self.generation
            self.thread = threading.Thread(target=self._read, args=(process, generation), daemon=True)
            self.thread.start()
        return generation

    def _read(self, process, generation):
        try:
            while True:
                line = process.stdout.readline(self.MAX_REPLY + 1)
                received = time.monotonic()
                if not line:
                    raise RuntimeError("Picamera2 取景进程已退出")
                if len(line) > self.MAX_REPLY or not line.endswith(b"\n"):
                    raise ValueError("Picamera2 响应超出限制或没有完整结束")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Picamera2 响应格式错误")
                with self.condition:
                    if generation != self.generation:
                        return
                    if event.get("kind") == "fatal":
                        raise RuntimeError(str(event.get("error", "相机初始化失败"))[:500])
                    if self.pending is not None and event.get("id") == self.pending:
                        self.reply = (received, event)
                        self.condition.notify_all()
        except Exception as error:
            with self.condition:
                if generation == self.generation:
                    # 不输出原始协议行，避免把图像 base64 写进日志。
                    self.failure = f"{type(error).__name__}: {error}"
                    self.condition.notify_all()
        finally:
            process.stdout.close()

    def _stop_locked(self):
        with self.condition:
            self.generation += 1
            process, self.process = self.process, None
            reader, self.thread = self.thread, None
            self.pending = self.reply = self.failure = None
            self.condition.notify_all()
        try:
            stop_process(process)
        finally:
            if process is not None and process.stdin is not None:
                process.stdin.close()
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=0.2)

    def stop(self):
        with self.lifecycle:
            self._stop_locked()

    def _stop_generation(self, generation):
        with self.lifecycle:
            if generation == self.generation:
                self._stop_locked()

    def fresh(self, cancelled, profile="general"):
        if profile not in self.PROFILES:
            raise ValueError("未知相机取景模式")
        deadline = time.monotonic() + self.WAIT_SECONDS
        while not self.requests.acquire(timeout=0.02):
            if cancelled():
                return None
            if time.monotonic() >= deadline:
                raise TimeoutError("前一轮相机请求尚未退出")
        generation = None
        request_id = uuid4().hex
        try:
            with self.lifecycle:
                if cancelled():
                    return None
                if time.monotonic() >= deadline:
                    raise TimeoutError("本轮取景等待已过期")
                generation = self._start_locked()
                if cancelled():
                    self._stop_generation(generation)
                    return None
                with self.condition:
                    self.pending, self.reply = request_id, None
                command = {"id": request_id, "profile": profile, "deadline": deadline}
                self.process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
                self.process.stdin.flush()
            with self.condition:
                while True:
                    if generation != self.generation or cancelled():
                        result = None
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Picamera2 未在本轮4秒预算内给出新画面")
                    if self.reply is not None:
                        result = self._frame(self.reply, profile)
                        break
                    if self.failure:
                        raise RuntimeError(self.failure)
                    self.condition.wait(min(0.02, max(0, deadline - time.monotonic())))
            if result is None:
                self._stop_generation(generation)
            return result
        except BaseException:
            if generation is not None:
                self._stop_generation(generation)
            raise
        finally:
            with self.condition:
                if self.pending == request_id:
                    self.pending = self.reply = None
            self.requests.release()

    def _frame(self, reply, profile):
        received, event = reply
        if event.get("kind") == "error":
            raise RuntimeError(str(event.get("error", "本轮相机取景失败"))[:500])
        if event.get("kind") != "frame" or event.get("profile") != profile:
            raise ValueError("相机返回的请求模式不一致")
        if (event.get("width"), event.get("height")) != self.PROFILES[profile]:
            raise ValueError("相机返回的图片尺寸与请求不一致")
        data = event.get("data")
        if not isinstance(data, str) or len(data) > 4_200_000:
            raise ValueError("相机图片超出限制")
        binary = base64.b64decode(data, validate=True)
        if len(binary) > 3_000_000 or not binary.startswith(b"\xff\xd8") or not binary.endswith(b"\xff\xd9"):
            raise ValueError("相机没有返回有效范围内的 JPEG")
        return {
            "data": data,
            # 保持原接口语义：完整图片到达采图适配后的年龄，不冒充曝光年龄。
            "age_ms": round((time.monotonic() - received) * 1000, 3),
            "backend": "picamera2", "profile": profile,
            "metadata": event.get("metadata", {}),
        }
