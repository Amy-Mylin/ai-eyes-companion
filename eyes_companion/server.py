"""只提供本项目需要的设备接口；默认仅监听电脑本机。"""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlparse

from .providers import Cancelled


def create_server(session, token, address=("127.0.0.1", 4318)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # 不把含会话标识或私人内容的URL写入默认访问日志。
            return

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def authorized(self):
            return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                state = session.health()
                if session.observer:
                    state["dashboard"] = session.observer.status
                return self.reply(200, state)
            if not self.authorized():
                return self.reply(401, {"error": "设备接入令牌缺失或不正确"})
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/events":
                    after = int(query.get("after", ["0"])[0])
                    if after < 0:
                        raise ValueError("事件游标不能为负")
                    data = session.poll(query.get("session_id", [""])[0], after)
                    return self.reply(200, data)
                if parsed.path == "/audio":
                    data = session.audio(query.get("session_id", [""])[0], query.get("clip_id", [""])[0])
                    return self.reply(200, data, "audio/wav")
                if parsed.path == "/memories":
                    return self.reply(200, {"revision": session.memory.revision(), "memories": session.memory.list()})
                if parsed.path == "/memories/export":
                    return self.reply(200, session.memory.export().encode("utf-8"))
                return self.reply(404, {"error": "接口不存在"})
            except (ValueError, KeyError, Cancelled) as error:
                return self.reply(409, {"error": str(error) or "请求已经取消"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401, {"error": "设备接入令牌缺失或不正确"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4_300_000:
                    return self.reply(413, {"error": "请求为空或过大"})
                if self.headers.get_content_type() != "application/json":
                    return self.reply(415, {"error": "请求必须使用JSON"})
                self.connection.settimeout(10)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须为对象")
                if self.path == "/connect":
                    data = session.connect(payload.get("client_id"), payload.get("mode", "device"))
                elif self.path == "/turn":
                    data = session.submit(payload.get("session_id"), payload.get("request_id"),
                                          payload.get("text"), visual=payload.get("visual"))
                elif self.path == "/interrupt":
                    if type(payload.get("end", False)) is not bool:
                        raise ValueError("结束标记必须是布尔值")
                    data = session.interrupt(payload.get("session_id"), end=payload.get("end", False))
                elif self.path == "/disconnect":
                    data = session.disconnect(payload.get("session_id"))
                elif self.path == "/frame":
                    data = session.provide_frame(payload.get("session_id"), payload.get("request_id"),
                                                 payload.get("data"), age_ms=payload.get("age_ms"))
                elif self.path == "/playback":
                    data = session.playback(payload.get("session_id"), payload.get("clip_id"), payload.get("state"))
                elif self.path == "/device-status":
                    with session.lock:
                        session.require(payload.get("session_id"))
                        session.emit("device_status", capture_ready=payload.get("capture_ready") is True,
                                     playback_ready=payload.get("playback_ready") is True)
                    data = {"accepted": True}
                elif self.path == "/memories/put":
                    data = session.mutate_memory("put", content=payload.get("content"),
                                                 memory_id=payload.get("memory_id"),
                                                 context=payload.get("context", "用户明确要求记住"))
                elif self.path == "/memories/delete":
                    data = session.mutate_memory("delete", memory_id=payload.get("memory_id"))
                elif self.path == "/shutdown":
                    session.stopped.set()
                    data = {"stopping": True}
                else:
                    return self.reply(404, {"error": "接口不存在"})
                return self.reply(200, data)
            except (ValueError, TypeError, KeyError, Cancelled) as error:
                return self.reply(409, {"error": str(error) or "请求已经取消"})
            except RuntimeError as error:
                return self.reply(503, {"error": str(error)})
            except Exception:
                return self.reply(500, {"error": "服务内部错误；没有确认操作完成"})

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server
