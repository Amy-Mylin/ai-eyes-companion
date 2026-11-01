"""打包时补齐的回放完整性检查，只读取指定记录及同目录图片。"""
import hashlib
import json
from pathlib import Path


def inspect_record(path):
    path = Path(path).resolve()
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(record, dict):
        raise ValueError('回放记录必须是JSON对象')
    name = record.get('image')
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name:
        raise ValueError('图片必须是记录同目录下的文件名')
    image = (path.parent / name).resolve()
    if image.parent != path.parent or image.suffix.lower() not in ('.jpg', '.jpeg'):
        raise ValueError('图片路径超出记录目录或格式不受支持')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if digest != record.get('image_sha256'):
        raise ValueError('图片摘要与记录不一致')
    messages = record.get('messages')
    if not isinstance(messages, list) or not messages:
        raise ValueError('记录缺少原始消息')
    if any(not isinstance(m, dict) or not isinstance(m.get('content'), str) or 'images' in m for m in messages):
        raise ValueError('消息格式不受支持或包含额外图像')
    return {'image_sha256': digest, 'integrity_checked': True, 'hardware_pass': False}
