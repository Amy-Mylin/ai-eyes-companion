"""只管理本次启动的模型进程，运行目录和配置不进入旧项目。"""

import os
from pathlib import Path
import socket
import shutil
import subprocess
import time

import httpx


class ModelRuntime:
    def __init__(self, assets: Path, runtime: Path, port=11438, *, models=None,
                 model="qwen3.5:4b", executable=None):
        self.assets = Path(assets).expanduser().resolve()
        self.runtime, self.port = Path(runtime).expanduser().resolve(), port
        # 子进程工作目录不同，须在调用方目录解释相对模型路径。
        self.models = Path(models).expanduser().resolve() if models else self.assets / "models/ollama"
        self.model = model
        self.executable = Path(executable).expanduser().resolve() if executable else None
        self.process = None
        self.log = None

    def start(self):
        executable = self.executable
        if executable is None:
            bundled = self.assets / "runtime/ollama" / ("ollama.exe" if os.name == "nt" else "ollama")
            executable = bundled if bundled.is_file() else Path(shutil.which("ollama") or "__ollama_missing__")
        models = self.models
        if not executable.is_file() or not models.is_dir():
            raise RuntimeError("已有运行器或模型资源不存在，未自动下载")
        with socket.socket() as reservation:
            if os.name == "nt":
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", self.port))
        self.runtime.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "OLLAMA_HOST": f"127.0.0.1:{self.port}", "OLLAMA_MODELS": str(models),
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1",
            "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_CONTEXT_LENGTH": "4096", "OLLAMA_KEEP_ALIVE": "-1",
        })
        self.log = (self.runtime / "model-runtime.log").open("ab")
        self.process = subprocess.Popen([str(executable), "serve"], env=env, cwd=self.runtime,
                                        stdout=self.log, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("本次模型进程退出，请查看本地运行日志")
                try:
                    response = client.get(f"http://127.0.0.1:{self.port}/api/version")
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPError:
                    time.sleep(0.2)
        raise RuntimeError("模型服务未按时启动")

    def close(self):
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                # Ollama会创建推理子进程；只清理本次仍存活进程的整棵子树。
                # 不能按进程名称清理，否则会误停其他应用的服务。
                try:
                    subprocess.run(["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    with httpx.Client(trust_env=False, timeout=8) as client:
                        client.post(f"http://127.0.0.1:{self.port}/api/generate",
                                    json={"model": self.model, "keep_alive": 0})
                except httpx.HTTPError:
                    pass
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()
