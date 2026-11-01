# 依赖与许可边界

源码包只包含本项目应用代码、示例配置、软件测试和文档，不包含下列第三方软件实现、模型权重、系统音色、驱动或相机固件。

| 使用的组件 | 来源 | 在本项目中的作用 |
| --- | --- | --- |
| Ollama | https://github.com/ollama/ollama | 本机模型服务，单独安装 |
| Qwen3.5 4B | https://huggingface.co/Qwen/Qwen3.5-4B | 视觉语言模型，单独下载 |
| sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx | 中文唤醒、流式识别及可选 TTS |
| HTTPX | https://github.com/encode/httpx | 本机模型 HTTP 客户端 |
| NumPy | https://numpy.org/ | 仅可选 Kokoro 语音后端需要 |
| Picamera2 | https://github.com/raspberrypi/picamera2 | CM5 相机双输出，使用系统安装 |
| rpicam-apps | https://github.com/raspberrypi/rpicam-apps | 相机备选后端 |
| Windows System.Speech 与中文音色 | Windows 系统组件 | 在安装音色的电脑上本地生成语音，不随源码分发 |

各依赖及权重按其上游许可使用。模型许可证与框架许可证不是同一项。

项目所有者尚未指定本项目应用代码的开源许可证。本次整理不代替所有者授予 MIT、Apache-2.0 等再使用许可，也不把第三方模型归为本项目自研。需要真正开放再使用权限时，由所有者选定许可证再加入 `LICENSE`。

公开演示材料需要另外筛选。本包不包括旧项目 Logo、现场拍摄照片、音频、私人对话、记忆数据库、设备标识和 SSH 材料。
