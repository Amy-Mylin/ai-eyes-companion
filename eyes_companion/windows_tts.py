"""当前Windows入口的快速语音后端；使用已有音色，不下载语音包。"""

import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import uuid4

from .providers import Turn


class WindowsTts:
    def __init__(self):
        self.process = None
        self.voice = None
        self.replies = queue.Queue(maxsize=16)
        self.lock = threading.Lock()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.replies.put(json.loads(line), timeout=2)
        except Exception:
            pass
        finally:
            try:
                self.replies.put_nowait({"error": "Windows中文语音进程结束"})
            except queue.Full:
                pass

    def _receive(self, request_id=None, turn=None, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if turn:
                turn.check()
            try:
                result = self.replies.get(timeout=0.05)
            except queue.Empty:
                continue
            if result.get("id") not in (None, request_id):
                continue
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result
        raise TimeoutError("Windows中文语音没有按时返回")

    def warm(self):
        if os.name != "nt":
            raise RuntimeError("Windows语音候选仅适用于当前Windows电脑")
        script = Path(__file__).with_name("windows_tts.ps1")
        self.process = subprocess.Popen([
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            result = self._receive()
            if not result.get("ready"):
                raise RuntimeError("系统没有可用的指定中文音色")
            self.voice = result.get("voice")
            self.synthesize("准备好了。", Turn("warm", 0, 0))
        except Exception:
            self.close()
            raise

    def synthesize(self, text, turn):
        while not self.lock.acquire(timeout=0.05):
            turn.check()
        try:
            turn.check()
            request_id = uuid4().hex
            self.process.stdin.write(json.dumps({"id": request_id, "text": text}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            result = self._receive(request_id, turn)
            turn.check()
            return base64.b64decode(result["wav"], validate=True)
        finally:
            self.lock.release()

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
