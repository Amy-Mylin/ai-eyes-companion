"""把真实业务事件异步接入已有大屏；显示故障不阻塞设备交流。"""

from datetime import datetime, timezone
import queue
import threading
import time
from uuid import uuid4

import httpx


class Dashboard:
    def __init__(self, base_url, token_path):
        self.base_url = base_url.rstrip("/")
        self.token_path = token_path
        self.queue = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.source_id = "companion-" + uuid4().hex[:12]
        self.session_id = None
        self.task_id = None
        self.mode = "companion"
        self.sequence = 0
        self.frame_sequence = 0
        self.status = "未连接"
        self.device_seen = 0.0
        self.device_state = "starting"
        self.device_detail = "等待采播端报告"
        self.client_mode = "test"
        self.compute_state = "starting"
        self.compute_detail = "等待模型就绪"
        self.models = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def put(self, event):
        try:
            self.queue.put_nowait(dict(event))
        except queue.Full:
            self.status = "大屏事件积压，显示可能不完整；设备交流继续"

    def _event(self, client, kind, data, source_suffix=""):
        self.sequence += 1
        response = client.post(self.base_url + "/api/events", json={
            "version": 1, "sessionId": self.session_id, "sourceId": self.source_id + source_suffix,
            "seq": self.sequence, "at": datetime.now(timezone.utc).isoformat(),
            "type": kind, "data": data,
        })
        response.raise_for_status()

    def _heartbeats(self, client):
        self._event(client, "heartbeat", {
            "role": "compute", "name": "电脑本地交流服务", "status": self.compute_state,
            "detail": self.compute_detail,
        })
        if self.device_seen:
            if time.monotonic() - self.device_seen >= 20:
                self.device_state = "degraded"
                self.device_detail = "近期未收到设备消息，采播状态未确认"
            self._event(client, "heartbeat", {
                "role": "glasses", "name": "CM5采播端" if self.client_mode == "device" else "采播协议回放（非实机）",
                "status": self.device_state, "detail": self.device_detail,
            }, source_suffix="-device")

    def _metrics(self, client):
        if self.models:
            # 只上报真实模型配置和已测阶段；声学首声与网络类型没有测量就留空。
            self._event(client, "metrics", {"models": self.models})

    def _clear_task(self, client, title, status="paused"):
        self.task_id = uuid4().hex
        self._event(client, "task", {
            "taskId": self.task_id, "mode": "companion", "status": status,
            "executor": "电脑本地交流服务", "title": title,
        })

    def _run(self):
        while not self.closed.is_set():
            try:
                token = self.token_path.read_text(encoding="utf-8").strip()
                with httpx.Client(trust_env=False, timeout=2,
                                  headers={"Authorization": "Bearer " + token}) as client:
                    next_heartbeat = time.monotonic() + 2
                    while not self.closed.is_set():
                        try:
                            event = self.queue.get(timeout=2)
                        except queue.Empty:
                            if self.session_id:
                                self._heartbeats(client)
                            continue
                        if "_flush" in event:
                            event["_flush"].set()
                            continue
                        self._send(client, event)
                        self.status = "已接入" if self.session_id else "等待设备会话"
                        if self.session_id and time.monotonic() >= next_heartbeat:
                            self._heartbeats(client)
                            next_heartbeat = time.monotonic() + 2
            except Exception as error:
                self.status = f"大屏未接入：{type(error).__name__}"
                self.closed.wait(3)

    def _send(self, client, event):
        kind = event["kind"]
        if kind == "runtime":
            self.models = [dict(model) for model in event["models"]]
            self.compute_state = "ready" if event["ready"] else (
                "starting" if all(model["status"] == "loading" for model in self.models) else "degraded")
            self.compute_detail = "本地模型与语音已就绪；未测量实际网络类型" if event["ready"] else "模型正在准备或已停止，查看模型状态"
            if self.session_id:
                self._metrics(client)
                self._heartbeats(client)
        elif kind == "connected":
            # 大屏使用独立观察会话，不把测试窗口伪装成实时硬件。
            self.session_id = "eyes-" + uuid4().hex
            self.task_id = None
            self.client_mode = event.get("mode", "test")
            self.device_seen = time.monotonic()
            self.device_state = "starting"
            self.device_detail = "连接已建立，实际采音与播放尚未确认"
            response = client.post(self.base_url + "/api/session", json={
                "sessionId": self.session_id, "label": "AI眼镜离线交流",
                "mode": "test" if event.get("mode") == "test" else "live",
            })
            response.raise_for_status()
            self._metrics(client)
            self._heartbeats(client)
        elif not self.session_id:
            return
        elif kind == "device_status":
            self.device_seen = time.monotonic()
            if event.get("capture_ready") and event.get("playback_ready"):
                self.device_state = "ready"
                self.device_detail = "采播端报告准备完成；实际出声仍以播放回执和现场听音为准"
            elif not event.get("capture_ready"):
                self.device_state = "degraded"
                self.device_detail = "采播端没有确认采音准备完成"
            elif self.device_state != "ready":
                self.device_state = "starting"
                self.device_detail = "已收到采音数据，尚无实际播放回执"
            self._heartbeats(client)
        elif kind == "device_link":
            self.device_seen = time.monotonic()
            self._heartbeats(client)
        elif kind == "turn":
            self.task_id = event["turn_id"]
            self.mode = "vision" if event["visual"] else "companion"
            for model in self.models:
                model.pop("latencyMs", None)
            self._metrics(client)
            self._event(client, "task", {
                "taskId": self.task_id, "mode": self.mode, "status": "active",
                "executor": "CM5采播＋电脑本地模型" if self.client_mode == "device" else "电脑结果或协议回放（非实机）",
                "title": "视觉交流" if event["visual"] else "陪伴交流",
            })
            self._event(client, "dialogue", {
                "taskId": self.task_id, "messageId": self.task_id + "-user", "speaker": "user",
                "text": event["text"], "status": "final", "displayAllowed": event["display_allowed"],
            })
        elif kind == "text" and event["turn_id"] == self.task_id and event["text"]:
            self._event(client, "dialogue", {
                "taskId": self.task_id, "messageId": self.task_id + "-assistant", "speaker": "assistant",
                "text": event["text"][:6000], "status": "final" if event["final"] else "partial",
                "displayAllowed": event["display_allowed"],
            })
        elif kind == "frame" and event.get("display_allowed") and event["turn_id"] == self.task_id:
            self.frame_sequence += 1
            response = client.post(self.base_url + "/api/frames", json={
                "version": 1, "sessionId": self.session_id, "sourceId": self.source_id,
                "seq": self.frame_sequence, "at": event["at"], "taskId": self.task_id,
                "frameId": event["frame_id"], "width": event["width"], "height": event["height"],
                "mimeType": "image/jpeg", "dataBase64": event["data"], "displayAllowed": True,
            })
            response.raise_for_status()
        elif kind == "audio" and event["turn_id"] == self.task_id:
            self._event(client, "speech", {
                "taskId": self.task_id, "utteranceId": event["clip_id"], "text": event["text"],
                "purpose": self.mode, "displayAllowed": event["display_allowed"],
            })
            if len(self.models) == 2 and "synthesis_ms" in event:
                self.models[1]["latencyMs"] = event["synthesis_ms"]
                self._metrics(client)
        elif kind == "playback" and event["turn_id"] == self.task_id:
            self.device_seen = time.monotonic()
            if event["state"] == "failed":
                # 现有大屏仅允许ready来源提交播放回执，不能为发送失败而伪造ready。
                self.device_state = "degraded"
                self.device_detail = "设备报告播放失败；没有确认该段声音已播完"
                self._heartbeats(client)
                self._clear_task(client, "设备播放失败，旧播报已撤销", "failed")
                return
            self.device_state = "ready"
            self.device_detail = "收到设备播放进程回执；声学效果仍需现场确认"
            self._heartbeats(client)
            self._event(client, "playback", {
                "taskId": self.task_id, "utteranceId": event["clip_id"], "state": event["state"],
                "output": "glasses", "detail": "设备播放进程回执；不是声学测量",
            }, source_suffix="-device")
        elif kind == "stop":
            self._clear_task(client, "已撤销旧回答，停止出声仍需设备确认")
        elif kind == "error":
            self._clear_task(client, "本次回答失败，请重试", "failed")
        elif kind in {"standby", "history_cleared", "memory_changed", "disconnected"}:
            if kind == "disconnected":
                self.device_seen = 0.0
                self._event(client, "heartbeat", {
                    "role": "glasses", "name": "CM5采播端" if self.client_mode == "device" else "采播协议回放（非实机）",
                    "status": "degraded",
                    "detail": event.get("reason", "设备客户端已退出") + "，连接已撤销",
                }, source_suffix="-device")
            # 切换任务清除旧画面、字幕和语音；不把私人记忆写入大屏。
            self._clear_task(client, "待机或上下文已清除")

    def close(self):
        if self.closed.is_set():
            return
        # 尽量发出服务停止状态；大屏不可用时等待有界，不阻塞交流端退出。
        flushed = threading.Event()
        try:
            self.queue.put_nowait({"_flush": flushed})
            flushed.wait(timeout=2)
        except queue.Full:
            pass
        self.closed.set()
        self.thread.join(timeout=3)
