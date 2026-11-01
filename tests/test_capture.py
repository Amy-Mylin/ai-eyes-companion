"""确认显式诊断只留有限静帧，且生成结果不伪装成实际播放。"""

import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from eyes_companion.capture import TurnCapture


class CaptureTests(unittest.TestCase):
    def test_bounded_capture_keeps_exact_frame_and_no_base64_in_text(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = TurnCapture(directory, limit=1)
            frame = {"data": base64.b64encode(b"exact-frame").decode(), "frame_id": "f1"}
            messages = [{"role": "user", "content": "这是什么", "images": [frame["data"]]}]
            capture.begin(SimpleNamespace(id="first"), "这是什么", frame, messages, "test-only")
            capture.begin(SimpleNamespace(id="second"), "第二个", frame, messages, "test-only")
            capture.finish("first", "测试回答")
            root = Path(directory)
            self.assertEqual((root / "first.jpg").read_bytes(), b"exact-frame")
            self.assertFalse((root / "second.jpg").exists())
            record = json.loads((root / "first.json").read_text(encoding="utf-8"))
            self.assertNotIn("data", record["frame"])
            self.assertNotIn("images", record["messages"][0])
            self.assertEqual(record["state"], "generated")
            self.assertIn("不等于全部实际播完", record["boundary"])


if __name__ == "__main__":
    unittest.main()
