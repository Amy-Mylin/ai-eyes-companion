"""按明确源码清单打包；模型、私有配置和运行记录始终留在包外。"""
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    'README.md', 'THIRD_PARTY.md', '.gitignore', '.gitattributes',
    'run_host.py', 'run_cm5.py', 'start_host.ps1', 'start_cm5.sh',
    'requirements-host.txt', 'requirements-cm5.txt', 'requirements-kokoro.txt', 'docs/MODELS.md',
)
GROUPS = (
    'eyes_companion/*.py', 'eyes_companion/*.ps1', 'cm5_client/*.py',
    'tests/*.py', 'tools/*.py', 'config/*.example.json',
    '.github/workflows/*.yml',
)
SECRET_PATTERNS = (
    re.compile(r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE).*?KEY-----'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b'),
)


def release_files():
    files = [ROOT / name for name in ROOT_FILES]
    for pattern in GROUPS:
        files.extend(ROOT.glob(pattern))
    # 若所有者以后选择开源协议，将正式许可证一起分发。
    if (ROOT / 'LICENSE').is_file():
        files.append(ROOT / 'LICENSE')
    selected = sorted(set(files))
    for path in selected:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f'源码文件缺失或路径不在项目内：{path.name}')
        if path.stat().st_size > 1_000_000:
            raise ValueError(f'异常大文件，拒绝自动发布：{path.name}')
        text = path.read_text(encoding='utf-8-sig')
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            # 只报告文件名，避免把疑似密钥再次输出。
            raise ValueError(f'检测到疑似密钥，需要人工检查：{path.name}')
    return selected


def main():
    files = release_files()
    manifest = {}
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    target = output / 'ai-eyes-companion-source.zip'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            manifest[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            info = zipfile.ZipInfo('ai-eyes-companion/' + name, date_time=(2026, 10, 31, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == '.sh' else 0o100644) << 16
            archive.writestr(info, data)
        archive.writestr('ai-eyes-companion/SOURCE_MANIFEST.json',
                         json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP校验失败')
        for name, expected in manifest.items():
            data = archive.read('ai-eyes-companion/' + name)
            if hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise RuntimeError('归档内容与源码摘要不一致')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{digest}  {target.name}\n', encoding='utf-8')
    (output / 'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(target), 'source_files': len(files),
                      'bytes': target.stat().st_size, 'sha256': digest,
                      'models_included': False, 'hardware_retested': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
