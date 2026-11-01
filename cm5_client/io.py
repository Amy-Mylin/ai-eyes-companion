"""有线局域网传输、可立即中断的播放和按需相机。"""

import base64
import io
import ipaddress
import json
import os
import queue
import subprocess
import threading
import time
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener
import wave


class Api:
    def __init__(self, base_url, token):
        parsed = urlparse(base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError("请填写电脑有线网卡的实际IPv4或IPv6地址") from error
        if (parsed.scheme != "http" or parsed.username or parsed.password or
                not (address.is_private or address.is_loopback) or parsed.path not in ("", "/")):
            raise ValueError("设备接口只允许受控的本地HTTP地址")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, path, data=None, query=None, raw=False, timeout=5):
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        request = Request(url, data=None if data is None else json.dumps(data).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        })
        # 不走系统代理；每次请求独立连接，长轮询不会阻塞停止请求。
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise RuntimeError("设备响应超出限制")
            return body if raw else json.loads(body)


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


class Player:
    def __init__(self, api, session_id, device, report, error, *, output_error=None):
        self.api, self.session_id, self.device = api, session_id, device
        self.report, self.error = report, error
        self.output_error = output_error or error
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=12)
        # 最多缓存两段待播声音，下载与当前播放可以并行。
        self.ready = queue.Queue(maxsize=2)
        self.pending_count = 0
        self.closed = threading.Event()
        self.revision = 0
        self.process = None
        self.playing = threading.Event()
        self.speech_playing = threading.Event()
        self.loading = threading.Event()
        self.fetcher = threading.Thread(target=self._fetch, daemon=True)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.fetcher.start()
        self.thread.start()

    def put(self, event):
        with self.lock:
            if self.closed.is_set():
                return
            self.queue.put_nowait((self.revision, dict(event)))
            self.pending_count += 1

    def pending(self):
        with self.lock:
            return self.pending_count > 0

    def put_local(self, name, wav, speech=True):
        with wave.open(io.BytesIO(wav), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if not 0 < duration <= 5:
                raise ValueError("本地提示长度异常")
        with self.lock:
            if self.closed.is_set():
                return False
            event = {"clip_id": "local-" + name, "local_cue": True, "speech": speech}
            try:
                # 本地提示直接进入播放器，不排在可能阻塞的网络下载后面。
                self.ready.put_nowait((self.revision, event, wav, duration))
            except queue.Full:
                return False
            self.pending_count += 1
            return True

    def wait_idle(self, timeout):
        deadline = time.monotonic() + timeout
        while self.pending() and not self.closed.is_set() and time.monotonic() < deadline:
            self.closed.wait(0.02)
        return not self.pending()

    def _finished(self, revision):
        with self.lock:
            if revision == self.revision:
                self.pending_count -= 1

    def stop(self):
        with self.lock:
            self.revision += 1
            self.pending_count = 0
            process = self.process
            for pending in (self.queue, self.ready):
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
        # 先终止本地播放器，再处理网络，断网也可以停声。
        stop_process(process)

    def _fetch(self):
        while not self.closed.is_set():
            try:
                revision, event = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self.loading.set()
            queued = False
            try:
                with self.lock:
                    if revision != self.revision or self.closed.is_set():
                        continue
                wav = self.api.request("/audio", query={"session_id": self.session_id,
                                                        "clip_id": event["clip_id"]}, raw=True)
                with wave.open(io.BytesIO(wav), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if not 0 < duration <= 30:
                        raise ValueError("单段语音长度异常")
                while not self.closed.is_set():
                    with self.lock:
                        if revision != self.revision:
                            break
                        try:
                            self.ready.put_nowait((revision, event, wav, duration))
                            queued = True
                            break
                        except queue.Full:
                            pass
                    self.closed.wait(0.05)
            except Exception as error:
                with self.lock:
                    stale = revision != self.revision
                if not stale and not self.closed.is_set():
                    self.error(f"语音下载没有完成：{type(error).__name__}")
            finally:
                if not queued:
                    self._finished(revision)
                self.loading.clear()

    def _run(self):
        while not self.closed.is_set():
            try:
                revision, event, wav, duration = self.ready.get(timeout=0.2)
            except queue.Empty:
                continue
            clip_id = event["clip_id"]
            local = event.get("local_cue", False)
            process = None
            started = False
            try:
                with self.lock:
                    if revision != self.revision or self.closed.is_set():
                        continue
                    process = subprocess.Popen(["aplay", "-q", "-D", self.device, "-t", "wav"],
                                               stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    self.process = process
                    self.playing.set()
                    if event.get("speech", True):
                        self.speech_playing.set()
                started = True
                if not local:
                    self.report(clip_id, "started")
                process.communicate(input=wav, timeout=duration + 5)
                with self.lock:
                    stale = revision != self.revision
                state = "interrupted" if stale else ("completed" if process.returncode == 0 else "failed")
                if not local:
                    self.report(clip_id, state)
                if state == "failed":
                    self.output_error("扬声器播放进程失败")
            except Exception as error:
                with self.lock:
                    stale = revision != self.revision
                if started and not local:
                    self.report(clip_id, "interrupted" if stale else "failed")
                if not stale and not self.closed.is_set():
                    self.output_error(f"播放没有完成：{type(error).__name__}")
            finally:
                stop_process(process)
                with self.lock:
                    if self.process is process:
                        self.process = None
                        self.playing.clear()
                        self.speech_playing.clear()
                self._finished(revision)

    def close(self):
        self.closed.set()
        self.stop()
        deadline = time.monotonic() + 6
        for worker in (self.fetcher, self.thread):
            worker.join(timeout=max(0, deadline - time.monotonic()))


class JpegStream:
    """拆分管道中的完整JPEG，不积压旧帧。"""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        if len(self.buffer) > 4_000_000:
            self.buffer.clear()
            raise ValueError("相机单帧超出限制")
        frames = []
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                return frames
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                return frames
            frames.append(bytes(self.buffer[:end + 2]))
            del self.buffer[:end + 2]


class Camera:
    PROFILES = {"general": (960, 720, 5), "text": (1920, 1440, 3)}

    def __init__(self, runtime):
        self.runtime = runtime
        self.condition = threading.Condition()
        self.process = None
        self.thread = None
        self.latest = None
        self.failure = None
        self.generation = 0
        self.lifecycle = threading.RLock()
        self.profile = None
        self.frame_count = 0

    def start(self, profile="general"):
        if profile not in self.PROFILES:
            raise ValueError("未知相机取景模式")
        with self.lifecycle:
            with self.condition:
                if (self.process is not None and self.process.poll() is None
                        and self.profile == profile and self.failure is None):
                    return
            # 只在识物／读字模式切换时重启，相同模式的连续追问复用画面流。
            self.stop()
            with self.condition:
                self.latest = None
                self.failure = None
                self.frame_count = 0
                self.generation += 1
                generation = self.generation
                width, height, fps = self.PROFILES[profile]
                self.runtime.mkdir(parents=True, exist_ok=True)
                with (self.runtime / "camera.log").open("ab") as log:
                    process = subprocess.Popen([
                        "rpicam-vid", "--nopreview", "--timeout", "0", "--codec", "mjpeg",
                        "--width", str(width), "--height", str(height), "--framerate", str(fps),
                        "--autofocus-mode", "continuous", "--output", "-",
                    ], stdout=subprocess.PIPE, stderr=log, bufsize=0)
                self.process = process
                self.profile = profile
                self.thread = threading.Thread(target=self._read, args=(process, generation), daemon=True)
                self.thread.start()

    def _read(self, process, generation):
        parser = JpegStream()
        try:
            while chunk := os.read(process.stdout.fileno(), 65536):
                for frame in parser.feed(chunk):
                    with self.condition:
                        if generation != self.generation:
                            return
                        # 这是管道收到完整帧的时刻，尚不等于传感器曝光时刻。
                        self.latest = (time.monotonic(), frame)
                        self.frame_count += 1
                        self.condition.notify_all()
            raise RuntimeError("相机画面流已关闭")
        except Exception as error:
            with self.condition:
                if generation == self.generation:
                    self.failure = str(error)
                    self.condition.notify_all()
        finally:
            process.stdout.close()

    def fresh(self, cancelled, profile="general"):
        if cancelled():
            return None
        requested = time.monotonic()
        with self.lifecycle:
            # 等待启动锁期间可能已结束会话，不能在停止之后重新打开相机。
            if cancelled():
                return None
            self.start(profile)
            with self.condition:
                generation = self.generation
        with self.condition:
            while not cancelled():
                # 只接受本次绑定的画面流，停止或切换模式会立即作废等待。
                if generation != self.generation or self.profile != profile:
                    return None
                if self.failure:
                    raise RuntimeError(self.failure)
                # 文字取景至少等两帧，让自动对焦有机会更新；仍不保证文字已清楚。
                settled = profile != "text" or self.frame_count >= 2
                if self.latest and self.latest[0] >= requested and settled:
                    received, frame = self.latest
                    return {"data": base64.b64encode(frame).decode("ascii"),
                            "age_ms": round((time.monotonic() - received) * 1000, 3)}
                if time.monotonic() - requested > 4.0:
                    raise TimeoutError("相机未及时给出新画面")
                self.condition.wait(0.05)
        return None

    def stop(self):
        with self.lifecycle:
            with self.condition:
                self.generation += 1
                process, self.process = self.process, None
                self.latest = None
                self.profile = None
                self.condition.notify_all()
            stop_process(process)
