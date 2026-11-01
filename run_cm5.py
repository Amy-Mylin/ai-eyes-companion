"""CM5设备入口；--check不采集，--live才开启真实麦克风和播放。"""

import argparse
from functools import partial
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parent


def keyboard(device):
    print("终端控制：输入 /wake 唤醒，/stop 停声，/end 待机，/quit 退出。", flush=True)
    while not device.closed.is_set():
        line = sys.stdin.readline()
        if not line:
            return
        command = line.strip()
        if command == "/wake":
            device.wake_requested.set()
        elif command == "/stop":
            device.interrupt()
        elif command == "/end":
            device.interrupt(end=True)
        elif command == "/quit":
            device.closed.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--assets", type=Path, default=ROOT / "assets")
    parser.add_argument("--host", help="电脑有线网卡地址，例如http://192.168.10.1:4318")
    parser.add_argument("--token-file", type=Path, default=ROOT / ".local/device-token")
    parser.add_argument("--cues", type=Path, default=ROOT / "assets/cues", help="随部署包携带的本地短提示目录")
    parser.add_argument("--speech-barge-in", action="store_true",
                        help="启用仍待现场抗回声验证的直接语音插话；默认使用唤醒词打断")
    parser.add_argument("--show-text", action="store_true", help="允许终端显示本次对话文字")
    parser.add_argument("--capture-device", default="hw:CARD=Array,DEV=0")
    parser.add_argument("--playback-device", default="plughw:CARD=Array,DEV=0")
    parser.add_argument("--allow-system-shutdown", action="store_true",
                        help="温度超限停采播后请求系统关机；默认只停采播，不修改系统权限")
    parser.add_argument("--camera-backend", choices=("rpicam", "picamera2"), default="rpicam",
                        help="显式选择取景后端；picamera2使用一次配置的普通与高清双输出")
    parser.add_argument("--camera-python", default="/usr/bin/python3",
                        help="能加载系统Picamera2的Python，仅用于所选picamera2后端")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("此入口需要CM5的Linux环境；电脑端请运行start_host.ps1")
    if args.live and not args.host:
        parser.error("真实运行必须指定电脑当前有线地址")
    camera_executable = "rpicam-vid" if args.camera_backend == "rpicam" else args.camera_python
    for executable in ("arecord", "aplay", "amixer", camera_executable):
        if not shutil.which(executable):
            parser.error(f"缺少{executable}；未自动安装或改动旧环境")
    from cm5_client.device import Device
    from cm5_client.io import Api
    from cm5_client.listening import Listening
    from cm5_client.cues import load_cues
    from cm5_client.thermal import ThermalGuard
    import fcntl
    runtime = ROOT / ".runtime/cm5"
    runtime.mkdir(parents=True, exist_ok=True)
    listening = Listening(args.assets, runtime)
    cues = load_cues(args.cues)
    if args.check:
        print(json.dumps({"models_loaded": True, "capture_channel": listening.config["capture_channel"],
                          "capture_started": False, "camera_started": False, "playback_started": False,
                          "local_cues": list(cues),
                          "boundary": "只核对本地资源；不是实际采播或网络通过"}, ensure_ascii=False))
        return
    api = Api(args.host, args.token_file.read_text(encoding="utf-8").strip())
    if not api.request("/health")["ready"]:
        raise RuntimeError("电脑服务尚未准备好")
    camera_factory = None
    if args.camera_backend == "picamera2":
        # 系统相机依赖放在独立worker里，保持已有语音Python环境不变。
        from cm5_client.picamera_backend import PicameraCamera
        camera_factory = partial(PicameraCamera, worker_python=args.camera_python)
    device = None
    thermal = None
    with (runtime / "client.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("本入口已有实例运行") from error
        try:
            # 保留使用者已经设置的系统音量，不套用原设备的声卡控制值。
            device = Device(api, runtime, args.playback_device, args.show_text, cues=cues,
                            camera_factory=camera_factory)
            thermal = ThermalGuard(
                runtime,
                lambda: device.fail("温度保护：读温异常或达到保护阈值，已停止本次交流。", audible=False),
                allow_shutdown=args.allow_system_shutdown,
            )
            thermal.start()
            threading.Thread(target=keyboard, args=(device,), daemon=True).start()
            print(f"等待唤醒：“{listening.config['phrase']}”。本次已开启麦克风；视觉问题才开启相机。", flush=True)
            print("直接插话待验证" if args.speech_barge_in else "当前用唤醒词打断；还不能称为完整自由插话", flush=True)
            print(f"本次相机后端：{args.camera_backend}；尚不代表文字准确性通过。", flush=True)
            listening.run(device, args.capture_device, runtime, args.speech_barge_in)
            if device.failure:
                raise RuntimeError(device.failure)
        except KeyboardInterrupt:
            print("用户停止。", flush=True)
        except Exception as error:
            if device:
                device.fail(f"交流入口已停止：{type(error).__name__}")
            raise
        finally:
            if thermal:
                # 已触发的过热处理不能被正常收尾提前取消，关机权限失败会明确记录。
                thermal.close()
                if thermal.last_status == "shutdown_not_authorized_or_failed":
                    print("采播已停止，但系统未确认接受关机请求，需要现场关机。", flush=True)
            if device:
                device.close()


if __name__ == "__main__":
    main()
