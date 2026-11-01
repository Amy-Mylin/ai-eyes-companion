"""只加载随程序携带的短提示，运行时不请求模型或网络。"""

import io
from pathlib import Path
import wave


CUE_TEXT = {
    "ready": "等待唤醒。",
    "standby": "已待机。",
    "retry": "没有完成，请重试。",
    "failure": "交流已停止。",
}
CUE_NAMES = ("wake", *CUE_TEXT)


def load_cues(directory: Path):
    cues = {}
    for name in CUE_NAMES:
        path = directory / (name + ".wav")
        data = path.read_bytes()
        if len(data) > 500_000:
            raise ValueError(f"本地提示文件过大：{name}")
        with wave.open(io.BytesIO(data), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 0 < duration <= 5:
                raise ValueError(f"本地提示格式或时长不合适：{name}")
        cues[name] = data
    return cues
