"""启动离线交流电脑端；真实采音和播放由CM5端显式启动。"""

import argparse
import os
from pathlib import Path
import secrets
import signal
import threading
import time

from eyes_companion.dashboard import Dashboard
from eyes_companion.capture import TurnCapture
from eyes_companion.memory import MemoryStore
from eyes_companion.providers import LocalModel, LocalTts
from eyes_companion.runtime import ModelRuntime
from eyes_companion.server import create_server
from eyes_companion.session import Session
from eyes_companion.windows_tts import WindowsTts


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=ROOT / "assets",
                        help="本地模型资源目录；不自动下载")
    parser.add_argument("--bind", default="127.0.0.1", help="默认本机；连接CM5时指定电脑有线网卡地址")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--model-port", type=int, default=11438)
    parser.add_argument("--model", default="qwen3.5:4b", help="已安装的精确模型标签，不自动下载")
    parser.add_argument("--models", type=Path, help="可选的独立模型目录；新模型不写入旧项目")
    parser.add_argument("--ollama-url", help="使用已经启动的本机Ollama，如http://127.0.0.1:11434；退出时不关闭它")
    parser.add_argument("--ollama", type=Path, help="由本程序管理时使用的Ollama可执行文件路径")
    parser.add_argument("--capture-directory", type=Path,
                        help="本次显式留存最多6轮视觉输入静帧与文字，默认关闭，不保存录音录像")
    parser.add_argument("--dashboard", action="store_true", help="向已有大屏发送真实事件")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:4317")
    parser.add_argument("--dashboard-token-file", type=Path,
                        help="另行部署的大屏接入令牌文件；大屏不在本源码包内")
    parser.add_argument("--allow-display", action="store_true", help="允许将本次画面和交流内容展示到大屏")
    parser.add_argument("--tts", choices=("kokoro", "windows"),
                        default="windows" if os.name == "nt" else "kokoro",
                        help="当前Windows入口默认使用快速系统语音；可显式选kokoro，音色仍待试听")
    args = parser.parse_args()
    if args.ollama_url and (args.models or args.ollama):
        parser.error("使用已有Ollama时，模型目录和运行器由该服务配置")
    if args.dashboard and not args.dashboard_token_file:
        parser.error("启用外部大屏时需要--dashboard-token-file")
    model_url = args.ollama_url or f"http://127.0.0.1:{args.model_port}"
    # 地址先验证，避免误把私人画面发往外部服务。
    model = LocalModel(model_url, model=args.model)
    private = ROOT / ".local"
    private.mkdir(exist_ok=True)
    token_path = private / "device-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        if os.name != "nt":
            token_path.chmod(0o600)
    token = token_path.read_text(encoding="utf-8").strip()
    runtime = None if args.ollama_url else ModelRuntime(
        args.assets, ROOT / ".runtime/host", args.model_port,
        models=args.models, model=args.model, executable=args.ollama)
    memory = MemoryStore(private / "memory.sqlite3")
    tts = WindowsTts() if args.tts == "windows" else LocalTts(
        args.assets / "models/sherpa/kokoro-int8-multi-lang-v1_1", ROOT / ".runtime/tts")
    capture = TurnCapture(args.capture_directory) if args.capture_directory else None
    session = Session(model, tts,
                      memory, display_allowed=args.allow_display, capture=capture)
    server = None
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        # 先占用应用端口，避免两个实例同时更改同一份记忆。
        server = create_server(session, token, (args.bind, args.port))
        # 加载期间即可查询准备状态；Session会拒绝尚未就绪的问答。
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if runtime:
            version = runtime.start()
            print(f"本地运行器：{version.get('version')}；正在预加载，尚未就绪。", flush=True)
        else:
            print("连接已有本机模型服务；正在预加载，尚未就绪。", flush=True)
        if args.dashboard:
            session.observer = Dashboard(args.dashboard_url, args.dashboard_token_file)
        session.warm()
        if not session.ready:
            raise RuntimeError(session.failure)
        voice = getattr(tts, "voice", None) or "Kokoro（当前配置）"
        print(f"当前语音：{type(tts).__name__} / {voice}；仅生成设备所需音频。", flush=True)
        print(f"交流服务已就绪：http://{args.bind}:{args.port}；按 Ctrl+C 停止。", flush=True)
        print("未启动麦克风、摄像头或播放。设备令牌保存在 .local/device-token，勿粘贴到聊天。", flush=True)
        while not stop.wait(1) and not session.stopped.is_set():
            with session.lock:
                if session.client_id and time.monotonic() - session.client_seen > 20:
                    session.disconnect(session.id, reason="设备连接超时")
    finally:
        session.close()
        if hasattr(tts, "close"):
            tts.close()
        if session.observer:
            session.observer.close()
        if server:
            if 'thread' in locals() and thread.is_alive():
                server.shutdown()
            server.server_close()
        if runtime:
            runtime.close()
        memory.close()


if __name__ == "__main__":
    main()
