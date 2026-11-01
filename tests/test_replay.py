"""检查离线回放保留原消息的边界；人工JPEG头不代表实际图片识别通过。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.replay_captured_turn import load_input


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'sample.json'
        image = bytes.fromhex('ffd8 ffc0 000b 08 0010 0020 01 01 1100 ffd9')
        (self.path.parent / 'sample.jpg').write_bytes(image)
        self.record = {
            'turn_id': 'sample', 'image': 'sample.jpg', 'image_sha256': hashlib.sha256(image).hexdigest(),
            'boundary': '人工协议检查', 'question': '读一下这个包装上的文字', 'model': 'test-only',
            'frame': {'width': 32, 'height': 16}, 'state': 'generated', 'answer': '人工答案',
            'messages': [{'role': 'system', 'content': '原系统提示'},
                         {'role': 'assistant', 'content': '旧回答'},
                         {'role': 'user', 'content': '读一下这个包装上的文字'}],
        }
        self.save()

    def tearDown(self):
        self.directory.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.record), encoding='utf-8')

    def test_default_replay_preserves_recorded_history_and_attaches_image_only_to_current_user(self):
        _, messages, integrity = load_input(self.path)
        self.assertEqual(messages[:-1], self.record['messages'][:-1])
        self.assertEqual(messages[-1]['content'], self.record['question'])
        self.assertEqual(sum('images' in message for message in messages), 1)
        self.assertFalse(integrity['hardware_pass'])

    def test_current_reading_is_explicit_and_does_not_reintroduce_old_answers(self):
        _, messages, _ = load_input(self.path, current_reading=True)
        self.assertEqual(len(messages), 2)
        self.assertNotIn('旧回答', str(messages))
        self.assertIn('没有明确字段', messages[0]['content'])

    def test_mismatched_question_or_dimensions_are_not_silently_repaired(self):
        self.record['question'] = '不是原始问题'
        self.save()
        with self.assertRaises(ValueError):
            load_input(self.path)
        self.record['question'] = self.record['messages'][-1]['content']
        self.record['frame']['width'] = 100
        self.save()
        with self.assertRaises(ValueError):
            load_input(self.path)


if __name__ == '__main__':
    unittest.main()
