"""验证发布适配中的安全退出和诊断文件边界，不访问真实设备。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cm5_client.thermal import ThermalGuard
from tools.inspect_capture import inspect_record


class PackagingTests(unittest.TestCase):
    def test_thermal_default_stops_capture_without_system_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            stops = []
            guard = ThermalGuard(folder, lambda: stops.append(True))
            with patch('cm5_client.thermal.subprocess.run') as run:
                guard._trip(76, shutdown=True)
                run.assert_not_called()
            self.assertEqual(stops, [True])
            self.assertEqual(guard.last_status, 'shutdown_disabled')

    def test_capture_rejects_escape_and_modified_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / 'sample.jpg'
            image.write_bytes(b'synthetic-fixture')
            record = {'image': '../sample.jpg', 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                      'messages': [{'role': 'user', 'content': 'test'}]}
            path = root / 'sample.json'
            path.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(ValueError):
                inspect_record(path)
            record['image'] = 'sample.jpg'
            path.write_text(json.dumps(record), encoding='utf-8')
            image.write_bytes(b'changed-fixture')
            with self.assertRaises(ValueError):
                inspect_record(path)
