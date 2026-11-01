"""使用实际留存的完整消息和原图离线回放；不接设备服务、不伪造取图或播放回执。"""

import argparse
import base64
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eyes_companion.context import build_messages, visual_task
from eyes_companion.images import jpeg_size
from eyes_companion.providers import LocalModel, Turn
from eyes_companion.runtime import ModelRuntime
from tools.inspect_capture import inspect_record


def load_input(path, current_reading=False):
    integrity = inspect_record(path)
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    messages = [dict(message) for message in record['messages']]
    if any(message.get('role') not in {'system', 'user', 'assistant'} for message in messages):
        raise ValueError('留存消息角色不受当前模型适配器支持')
    if messages[-1].get('role') != 'user' or messages[-1]['content'] != record['question']:
        raise ValueError('最后一条用户消息和留存问题不一致')
    binary = (path.resolve().parent / record['image']).read_bytes()
    width, height = jpeg_size(binary)
    if (width, height) != (record['frame'].get('width'), record['frame'].get('height')):
        raise ValueError('图片实际尺寸与留存元数据不符')
    frame = {**record['frame'], 'data': base64.b64encode(binary).decode('ascii')}
    if current_reading:
        if visual_task(record['question']) != 'read_text':
            raise ValueError('当前规则替换只适用于独立读字请求，不重建未知的普通聊天历史')
        messages = build_messages([messages[-1]], record['question'], [], frame)
    else:
        messages[-1]['images'] = [frame['data']]
    return record, messages, integrity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='新建记录目录，拒绝覆盖')
    parser.add_argument('--current-reading', action='store_true', help='仅读字：显式替换为当前转写提示；默认保留原消息')
    parser.add_argument('--model', help='已安装的模型标签；默认使用留存记录的模型，不下载')
    parser.add_argument('--assets', type=Path, default=ROOT / 'assets')
    parser.add_argument('--models', type=Path, default=ROOT / '.runtime/models')
    parser.add_argument('--model-port', type=int, default=11438)
    args = parser.parse_args()
    record, messages, integrity = load_input(args.record, args.current_reading)
    args.output.mkdir(parents=True, exist_ok=False)
    model_name = args.model or record['model']
    runtime = ModelRuntime(args.assets, args.output / 'runtime', args.model_port,
                           models=args.models, model=model_name)
    result = {
        'boundary': '实际留存消息与原图的模型回放；非实时取图、ASR、TTS、播放或实机验收',
        'record': str(args.record.resolve()), 'model': model_name,
        'prompt_mode': 'current_isolated_reading' if args.current_reading else 'recorded_messages',
        'history_note': '读取原始消息；选择当前读字时按已确认规则去掉历史和个人偏好',
        'integrity': integrity, 'question': record['question'], 'answer': '',
        'messages': [{k: v for k, v in message.items() if k != 'images'} for message in messages],
        'sampling_note': '使用当前LocalModel适配参数；原留存未记录随机种子，不能宣称逐字确定性重现',
        'hardware_pass': False,
    }
    try:
        # 拒绝已占用端口，不接管演示服务，也不改变其他进程的驻留模型。
        result['runtime'] = runtime.start()
        model = LocalModel(f'http://127.0.0.1:{args.model_port}', model_name)
        model.warm()
        turn = Turn('recorded-frame-replay', 0, 0)
        started = time.monotonic()
        for chunk in model.stream(messages, turn):
            if chunk and 'first_text_seconds' not in result:
                result['first_text_seconds'] = round(time.monotonic() - started, 3)
            result['answer'] += chunk
        result['generation_seconds'] = round(time.monotonic() - started, 3)
        result['backend_metrics'] = turn.model_metrics
        result['backend_duration_unit'] = 'nanoseconds'
        result['state'] = 'generated'
    except Exception as error:
        result.update(state='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        runtime.close()
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: result[key] for key in (
        'state', 'model', 'prompt_mode', 'answer', 'first_text_seconds', 'generation_seconds', 'backend_metrics'
    )}, ensure_ascii=False))
    print(str(args.output / 'result.json'))


if __name__ == '__main__':
    main()
