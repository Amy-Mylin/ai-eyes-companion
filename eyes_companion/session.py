"""管理一个设备会话；文字生成、实际播放和长期记忆各自保留边界。"""

import base64
from collections import OrderedDict
from datetime import datetime, timezone
import queue
import re
import threading
import time
import unicodedata
from uuid import uuid4

from .context import SYSTEM, build_messages, text_frame_needed
from .events import EventLog
from .images import jpeg_size
from .intents import camera_disabled, memory_change, needs_vision
from .providers import Cancelled, Turn


def stamp():
    return datetime.now(timezone.utc).isoformat()


def compact_command(text):
    return re.sub(r"[\s，。！？、,.!?]", "", text)


def split_speech(buffer, final=False, limit=60):
    segments = []
    while buffer:
        match = re.search(r"[。！？!?；;\n]", buffer)
        if match and match.end() <= limit:
            end = match.end()
        elif len(buffer) >= limit:
            end = max(buffer.rfind("，", 8, limit), buffer.rfind(",", 8, limit)) + 1
            end = end if end > 0 else limit
        elif final:
            end = len(buffer)
        else:
            break
        part, buffer = buffer[:end].strip(), buffer[end:]
        # 独立的右括号等标点会被系统语音合成为零时长WAV；完整文字仍保留在回答中。
        if part and any(unicodedata.category(char)[0] not in "PZC" for char in part):
            segments.append(part)
    return segments, buffer


