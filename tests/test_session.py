"""取消、记忆、播放与设备接口的回归；替身只在测试中使用。"""

import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eyes_companion.events import EventLog
from eyes_companion.images import jpeg_size
from eyes_companion.memory import MemoryStore
from eyes_companion.providers import Turn
from eyes_companion.server import create_server
from eyes_companion.session import Session


class ModelDouble:
    model = "test-only"

    def __init__(self):
        self.messages = []
        self.block = False
        self.started = threading.Event()

    def stream(self, messages, turn):
        self.messages.append(messages)
        self.started.set()
        while self.block and not turn.cancelled.wait(0.01):
            pass
        # 故意模拟无法立即取消的旧服务，检查会话层是否丢弃迟到内容。
        yield "这是一段仅用于回归检查的回答。"


class SpeechDouble:
    def synthesize(self, text, turn):
        turn.check()
        return b"test-only-not-audio"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.directory.name) / "memory.db")
        self.model = ModelDouble()
        self.session = Session(self.model, SpeechDouble(), self.memory)
        self.session.ready = True
        self.connected = self.session.connect("test-device")
        self.sid = self.connected["session_id"]

    def tearDown(self):
        self.session.close()
        self.memory.close()
        self.directory.cleanup()

    def submit(self, text="你好", request_id="request-1"):
        return self.session.submit(self.sid, request_id, text)

    def wait_event(self, kind, after=0):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            items = self.session.events.read(after, timeout=0)["events"]
            matches = [item for item in items if item["kind"] == kind]
            if matches:
                return matches[-1]
            time.sleep(0.01)
        self.fail(f"没有收到事件：{kind}")

    def test_generation_is_not_heard_until_playback_receipt(self):
        self.submit()
        event = self.wait_event("audio")
        self.assertTrue(all(item["role"] != "assistant" for item in self.session.history))
        with self.assertRaises(ValueError):
            self.session.playback(self.sid, event["clip_id"], "completed")
        self.session.playback(self.sid, event["clip_id"], "started")
        self.session.playback(self.sid, event["clip_id"], "completed")
        self.assertEqual(self.session.history[-1]["content"], event["text"])
        self.session.playback(self.sid, event["clip_id"], "completed")
        self.assertEqual(self.session.history[-1]["content"], event["text"])

    def test_observation_heartbeat_does_not_wake_its_own_poll(self):
        observed = []

        class Observer:
            def put(self, event):
                observed.append(event)

        self.session.observer = Observer()
        cursor = self.session.events.sequence
        started = time.monotonic()
        batch = self.session.poll(self.sid, cursor, timeout=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        self.assertEqual(batch["events"], [])
        self.assertEqual(batch["cursor"], cursor)
        self.assertEqual(observed, [{"kind": "device_link"}])

    def test_interrupt_rejects_late_generation_and_audio(self):
        self.model.block = True
        self.submit()
        self.assertTrue(self.model.started.wait(2))
        self.session.interrupt(self.sid)
        for thread in self.session.workers:
            thread.join(timeout=2)
        events = self.session.events.read(0, 0)["events"]
        self.assertFalse(any(event["kind"] == "audio" for event in events))
        self.assertFalse(any(item["role"] == "assistant" for item in self.session.history))

    def test_delete_cancels_existing_answer_and_scrubs_context(self):
        saved = self.session.mutate_memory("put", content="我的昵称是测试秘密")
        self.submit()
        clip = self.wait_event("audio")["clip_id"]
        self.session.mutate_memory("delete", memory_id=saved["memory_id"])
        self.assertEqual(self.memory.list(), [])
        self.assertEqual(self.session.history, [])
        with self.assertRaises(KeyError):
            self.session.audio(self.sid, clip)
        events = self.session.events.read(0, 0)
        self.assertNotIn("测试秘密", json.dumps(events, ensure_ascii=False))

    def test_failed_memory_edit_still_sends_stop(self):
        with self.assertRaises(KeyError):
            self.session.mutate_memory("put", content="新内容", memory_id="missing")
        self.assertEqual(self.wait_event("stop")["reason"], "记忆操作撤销了旧回答")

    def test_duplicate_turn_not_executed_twice(self):
        first = self.submit()
        second = self.submit()
        self.wait_event("generation_finished")
        self.assertEqual(first, second)
        self.assertEqual(len(self.model.messages), 1)

    def test_negation_is_not_stop_or_end(self):
        self.submit("不要停止，我还想听你解释")
        self.wait_event("generation_finished")
        self.assertEqual(self.model.messages[-1][-1]["content"], "不要停止，我还想听你解释")
        self.assertFalse(any(event["kind"] == "standby" for event in self.session.events.read(0, 0)["events"]))

    def test_general_questions_and_camera_refusal_do_not_wait_for_a_frame(self):
        for index, text in enumerate((
            "生产日期是什么意思", "不要打开摄像头，聊聊包装设计", "今天看看新闻",
        )):
            with self.subTest(text=text):
                # 不只检查首次提问，也检查从视觉交流切回普通话题。
                self.session.visual = True
                cursor = self.session.events.sequence
                response = self.submit(text, f"ordinary-{index}")
                for thread in self.session.workers:
                    thread.join(timeout=1)
                    self.assertFalse(thread.is_alive())
                events = self.session.events.read(cursor, 0)["events"]
                self.assertFalse(any(event["kind"] == "frame_requested" for event in events))
                self.assertTrue(any(event["kind"] == "generation_finished" and
                                    event["turn_id"] == response["turn_id"] for event in events))
                self.assertEqual(self.model.messages[-1][-1]["content"], text)

    def test_visual_followup_requests_a_new_frame_and_explicit_refusal_wins(self):
        self.submit("看看这个", "first-image")
        first = self.wait_event("frame_requested")["request_id"]
        self.session.interrupt(self.sid)
        self.submit("它是什么颜色？", "second-image")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            request = self.session.frame_request
            if request and request[0] != first:
                break
            time.sleep(0.01)
        self.assertIsNotNone(self.session.frame_request)
        self.assertNotEqual(self.session.frame_request[0], first)
        self.session.submit(self.sid, "camera-refusal", "不要打开摄像头，聊聊包装设计", visual=True)
        self.assertFalse(self.session.visual)

    def test_actual_frame_and_playback_history_keep_observation_provenance(self):
        self.submit("看看这个是什么", "observed-object")
        request = self.wait_event("frame_requested")["request_id"]
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0010 0020 01 01 1100 ffd9")
        self.session.provide_frame(self.sid, request, base64.b64encode(jpeg).decode(), age_ms=1)
        self.wait_event("generation_finished")
        for event in self.session.events.read(0, 0)["events"]:
            if event["kind"] == "audio":
                self.session.playback(self.sid, event["clip_id"], "started")
                self.session.playback(self.sid, event["clip_id"], "completed")
        heard = self.session.history[-1]
        self.assertTrue(heard["visual"])
        self.assertIsNotNone(heard["frame_id"])
        self.assertEqual(heard["turn_id"], self.session.current.id)
        self.assertNotIn("images", heard)
        # 下一轮直接识物必须排除过去的视觉回答，仍实际请求新帧。
        self.submit("现在这个是什么", "changed-object")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            latest = self.session.frame_request
            if latest and latest[0] != request:
                break
            time.sleep(0.01)
        self.assertIsNotNone(self.session.frame_request)
        self.session.provide_frame(self.sid, self.session.frame_request[0],
                                   base64.b64encode(jpeg).decode(), age_ms=1)
        for worker in self.session.workers:
            worker.join(timeout=2)
        self.assertNotIn(heard["content"], str(self.model.messages[-1]))

    def test_memory_questions_negation_and_retraction_do_not_write(self):
        for index, text in enumerate((
            "记住了吗", "记住我说的话了吗？", "记住这个词是什么意思", "不要记住我的生日",
            "记住我喜欢蓝色，不，别记了", "把刚才那条记忆改成绿色了吗？",
        )):
            with self.subTest(text=text):
                self.submit(text, f"no-memory-{index}")
                self.assertEqual(self.memory.list(), [])
                self.assertEqual(self.memory.revision(), 0)

    def test_explicit_memory_write_correction_and_delete_still_work(self):
        self.submit("请记住，我喜欢简短回答", "remember")
        first = self.memory.list()[0]
        self.assertEqual(first["content"], "我喜欢简短回答")
        self.submit("请把刚才那条记忆改成我喜欢详细回答", "correct")
        rows = self.memory.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first["id"])
        self.assertEqual(rows[0]["content"], "我喜欢详细回答")
        self.submit("删除刚才那条记忆", "delete")
        self.assertEqual(self.memory.list(), [])

    def test_end_clears_context_but_keeps_memory(self):
        self.memory.put("我喜欢简短回答")
        self.submit("结束聊天")
        self.assertEqual(self.session.history, [])
        self.assertEqual(len(self.memory.list()), 1)
        self.assertEqual(self.wait_event("standby")["kind"], "standby")

    def test_reconnect_invalidates_old_session(self):
        self.session.connect("test-device")
        with self.assertRaises(ValueError):
            self.session.submit(self.sid, "old", "你好")

    def test_disconnect_cancels_running_answer_and_rejects_delayed_turn(self):
        self.memory.put("我喜欢简短回答")
        self.model.block = True
        self.submit()
        self.assertTrue(self.model.started.wait(2))
        self.session.disconnect(self.sid)
        for thread in self.session.workers:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        with self.assertRaises(ValueError):
            self.session.submit(self.sid, "late-question", "这是退出前发出的旧问题")
        self.assertIsNone(self.session.current)
        self.assertEqual(self.session.history, [])
        self.assertFalse(self.session.health()["device_connected"])
        self.assertEqual(len(self.memory.list()), 1)
        events = self.session.events.read(0, 0)["events"]
        self.assertFalse(any(event["kind"] == "audio" for event in events))

    def test_standby_can_resume_but_disconnected_session_cannot(self):
        self.session.interrupt(self.sid, end=True)
        self.submit("再次唤醒后的问题")
        self.wait_event("generation_finished")
        self.session.disconnect(self.sid)
        new_sid = self.session.connect("test-device")["session_id"]
        # 旧退出请求晚到，也不能撤销新连接。
        with self.assertRaises(ValueError):
            self.session.disconnect(self.sid)
        self.assertEqual(self.session.id, new_sid)
        self.session.submit(new_sid, "new-question", "新连接可以正常交流")
        self.wait_event("generation_finished")

    def test_frame_wait_is_cancelled_without_waiting_for_timeout(self):
        self.submit("看看这个")
        self.wait_event("frame_requested")
        self.session.interrupt(self.sid)
        for thread in self.session.workers:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.model.messages, [])

    def test_text_request_requires_high_resolution_and_keeps_date_task(self):
        self.submit("读一下这个包装上的生产日期", "read-date")
        event = self.wait_event("frame_requested")
        self.assertEqual(event["profile"], "text")
        small = bytes.fromhex("ffd8 ffc0 000b 08 0010 0020 01 01 1100 ffd9")
        with self.assertRaises(ValueError):
            self.session.provide_frame(self.sid, event["request_id"],
                                       base64.b64encode(small).decode(), age_ms=1)
        large = bytes.fromhex("ffd8 ffc0 000b 08 05a0 0780 01 01 1100 ffd9")
        self.session.provide_frame(self.sid, event["request_id"],
                                   base64.b64encode(large).decode(), age_ms=1)
        self.wait_event("generation_finished")
        self.assertIn("没有明确字段", self.model.messages[-1][0]["content"])
        self.assertEqual(self.session.frame["profile"], "text")

    def test_identification_keeps_needed_second_sentence_and_all_requested_parts(self):
        cases = [
            ("看看这个是什么", "现在看不清。请把物品靠近镜头并保持不动。"),
            ("看看这个是什么，还能看到哪些特征", "像一个盒子，但不能确认里面装什么。表面有蓝色和白色图案。"),
        ]
        for index, (question, answer) in enumerate(cases):
            with self.subTest(question=question):
                self.session.model.stream = lambda messages, turn: iter([answer])
                cursor = self.session.events.sequence
                self.submit(question, f"complete-answer-{index}")
                event = self.wait_event("frame_requested", after=cursor)
                jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0010 0020 01 01 1100 ffd9")
                self.session.provide_frame(self.sid, event["request_id"],
                                           base64.b64encode(jpeg).decode(), age_ms=1)
                for worker in self.session.workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                outputs = [item for item in self.session.events.read(0, 0)["events"]
                           if item["kind"] == "text" and item.get("final")]
                self.assertEqual(outputs[-1]["text"], answer)

    def test_memory_survives_restart_and_correction_replaces(self):
        saved = self.memory.put("我喜欢长回答")
        self.memory.put("我喜欢简短回答", memory_id=saved)
        other = MemoryStore(Path(self.directory.name) / "memory.db")
        try:
            self.assertEqual(other.list()[0]["content"], "我喜欢简短回答")
            self.assertNotIn("长回答", other.export())
        finally:
            other.close()

    def test_event_overrun_requires_resync(self):
        events = EventLog(capacity=2)
        for _ in range(3):
            events.publish("test")
        self.assertTrue(events.read(0, 0)["reset_required"])

    def test_intentional_clear_keeps_new_acknowledgement_deliverable(self):
        events = EventLog(capacity=8)
        events.publish("old-private-message")
        cursor = events.sequence
        events.publish("old-audio")
        events.clear()
        events.publish("new-acknowledgement")
        batch = events.read(cursor, 0)
        self.assertFalse(batch["reset_required"])
        self.assertTrue(batch["history_cleared"])
        self.assertIn("new-acknowledgement", [event["kind"] for event in batch["events"]])
        self.assertNotIn("old-audio", [event["kind"] for event in batch["events"]])

    def test_jpeg_dimensions_accept_trailer_but_reject_missing_end(self):
        header = bytes.fromhex("ffd8 ffc0 000b 08 0010 0020 01 01 1100 ffd9")
        self.assertEqual(jpeg_size(header + b"trailing-app-metadata"), (32, 16))
        with self.assertRaises(ValueError):
            jpeg_size(header[:-2])

    def test_http_auth_and_real_session_control(self):
        server = create_server(self.session, "test-token", ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base + "/health") as response:
                self.assertTrue(json.load(response)["ready"])
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/memories")
            self.assertEqual(error.exception.code, 401)
            req = Request(base + "/interrupt", json.dumps({"session_id": self.sid}).encode(),
                          {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
            with urlopen(req) as response:
                self.assertIn("epoch", json.load(response))
            req = Request(base + "/disconnect", json.dumps({"session_id": self.sid}).encode(),
                          {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
            with urlopen(req) as response:
                self.assertTrue(json.load(response)["disconnected"])
            req = Request(base + "/turn", json.dumps({
                "session_id": self.sid, "request_id": "late-http", "text": "迟到的问题",
            }).encode(), {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
