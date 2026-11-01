"""离线生成系统短提示，素材独立保存，CM5运行时不再合成。"""

from array import array
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cm5_client.cues import CUE_TEXT, load_cues
from eyes_companion.providers import Turn
from eyes_companion.windows_tts import WindowsTts


def wake_tone():
    rate, duration = 16000, 0.16
    samples = array("h")
    for index in range(round(rate * duration)):
        seconds = index / rate
        fade = min(1, seconds / 0.01, (duration - seconds) / 0.01)
        samples.append(round(0.16 * 32767 * fade * math.sin(2 * math.pi * 880 * seconds)))
    if sys.byteorder != "little":
        samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


def main():
    destination = ROOT / "assets/cues"
    destination.mkdir(parents=True, exist_ok=True)
    tone = destination / "wake.wav"
    if not tone.exists():
        tone.write_bytes(wake_tone())
    missing = [name for name in CUE_TEXT if not (destination / (name + ".wav")).exists()]
    if missing:
        voice = WindowsTts()
        try:
            voice.warm()
            for name in missing:
                (destination / (name + ".wav")).write_bytes(voice.synthesize(CUE_TEXT[name], Turn(name, 0, 0)))
        finally:
            voice.close()
    cues = load_cues(destination)
    manifest = {"source": "wake为本地生成160毫秒提示音；中文为已安装Microsoft Huihui Desktop离线生成",
                "boundary": "已生成并验证WAV结构；尚未在CM5实听，不代表唤醒或扬声器验收", "files": {}}
    for name, data in cues.items():
        with wave.open(io.BytesIO(data), "rb") as audio:
            manifest["files"][name + ".wav"] = {
                "text": CUE_TEXT.get(name, "短提示音"), "sha256": hashlib.sha256(data).hexdigest(),
                "seconds": round(audio.getnframes() / audio.getframerate(), 3), "bytes": len(data),
            }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"directory": str(destination), **manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
