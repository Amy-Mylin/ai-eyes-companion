"""显式开启的有限视觉诊断：只保存本轮实际输入静帧与生成文字。"""

import base64
import hashlib
import json
from pathlib import Path
import threading


class TurnCapture:
    def __init__(self, directory, limit=6):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.records = {}
        self.lock = threading.Lock()

    def begin(self, turn, text, frame, messages, model):
        if not frame:
            return
        with self.lock:
            if len(self.records) >= self.limit:
                return
            binary = base64.b64decode(frame["data"], validate=True)
            record = {
                "boundary": "为本轮准备的静帧与文字；generated表示模型完整返回，非连续录像；生成回答不等于全部实际播完",
                "turn_id": turn.id, "question": text, "model": model,
                "frame": {key: value for key, value in frame.items() if key != "data"},
                "image": turn.id + ".jpg", "image_sha256": hashlib.sha256(binary).hexdigest(),
                "messages": [{key: value for key, value in item.items() if key != "images"}
                             for item in messages],
                "state": "input_prepared",
            }
            (self.directory / record["image"]).write_bytes(binary)
            self.records[turn.id] = record
            self._save(turn.id)

    def finish(self, turn_id, answer, state="generated", metrics=None):
        with self.lock:
            if turn_id in self.records:
                self.records[turn_id].update(answer=answer, state=state)
                if metrics:
                    self.records[turn_id]["metrics"] = metrics
                self._save(turn_id)

    def _save(self, turn_id):
        (self.directory / (turn_id + ".json")).write_text(
            json.dumps(self.records[turn_id], ensure_ascii=False, indent=2), encoding="utf-8")
