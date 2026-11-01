# 模型与运行资源

这些资源须单独准备，不在源码 ZIP 中。下面给出当前适配器要求的文件布局；模型标签、软件版本和模型许可证需分别记录。缺少资源时程序报错，不回退到预录答案或云服务。

## 电脑模型

- 参考配置：`qwen3.5:4b`，Q4_K_M 量化，4096 上下文，`think: false`。
- 官方模型条目：https://ollama.com/library/qwen3.5:4b
- 模型家族来源：https://huggingface.co/Qwen/Qwen3.5-4B
- 默认文档路径连接已启动的本机 Ollama；由 Ollama 管理模型位置。下载后保存 `ollama show` 和实际模型摘要，远程同名标签不保证永远是同一个文件。
- 如用本项目管理独立运行器，提供 `--ollama`、`--models`，不同时使用 `--ollama-url`。该方式默认监听 11438，不接管已有进程。
- 电脑可选 Kokoro TTS：安装 `requirements-kokoro.txt`，资源放入 `assets/models/sherpa/kokoro-int8-multi-lang-v1_1/`，运行时选 `--tts kokoro`。这不是本次重新实测的主要语音路径。
- Kokoro 配置依据：https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html

## CM5 模型

唤醒模型官方包：

https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2

中文 ASR 官方包：

https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2

解压后，**将内容放入下面的项目目录**，不要改动模型内部的文件名，不要选成 sherpa-ncnn 格式：

```text
assets/
  wake_config.json
  cues/
    wake.wav
    ready.wav
    standby.wav
    retry.wav
    failure.wav
  models/
    kws-zh-3m/
      encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
      decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx
      joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx
      tokens.txt
    asr-zh-14m/
      encoder-epoch-99-avg-1.int8.onnx
      decoder-epoch-99-avg-1.onnx
      joiner-epoch-99-avg-1.int8.onnx
      tokens.txt
```

`wake_config.json` 从仓库示例复制；它包含唤醒短语、该模型使用的拼音 token 和声道配置，不包含设备接入令牌。换唤醒词时需要一起核对 token，不能只改中文字符串。

模型下载、Python 依赖下载和中文系统音色安装在联网准备时完成。离线运行不安装缺少的资源。仅下载成功不是准确性、延迟、断网启动或续航验收。

## 许可与版本

Ollama、sherpa-onnx 的软件许可证与具体模型权重的许可证分开核对；不要从推理框架的许可证推断所有模型都可任意再分发。本仓库只提供接口代码和来源链接，不重新分发权重、Windows 音色或运行时二进制。
