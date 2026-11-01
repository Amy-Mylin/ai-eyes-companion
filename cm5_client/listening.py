"""复用已验证的CM5中文唤醒和识别资源，持续读取同一条采音流。"""

from array import array
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

from .io import stop_process


class Listening:
    def __init__(self, assets: Path, runtime: Path):
        from sherpa_onnx import KeywordSpotter, OnlineRecognizer
        self.config = json.loads((assets / "wake_config.json").read_text(encoding="utf-8"))
        if self.config["capture_channel"] not in (1, 2):
            raise ValueError("当前采集只配置双声道，请核对实际USB通道")
        runtime.mkdir(parents=True, exist_ok=True)
        keywords = runtime / "keywords.txt"
        keywords.write_text(f"{self.config['tokens']} @{self.config['phrase']}\n", encoding="utf-8")
        kws, asr = assets / "models/kws-zh-3m", assets / "models/asr-zh-14m"
        self.spotter = KeywordSpotter(
            **{part: str(kws / f"{part}-epoch-12-avg-2-chunk-16-left-64.int8.onnx")
               for part in ("encoder", "decoder", "joiner")},
            tokens=str(kws / "tokens.txt"), keywords_file=str(keywords), num_threads=2,
            provider="cpu", sample_rate=16000, keywords_threshold=self.config["threshold"],
            keywords_score=self.config["score"],
        )
        self.recognizer = OnlineRecognizer.from_transducer(
            encoder=str(asr / "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=str(asr / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(asr / "joiner-epoch-99-avg-1.int8.onnx"),
            tokens=str(asr / "tokens.txt"), num_threads=2, sample_rate=16000,
            feature_dim=80, decoding_method="greedy_search", provider="cpu",
            enable_endpoint_detection=True, rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=0.6, rule3_min_utterance_length=15.0,
        )
        self.kws_stream = self.spotter.create_stream()
        self.asr_stream = self.recognizer.create_stream()
        self.active = False
        self.previous = ""
        self.interrupted = False
        self.last_voice = time.monotonic()

    def reset_asr(self):
        self.asr_stream = self.recognizer.create_stream()
        self.previous = ""
        self.interrupted = False

    def feed(self, raw, device, speech_barge_in):
        pcm = array("h", raw)
        if sys.byteorder != "little":
            pcm.byteswap()
        samples = [value / 32768 for value in pcm[self.config["capture_channel"] - 1::2]]
        now = time.monotonic()
        if device.standby_requested.is_set():
            device.standby_requested.clear()
            was_active = self.active
            self.active = False
            self.reset_asr()
            if was_active:
                device.notice("standby")
        keyword = False
        self.kws_stream.accept_waveform(16000, samples)
        while self.spotter.is_ready(self.kws_stream):
            self.spotter.decode_stream(self.kws_stream)
            if self.spotter.get_result(self.kws_stream):
                keyword = True
                self.kws_stream = self.spotter.create_stream()
                break
        if keyword or device.wake_requested.is_set():
            device.wake_requested.clear()
            device.interrupt()
            self.active = True
            self.last_voice = now
            self.reset_asr()
            device.notice("wake")
            print("已进入交流，可以继续说；结束时说“结束聊天”。", flush=True)
            return
        if not self.active:
            return
        if device.player.speech_playing.is_set() and not speech_barge_in:
            # 抗回声未验收时只允许唤醒词抢话，明确属于暂时的交互限制。
            self.reset_asr()
            self.last_voice = now
            return
        self.asr_stream.accept_waveform(16000, samples)
        while self.recognizer.is_ready(self.asr_stream):
            self.recognizer.decode_stream(self.asr_stream)
        text = self.recognizer.get_result_all(self.asr_stream).text.strip()
        if text and text != self.previous:
            self.previous = text
            self.last_voice = now
            if device.busy() and not self.interrupted:
                device.interrupt()
                self.interrupted = True
        if self.recognizer.is_endpoint(self.asr_stream):
            if text:
                if device.show_text:
                    print("用户：" + text, flush=True)
                device.submit(text)
                self.last_voice = now
            self.reset_asr()
        # 不在回答尚未完成时自动结束；安静待机只释放临时会话和相机。
        if not device.busy() and now - self.last_voice > 90:
            device.interrupt(end=True)

    def run(self, device, capture_device, runtime, speech_barge_in=False):
        capture = None
        try:
            with (runtime / "capture.log").open("ab") as log:
                capture = subprocess.Popen([
                    "arecord", "-q", "-D", capture_device, "-t", "raw", "-f", "S16_LE",
                    "-r", "16000", "-c", "2",
                ], stdout=subprocess.PIPE, stderr=log, bufsize=0)
            pending = bytearray()
            last_audio = time.monotonic()
            reported = False
            while not device.closed.is_set():
                if capture.poll() is not None:
                    raise RuntimeError("采音提前退出，请核对USB声卡及其他程序占用")
                readable, _, _ = select.select([capture.stdout], [], [], 0.2)
                if readable:
                    raw = os.read(capture.stdout.fileno(), 6400)
                    if not raw:
                        raise RuntimeError("采音流关闭")
                    pending.extend(raw)
                    last_audio = time.monotonic()
                    if not reported:
                        device.report_capture()
                        device.notice("ready")
                        reported = True
                    while len(pending) >= 3200:
                        self.feed(pending[:3200], device, speech_barge_in)
                        del pending[:3200]
                if time.monotonic() - last_audio > 3:
                    raise RuntimeError("超过3秒未收到录音，已停止交流")
        finally:
            stop_process(capture)
            if capture:
                capture.stdout.close()
