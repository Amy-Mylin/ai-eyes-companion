"""系统 Python 的独立相机进程；图像只通过父进程管道传输。"""

import base64
import io
import json
import os
import signal
import sys
import time


PROFILES = {"general": ("lores", (960, 720)), "text": ("main", (1920, 1440))}


def open_camera():
    try:
        from picamera2 import Picamera2
        from libcamera import controls
    except (ImportError, OSError) as error:
        raise RuntimeError("系统 Python 无法加载 Picamera2/libcamera；未自动安装或切换后端") from error
    camera = Picamera2()
    try:
        if "AfMode" not in camera.camera_controls:
            raise RuntimeError("当前相机未报告自动对焦控制")
        config = camera.create_video_configuration(
            main={"size": PROFILES["text"][1], "format": "YUV420"},
            lores={"size": PROFILES["general"][1], "format": "YUV420"},
            raw=None, buffer_count=4, queue=False,
            controls={"AfMode": controls.AfModeEnum.Continuous, "FrameRate": 3},
        )
        camera.configure(config)
        camera.options["quality"] = 90
        camera.start()
        return camera
    except BaseException:
        camera.close()
        raise


def capture(camera, command):
    remaining = command["deadline"] - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("取景请求已经过期")
    request = None
    try:
        job = camera.capture_request(wait=False, flush=True)
        request = job.get_result(timeout=min(remaining, 4.0))
        metadata = request.get_metadata()
        stream, size = PROFILES[command["profile"]]
        if tuple(request.config[stream]["size"]) != size:
            raise RuntimeError("相机实际配置尺寸与请求不一致")
        output = io.BytesIO()
        request.save(stream, output, format="jpeg")
        binary = output.getvalue()
        if len(binary) > 3_000_000:
            raise ValueError("相机 JPEG 超出3MB限制")
        if time.monotonic() >= command["deadline"]:
            raise TimeoutError("本轮相机编码已超过取景预算")
        return {
            "kind": "frame", "id": command["id"], "profile": command["profile"],
            "width": size[0], "height": size[1],
            "data": base64.b64encode(binary).decode("ascii"),
            "metadata": {
                "af_state": metadata.get("AfState"),
                "exposure_time_us": metadata.get("ExposureTime"),
                "sensor_timestamp_ns": metadata.get("SensorTimestamp"),
                "frame_duration_us": metadata.get("FrameDuration"),
                "lens_position_dioptres": metadata.get("LensPosition"),
                "analogue_gain": metadata.get("AnalogueGain"),
            },
        }
    finally:
        if request is not None:
            request.release()


def main():
    if sys.argv[1:] != ["--worker"] or sys.platform != "linux":
        print("此程序由 CM5 Picamera2 适配显式启动，不是独立演示入口。", file=sys.stderr)
        return 2
    # 保存独立协议描述符；第三方库的 stdout 也转到日志，不能混入图像协议。
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def send(event):
        data = memoryview((json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        while data:
            written = protocol.write(data)
            if not written:
                raise BrokenPipeError("父进程不再接收相机结果")
            data = data[written:]

    def stop(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    camera = None
    failed = False
    try:
        while True:
            line = sys.stdin.buffer.readline(4097)
            if not line:
                break
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise ValueError("相机请求格式错误")
            command = json.loads(line)
            if (not isinstance(command, dict) or command.get("profile") not in PROFILES
                    or not isinstance(command.get("id"), str) or len(command["id"]) > 64
                    or type(command.get("deadline")) not in (int, float)):
                raise ValueError("相机请求字段错误")
            if not 0 < command["deadline"] - time.monotonic() <= 4.1:
                raise TimeoutError("相机请求等待预算无效或已过期")
            if camera is None:
                camera = open_camera()
            send(capture(camera, command))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        failed = True
        try:
            send({"kind": "fatal", "error": f"{type(error).__name__}: {error}"[:500]})
        except (BrokenPipeError, OSError):
            pass
    finally:
        if camera is not None:
            for name in ("cancel_all_and_flush", "stop", "close"):
                try:
                    getattr(camera, name)()
                except Exception as error:
                    failed = True
                    print(f"相机清理失败 {name}: {type(error).__name__}", file=sys.stderr)
        protocol.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
