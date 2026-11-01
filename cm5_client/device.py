"""设备控制和事件消费；控制请求与采音相互独立。"""

import hashlib
import queue
import threading
import time
from uuid import uuid4

from .io import Camera, Player


class Device:
    def __init__(self, api, runtime, playback_device, show_text=False, cues=None, *, camera_factory=None):
        self.api = api
        self.show_text = show_text
        self.cues = cues or {}
        self.closed = threading.Event()
        self.cleanup_lock = threading.Lock()
        self.cleaned_up = False
        self.lock = threading.RLock()
        self.network_control = threading.Lock()
        self.actions = queue.Queue(maxsize=32)
        self.local_revision = 0
        self.blocked = False
        self.awaiting_answer = False
        self.generation_finished = True
        self.wake_requested = threading.Event()
        self.standby_requested = threading.Event()
        self.failure = None
        self.audible_failure = False
        self.local_stop_epoch = None
        connected = api.request("/connect", {"client_id": "cm5-companion", "mode": "device"})
        if not connected["ready"]:
            raise RuntimeError("电脑模型尚未就绪")
        self.session_id = connected["session_id"]
        self.epoch = connected["epoch"]
        self.cursor = connected["cursor"]
        self.camera = (camera_factory or Camera)(runtime)
        self.player = Player(api, self.session_id, playback_device, self.playback, self.fail,
                             output_error=lambda message: self.fail(message, audible=False))
        self.threads = [threading.Thread(target=target, daemon=True)
                        for target in (self._actions, self._events)]
        for thread in self.threads:
            thread.start()

    def fail(self, message, audible=True):
        if not self.closed.is_set():
            self.failure = message
            self.audible_failure = audible
            print(message, flush=True)
            self.closed.set()
            self.player.stop()
            self.camera.stop()

    def notice(self, name):
        if name not in self.cues:
            return False
        # 提示不作为模型回答，也不会伪造服务端的回答播放回执。
        return self.player.put_local(name, self.cues[name], speech=name != "wake")

    def _queue(self, kind, payload, revision=None):
        if self.closed.is_set():
            return
        try:
            self.actions.put_nowait((kind, payload, revision))
        except queue.Full:
            self.fail("设备控制队列积压，已停止播放，请重新启动入口")

    def playback(self, clip_id, state):
        if self.show_text:
            print(f"播放进程回执：{clip_id[:8]} {state}（不等同于实听确认）", flush=True)
        self._queue("/playback", {"clip_id": clip_id, "state": state})

    def report_capture(self):
        # 此时只确认采集实际收到数据；播放尚未完成，不能冒充已验证输出。
        self._queue("/device-status", {"capture_ready": True, "playback_ready": False})

    def interrupt(self, end=False):
        with self.lock:
            if self.closed.is_set():
                return
            self.local_revision += 1
            revision = self.local_revision
            self.blocked = True
            self.awaiting_answer = False
        self.player.stop()
        if end:
            self.camera.stop()
            self.standby_requested.set()
        self._queue("/interrupt", {"end": end}, revision)

    def submit(self, text):
        with self.lock:
            if self.closed.is_set():
                return
            self.local_revision += 1
            revision = self.local_revision
            self.blocked = True
            self.awaiting_answer = True
            self.generation_finished = False
        self.player.stop()
        self._queue("/turn", {"text": text, "request_id": uuid4().hex}, revision)

    def _actions(self):
        while not self.closed.is_set():
            try:
                path, payload, revision = self.actions.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.network_control:
                    with self.lock:
                        if self.closed.is_set() or (revision is not None and revision != self.local_revision):
                            continue
                    result = self.api.request(path, {"session_id": self.session_id, **payload})
                    if revision is not None:
                        with self.lock:
                            self.epoch = max(self.epoch, result.get("epoch", self.epoch))
                            if revision == self.local_revision and not self.closed.is_set():
                                self.local_stop_epoch = result.get("epoch")
                                self.blocked = False
            except Exception as error:
                self.fail(f"电脑控制连接失败：{type(error).__name__}；本地已经停声")

    def _frame(self, request, epoch, revision, profile="general"):
        def cancelled():
            return self.closed.is_set() or self.epoch != epoch or self.local_revision != revision
        try:
            started = time.monotonic()
            with self.lock:
                if cancelled():
                    return
            frame = self.camera.fresh(cancelled, profile=profile)
            if frame and not cancelled():
                self.api.request("/frame", {"session_id": self.session_id,
                                           "request_id": request, **frame})
                if self.show_text:
                    # 摘要用于关联本轮取景，不保存画面，也不能单凭摘要不同判断物品已变化。
                    digest = hashlib.sha256(frame["data"].encode("ascii")).hexdigest()[:12]
                    elapsed = round((time.monotonic() - started) * 1000)
                    print(f"取景已提交：请求{request[:8]}，图像摘要{digest}，耗时{elapsed}ms，"
                          f"接收年龄{frame['age_ms']}ms，模式{profile}", flush=True)
        except Exception as error:
            if not cancelled():
                print(f"这次取景没有完成：{type(error).__name__}；电脑会明确提示缺图", flush=True)

    def _events(self):
        while not self.closed.is_set():
            try:
                batch = self.api.request("/events", query={"session_id": self.session_id,
                                                          "after": self.cursor}, timeout=19)
                if batch["reset_required"]:
                    raise RuntimeError("事件已丢失，不能继续播放旧队列，请重新启动")
                # 等待当前控制请求返回，避免新回答先到达而被当成旧回答丢弃。
                with self.network_control:
                    with self.lock:
                        if self.closed.is_set():
                            return
                        self.epoch = max(self.epoch, batch["epoch"])
                        for event in batch["events"]:
                            if event.get("epoch", self.epoch) != self.epoch:
                                continue
                            kind = event["kind"]
                            if kind == "stop":
                                if event.get("epoch") != self.local_stop_epoch:
                                    self.player.stop()
                            elif kind == "standby":
                                self.awaiting_answer = False
                                self.generation_finished = True
                                self.standby_requested.set()
                                self.camera.stop()
                            elif self.blocked:
                                continue
                            elif kind == "audio":
                                self.player.put(event)
                            elif kind == "frame_requested":
                                threading.Thread(target=self._frame, args=(event["request_id"],
                                    self.epoch, self.local_revision, event.get("profile", "general")),
                                    daemon=True).start()
                            elif kind == "turn":
                                if self.show_text:
                                    print("本轮模式：" + ("视觉" if event["visual"] else "普通交流"), flush=True)
                                if not event["visual"]:
                                    self.camera.stop()
                            elif kind == "text" and event["final"] and self.show_text:
                                print("助手：" + event["text"], flush=True)
                            elif kind == "generation_finished":
                                self.generation_finished = True
                            elif kind == "error":
                                self.awaiting_answer = False
                                self.player.stop()
                                self.notice("retry")
                                print("当前回答失败：" + event["detail"], flush=True)
                        self.cursor = batch["cursor"]
            except Exception as error:
                self.fail(f"设备事件连接已停止：{type(error).__name__}")

    def busy(self):
        with self.lock:
            # 包含下载、预取和播放，避免队列交接时误判回答已结束。
            pending = self.player.pending()
            if self.generation_finished and not pending:
                self.awaiting_answer = False
            return self.awaiting_answer or pending

    def close(self):
        with self.cleanup_lock:
            if self.cleaned_up:
                return
            with self.lock:
                self.closed.set()
                self.local_revision += 1
                self.blocked = True
                self.awaiting_answer = False
            self.player.stop()
            self.camera.stop()
            # 声卡本身失败时不循环重试；其他故障可用本地提示说明交流已停止。
            fault_cue = bool(self.failure and self.audible_failure and self.notice("failure"))
            # 不等待旧请求：服务端撤销会话后，晚到的问题也不能重新启动回答。
            # 断网时仍先停本地声音；无法收到确认时，不宣称电脑已经停止。
            try:
                self.api.request("/disconnect", {"session_id": self.session_id}, timeout=2)
            except Exception as error:
                print(f"本地已停止；未确认电脑端连接撤销：{type(error).__name__}", flush=True)
            if fault_cue:
                self.player.wait_idle(timeout=2.5)
            self.player.close()
            deadline = time.monotonic() + 1.0
            for thread in self.threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            self.cleaned_up = True
