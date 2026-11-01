"""本地模型和CPU语音合成；不设置云端兜底，不调用下载接口。"""

import io
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
import wave

import httpx


class Cancelled(Exception):
    pass


class Turn:
    def __init__(self, turn_id, epoch, revision):
        self.id = turn_id
        self.epoch = epoch
        self.revision = revision
        self.cancelled = threading.Event()
        self.response = None
        self.started = time.monotonic()
        self.model_metrics = {}

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def cancel(self):
        self.cancelled.set()
        response = self.response
        if response is not None:
            # 关闭连接不在控制线程等待，停声事件必须能立即送出。
            threading.Thread(target=self._close, args=(response,), daemon=True).start()

    @staticmethod
    def _close(response):
        try:
            response.close()
        except Exception:
            pass


class LocalModel:
    def __init__(self, base_url, model="qwen3.5:4b", context=4096):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("离线模型地址只能是本机HTTP服务")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.context = context
        self.lock = threading.Lock()

    def warm(self):
        with httpx.Client(trust_env=False, timeout=120) as client:
            show = client.post(self.base_url + "/api/show", json={"model": self.model})
            show.raise_for_status()
            if "vision" not in show.json().get("capabilities", []):
                raise RuntimeError("所选模型没有报告视觉能力")
            result = client.post(self.base_url + "/api/generate", json={
                "model": self.model, "keep_alive": -1, "stream": False,
                "options": {"num_ctx": self.context},
            })
            result.raise_for_status()
            if result.json().get("error"):
                raise RuntimeError(result.json()["error"])

    def stream(self, messages, turn):
        while not self.lock.acquire(timeout=0.1):
            turn.check()
        try:
            turn.check()
            with httpx.Client(trust_env=False, timeout=httpx.Timeout(30, connect=3)) as client:
                with client.stream("POST", self.base_url + "/api/chat", json={
                    "model": self.model, "messages": messages, "think": False,
                    "stream": True, "keep_alive": -1,
                    "options": {"num_ctx": self.context, "num_predict": 384,
                                "temperature": 0.7, "top_p": 0.8},
                }) as response:
                    turn.response = response
                    response.raise_for_status()
                    finished = False
                    for line in response.iter_lines():
                        turn.check()
                        if not line:
                            continue
                        event = json.loads(line)
                        if event.get("error"):
                            raise RuntimeError(event["error"])
                        message = event.get("message", {})
                        if message.get("thinking"):
                            raise RuntimeError("模型仍在输出思考内容，配置需要核对")
                        text = message.get("content", "")
                        if text:
                            yield text
                        if event.get("done"):
                            # 后端计时不包含采音、取图和扬声器首声，分开留存用于定位等待。
                            turn.model_metrics = {
                                key: event[key] for key in (
                                    "load_duration", "prompt_eval_duration", "eval_duration",
                                    "prompt_eval_count", "eval_count", "total_duration", "done_reason",
                                ) if key in event
                            }
                            if event.get("done_reason") == "length":
                                raise RuntimeError("回答达到长度限制，未完整生成")
                            finished = True
                    if not finished:
                        raise RuntimeError("模型流在完成标记前断开")
        finally:
            turn.response = None
            self.lock.release()


class LocalTts:
    def __init__(self, model_dir: Path, runtime_dir: Path, speaker=3, threads=4):
        self.model_dir = model_dir
        self.runtime_dir = runtime_dir
        self.speaker = speaker
        self.threads = threads
        self.lock = threading.Lock()
        self.engine = None

    def warm(self):
        import sherpa_onnx
        with self.lock:
            if self.engine is not None:
                return
            model = self.model_dir
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            filtered = self.runtime_dir / "lexicon-zh.filtered.txt"
            tokens = {line.rsplit(" ", 1)[0] for line in
                      (model / "tokens.txt").read_text(encoding="utf-8").splitlines() if line.strip()}
            # 只写新目录，保留旧模型资源；剔除词典里模型不支持的音素。
            with (model / "lexicon-zh.txt").open(encoding="utf-8") as source, \
                    filtered.open("w", encoding="utf-8") as output:
                for line in source:
                    fields = line.split()
                    if len(fields) > 1 and all(phone in tokens for phone in fields[1:]):
                        output.write(line)
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(model / "model.int8.onnx"), voices=str(model / "voices.bin"),
                        tokens=str(model / "tokens.txt"), data_dir=str(model / "espeak-ng-data"),
                        lexicon=f"{model / 'lexicon-us-en.txt'},{filtered}",
                    ), provider="cpu", num_threads=self.threads, debug=False,
                ), max_num_sentences=1, silence_scale=0.4,
            )
            config.rule_fsts = ",".join(str(model / name) for name in
                                      ("phone-zh.fst", "date-zh.fst", "number-zh.fst"))
            if not config.validate():
                raise RuntimeError("本地语音资源不完整或配置无效")
            self.engine = sherpa_onnx.OfflineTts(config)
            self.generation = sherpa_onnx.GenerationConfig()
            self.generation.sid = self.speaker
            self.generation.speed = 1.0
            # 预先完成文本处理初始化，避免把这段等待留给第一问。
            self.engine.generate("准备好了。", self.generation)

    def synthesize(self, text, turn):
        import numpy as np
        turn.check()
        while not self.lock.acquire(timeout=0.1):
            turn.check()
        try:
            turn.check()
            if self.engine is None:
                raise RuntimeError("语音模型尚未准备好")
            audio = self.engine.generate(text, self.generation)
            turn.check()
            pcm = (np.clip(np.asarray(audio.samples), -1, 1) * 32767).astype("<i2").tobytes()
            stream = io.BytesIO()
            with wave.open(stream, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(audio.sample_rate)
                output.writeframes(pcm)
            return stream.getvalue()
        finally:
            self.lock.release()