class Session:
    def __init__(self, model, tts, memory, *, display_allowed=False, capture=None):
        self.model, self.tts, self.memory = model, tts, memory
        self.capture = capture
        self.display_allowed = display_allowed
        self.events = EventLog()
        self.lock = threading.RLock()
        self.frame_condition = threading.Condition(self.lock)
        self.id = uuid4().hex
        self.epoch = 0
        self.current = None
        self.client_id = None
        self.client_mode = "device"
        self.client_seen = 0.0
        self.history = []
        self.clips = OrderedDict()
        self.requests = OrderedDict()
        self.frame_request = None
        self.frame = None
        self.visual = False
        self.last_memory = None
        self.ready = False
        self.failure = None
        self.stopped = threading.Event()
        self.workers = []
        self.observer = None

    def emit(self, kind, **data):
        event = self.events.publish(kind, session_id=self.id, epoch=self.epoch, **data)
        if self.observer:
            self.observer.put(event)
        return event

    def report_runtime(self, status=None):
        status = status or ("ready" if self.ready else "error" if self.failure else "loading")
        self.emit("runtime", ready=self.ready, failure=self.failure, models=[
            {"name": getattr(self.model, "model", type(self.model).__name__),
             "provider": type(self.model).__name__, "location": "compute",
             "purpose": "对话与按需视觉理解", "status": status},
            {"name": getattr(self.tts, "voice", None) or type(self.tts).__name__,
             "provider": type(self.tts).__name__, "location": "compute",
             "purpose": "语音合成（本段生成耗时）", "status": status},
        ])

    def warm(self):
        try:
            self.emit("status", state="preparing", detail="正在准备本地模型和语音")
            self.report_runtime()
            self.model.warm()
            self.tts.warm()
            with self.lock:
                self.ready = True
                self.emit("status", state="ready", detail="本地模型与语音就绪，等待真实设备")
                self.report_runtime()
        except Exception as error:
            with self.lock:
                self.failure = str(error)
                self.emit("status", state="error", detail=self.failure)
                self.report_runtime()

    def connect(self, client_id, mode="device"):
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client_id):
            raise ValueError("设备标识格式错误")
        if mode not in {"device", "test"}:
            raise ValueError("接入模式必须是device或test")
        with self.lock:
            if self.client_id and self.client_id != client_id and time.monotonic() - self.client_seen < 15:
                raise RuntimeError("另一个设备正在使用此入口")
            self._cancel("设备重新连接")
            self.client_id = client_id
            self.client_mode = mode
            self.client_seen = time.monotonic()
            self.id = uuid4().hex
            self.history.clear()
            self.requests.clear()
            self.visual = False
            self.events.clear()
            self.emit("connected", client_id=client_id, ready=self.ready, mode=mode)
            self.report_runtime()
            return {"session_id": self.id, "epoch": self.epoch,
                    "cursor": self.events.sequence, "ready": self.ready}

    def require(self, session_id):
        if not self.client_id or session_id != self.id:
            raise ValueError("设备会话失效，请重新连接")

    def health(self):
        with self.lock:
            return {"ready": self.ready, "error": self.failure,
                    "model": self.model.model,
                    "tts_backend": type(self.tts).__name__,
                    "tts_voice": getattr(self.tts, "voice", None),
                    "client_mode": self.client_mode,
                    "device_connected": bool(self.client_id and time.monotonic() - self.client_seen < 15),
                    "visual": self.visual, "epoch": self.epoch,
                    "boundary": "设备在线仅代表通信；实际采播以设备回执为准"}

    def poll(self, session_id, after, timeout=15):
        with self.lock:
            self.require(session_id)
            self.client_seen = time.monotonic()
            if self.observer:
                # 只通知观察端；写回设备事件队列会让长轮询被自己的心跳不断唤醒。
                self.observer.put({"kind": "device_link"})
        result = self.events.read(after, timeout)
        with self.lock:
            self.require(session_id)
            self.client_seen = time.monotonic()
            result["epoch"] = self.epoch
        return result

    def _cancel(self, reason):
        if self.current:
            self.current.cancel()
        self.current = None
        self.epoch += 1
        self.clips.clear()
        self.frame_request = self.frame = None
        self.frame_condition.notify_all()
        self.emit("stop", reason=reason)

    def interrupt(self, session_id, end=False):
        with self.lock:
            self.require(session_id)
            self._cancel("结束聊天" if end else "用户插话或停止播报")
            if end:
                self.history.clear()
                self.visual = False
                self.events.clear()
                self.emit("stop", reason="结束会话并清除临时内容")
                self.emit("standby")
            return {"epoch": self.epoch}

    def disconnect(self, session_id, reason="设备退出"):
        with self.lock:
            self.require(session_id)
            # 待机保留连接供再次唤醒；客户端退出则撤销连接，拒绝在途旧问题。
            self._cancel(reason)
            self.history.clear()
            self.requests.clear()
            self.visual = False
            self.client_id = None
            self.client_seen = 0.0
            self.events.clear()
            self.emit("stop", reason=reason + "，连接已撤销")
            self.emit("disconnected", reason=reason)
            return {"epoch": self.epoch, "disconnected": True}

    def current_turn(self, turn):
        turn.check()
        if self.current is not turn or turn.epoch != self.epoch or turn.revision != self.memory.revision():
            raise Cancelled()

    def mutate_memory(self, action, *, content=None, memory_id=None, context="用户明确要求记住"):
        with self.lock:
            # 先撤销引用旧记忆的生成与音频，再提交数据库；失败不伪报成功。
            self._cancel("记忆发生更改")
            self.history.clear()
            self.requests.clear()
            self.events.clear()
            self.emit("history_cleared")
            try:
                if action == "delete":
                    if not self.memory.delete(memory_id):
                        raise KeyError("记忆不存在或已经删除")
                    if self.last_memory == memory_id:
                        self.last_memory = None
                    result = {"deleted": memory_id}
                elif action == "put":
                    memory_id = self.memory.put(content, memory_id=memory_id, context=context)
                    self.last_memory = memory_id
                    result = {"memory_id": memory_id}
                else:
                    raise ValueError("未知记忆操作")
            finally:
                # 即使写入失败，也要让客户端停止已经取走的旧音频。
                self.emit("stop", reason="记忆操作撤销了旧回答")
            self.emit("memory_changed", revision=self.memory.revision())
            return {**result, "revision": self.memory.revision(), "epoch": self.epoch}

    def submit(self, session_id, request_id, text, *, visual=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 800:
            raise ValueError("单次问题请控制在800个字符以内，可以分次继续说")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id):
            raise ValueError("请求标识格式错误")
        if visual is not None and type(visual) is not bool:
            raise ValueError("视觉开关必须为布尔值")
        text = text.strip()
        command = compact_command(text)
        with self.lock:
            self.require(session_id)
            if request_id in self.requests:
                return self.requests[request_id]
            if not self.ready:
                raise RuntimeError(self.failure or "模型尚未准备好")
            if command in {"结束聊天", "退出聊天", "结束对话", "再见小瞳"}:
                return self.interrupt(session_id, end=True)
            if command in {"停止播报", "停止回答", "别说了", "先别说了", "停一下"}:
                return self.interrupt(session_id)

            acknowledgement = None
            change = memory_change(text)
            if change and change[0] == "remember":
                self.mutate_memory("put", content=change[1])
                acknowledgement = "这条信息已经保存。你可以让我修改或删除刚才那条记忆。"
            elif change and change[0] == "correct":
                if self.last_memory:
                    self.mutate_memory("put", content=change[1], memory_id=self.last_memory)
                    acknowledgement = "刚才那条记忆已经修改，旧内容不会再用于后续回答。"
                else:
                    acknowledgement = "当前没有可以明确对应的上一条记忆。请先查看记忆，再指定要修改的条目。"
            elif command in {"忘记刚才那条记忆", "删除刚才那条记忆"}:
                if self.last_memory:
                    self.mutate_memory("delete", memory_id=self.last_memory)
                    acknowledgement = "刚才那条记忆已经删除，并清除了当前对话中的相关旧内容。"
                else:
                    acknowledgement = "当前没有可以明确对应的上一条记忆。请先查看记忆，再指定要删除的条目。"
            elif command in {"查看记忆", "你记住了什么", "看看我的记忆"}:
                rows = self.memory.list()
                acknowledgement = "现在没有保存的记忆。" if not rows else "当前保存的记忆包括：" + "；".join(
                    row["content"] for row in rows[:6]) + ("。其他条目可通过本地记忆导出查看。" if len(rows) > 6 else "。")
            elif command in {"关闭摄像头", "退出看景", "停止看景"}:
                self.visual = False
                acknowledgement = "已经退出视觉交流。"
            elif camera_disabled(text):
                # 混合话语仍交给模型回答；否定摄像头的要求优先于接口传入的视觉标记。
                self.visual = False
            elif visual is not None:
                self.visual = visual
            else:
                self.visual = needs_vision(text, active=self.visual)

            self._cancel("新问题取代旧回答")
            turn = Turn(uuid4().hex, self.epoch, self.memory.revision())
            self.current = turn
            self.emit("turn", turn_id=turn.id, text=text, visual=self.visual,
                      display_allowed=self.display_allowed)
            response = {"turn_id": turn.id, "epoch": self.epoch}
            self.requests[request_id] = response
            while len(self.requests) > 128:
                self.requests.popitem(last=False)
            worker = threading.Thread(target=self._run, args=(turn, text, acknowledgement), daemon=True)
            self.workers = [thread for thread in self.workers if thread.is_alive()]
            self.workers.append(worker)
            worker.start()
            return response

    def provide_frame(self, session_id, frame_request_id, data, *, age_ms):
        if type(age_ms) not in {int, float} or not 0 <= age_ms <= 750:
            raise ValueError("画面过期，请重新取景")
        if not isinstance(data, str) or len(data) > 4_200_000:
            raise ValueError("图片过大")
        binary = base64.b64decode(data, validate=True)
        if len(binary) > 3_000_000:
            raise ValueError("图片过大")
        width, height = jpeg_size(binary)
        with self.lock:
            self.require(session_id)
            if not self.frame_request or self.frame_request[0] != frame_request_id:
                raise ValueError("取景请求已经失效")
            if time.monotonic() - self.frame_request[1] > 5:
                raise ValueError("画面传输超时，请重新取景")
            profile = self.frame_request[2] if len(self.frame_request) > 2 else "general"
            if profile == "text" and (width < 1600 or height < 1000):
                raise ValueError("本轮需要清晰文字画面，设备尚未提交足够分辨率的图片")
            self.frame = {"data": data, "width": width, "height": height,
                          "frame_id": uuid4().hex, "at": stamp(), "age_ms": age_ms,
                          "profile": profile}
            self.frame_condition.notify_all()
            return {"accepted": True}

    def _fresh_frame(self, turn, profile="general"):
        with self.frame_condition:
            self.current_turn(turn)
            request_id = uuid4().hex
            self.frame_request = (request_id, time.monotonic(), profile)
            self.frame = None
            self.emit("frame_requested", turn_id=turn.id, request_id=request_id, profile=profile)
            deadline = time.monotonic() + 5
            while self.frame is None:
                self.current_turn(turn)
                if time.monotonic() >= deadline:
                    return None
                self.frame_condition.wait(0.1)
            frame = self.frame
            self.frame_request = None
            self.emit("frame", turn_id=turn.id, **frame, display_allowed=self.display_allowed)
            return frame

    def _run(self, turn, text, acknowledgement):
        parts = queue.Queue(maxsize=8)
        speech = threading.Thread(target=self._speech, args=(turn, parts), daemon=True)
        speech.start()
        try:
            frame = None
            with self.lock:
                self.current_turn(turn)
                visual = self.visual and acknowledgement is None
            if visual:
                frame = self._fresh_frame(turn, "text" if text_frame_needed(text) else "general")
                if frame is None:
                    acknowledgement = "没有收到新的摄像头画面，暂时不能确认眼前的内容。请检查相机连接后再让我看一次。"
            with self.lock:
                self.current_turn(turn)
                turn.visual = visual
                turn.frame_id = frame["frame_id"] if frame else None
                self.history.append({"role": "user", "content": text,
                                     "turn_id": turn.id, "at": stamp(), "visual": visual,
                                     "frame_id": turn.frame_id})
                self.history = self.history[-12:]
                facts = []
                fact_budget = 500 if frame else 800
                for fact in self.memory.relevant(text):
                    if len(fact["content"]) > fact_budget:
                        continue
                    facts.append({"content": fact["content"], "context": fact["context"],
                                  "updated_at": fact["updated_at"]})
                    fact_budget -= len(fact["content"])
                messages = build_messages(self.history, text, facts, frame)
            if self.capture:
                try:
                    self.capture.begin(turn, text, frame, messages, self.model.model)
                except Exception as error:
                    self.emit("diagnostic_error", detail="视觉输入留存失败：" + type(error).__name__)
            source = iter([acknowledgement]) if acknowledgement else self.model.stream(messages, turn)
            # 由任务提示要求简洁，不能截掉下一句中的不确定性或取景建议。
            buffer = ""
            full_text = ""
            first_text_seconds = None
            segment_count = 0
            for token in source:
                with self.lock:
                    self.current_turn(turn)
                    if first_text_seconds is None and token:
                        first_text_seconds = round(time.monotonic() - turn.started, 3)
                    full_text += token
                    buffer += token
                    self.emit("text", turn_id=turn.id, text=full_text, final=False,
                              display_allowed=self.display_allowed)
                segments, buffer = split_speech(buffer, limit=24 if segment_count == 0 else 60)
                for segment in segments:
                    self._enqueue(parts, (segment, False), turn)
                    segment_count += 1
            segments, _ = split_speech(buffer, final=True)
            for segment in segments:
                self._enqueue(parts, (segment, False), turn)
            if self.capture:
                try:
                    self.capture.finish(turn.id, full_text, metrics={
                        "first_text_seconds_from_submit": first_text_seconds,
                        "generation_seconds_from_submit": round(time.monotonic() - turn.started, 3),
                        "model_backend": turn.model_metrics,
                        "model_duration_unit": "nanoseconds",
                        "boundary": "电脑收到问题后的文字阶段，含取帧；不含ASR或扬声器首声",
                    })
                except Exception as error:
                    self.emit("diagnostic_error", detail="视觉回答留存失败：" + type(error).__name__)
            with self.lock:
                self.current_turn(turn)
                self.emit("text", turn_id=turn.id, text=full_text, final=True,
                          display_allowed=self.display_allowed)
            self._enqueue(parts, (None, True), turn)
            speech.join(timeout=60)
            with self.lock:
                self.current_turn(turn)
                if speech.is_alive():
                    raise RuntimeError("语音生成未按时完成")
                self.emit("generation_finished", turn_id=turn.id)
        except Cancelled:
            return
        except Exception as error:
            with self.lock:
                if self.current is turn and not turn.cancelled.is_set():
                    self._cancel("当前回答失败")
                    self.emit("error", detail=str(error), turn_id=turn.id)
        finally:
            # 如果模型失败，不让语音线程永远等下一段。
            if self.current is not turn:
                turn.cancel()

    @staticmethod
    def _enqueue(parts, item, turn):
        while True:
            turn.check()
            try:
                parts.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _speech(self, turn, parts):
        try:
            while not turn.cancelled.is_set():
                try:
                    text, finished = parts.get(timeout=0.1)
                except queue.Empty:
                    continue
                if finished:
                    return
                turn.check()
                synth_started = time.monotonic()
                wav = self.tts.synthesize(text, turn)
                synth_ms = round((time.monotonic() - synth_started) * 1000, 3)
                with self.lock:
                    self.current_turn(turn)
                    clip_id = uuid4().hex
                    self.clips[clip_id] = {"wav": wav, "text": text, "turn": turn,
                                           "state": "queued", "created": time.monotonic()}
                    if len(self.clips) > 24:
                        raise RuntimeError("设备未及时播放，语音队列已满")
                    self.emit("audio", turn_id=turn.id, clip_id=clip_id, text=text,
                              display_allowed=self.display_allowed, synthesis_ms=synth_ms)
        except Cancelled:
            return
        except Exception as error:
            with self.lock:
                if self.current is turn and not turn.cancelled.is_set():
                    self._cancel("语音合成失败")
                    self.emit("error", detail=str(error), turn_id=turn.id)

    def audio(self, session_id, clip_id):
        with self.lock:
            self.require(session_id)
            clip = self.clips.get(clip_id)
            if clip is None or time.monotonic() - clip["created"] > 60:
                raise KeyError("语音已经失效")
            self.current_turn(clip["turn"])
            return clip["wav"]

    def playback(self, session_id, clip_id, state):
        if state not in {"started", "completed", "interrupted", "failed"}:
            raise ValueError("播放状态无效")
        with self.lock:
            self.require(session_id)
            clip = self.clips.get(clip_id)
            if clip is None:
                return {"accepted": False, "reason": "过期音频回执已丢弃"}
            self.current_turn(clip["turn"])
            previous = clip["state"]
            if previous == state:
                return {"accepted": True}
            if previous in {"completed", "interrupted", "failed"}:
                raise ValueError("播放终态不能回退")
            if state == "completed" and previous != "started":
                raise ValueError("没有开始回执，不能声明已播完")
            clip["state"] = state
            if state == "completed":
                if (self.history and self.history[-1]["role"] == "assistant"
                        and self.history[-1].get("turn_id") == clip["turn"].id):
                    self.history[-1]["content"] += clip["text"]
                else:
                    self.history.append({"role": "assistant", "content": clip["text"],
                                         "turn_id": clip["turn"].id, "at": stamp(),
                                         "visual": getattr(clip["turn"], "visual", False),
                                         "frame_id": getattr(clip["turn"], "frame_id", None)})
            self.emit("playback", clip_id=clip_id, turn_id=clip["turn"].id, state=state)
            return {"accepted": True}

    def close(self):
        with self.lock:
            self.stopped.set()
            self._cancel("服务停止")
            self.ready = False
            self.report_runtime("stopped")
        for worker in self.workers:
            worker.join(timeout=2)
