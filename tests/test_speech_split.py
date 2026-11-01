"""用改写后的示例复现落单标点故障，不随源码分发真实对话。"""
import unicodedata
import unittest

from eyes_companion.session import split_speech


class SpeechSplitTests(unittest.TestCase):
    def split_stream(self, text):
        parts, buffer = [], ''
        for character in text:
            buffer += character
            emitted, buffer = split_speech(buffer, limit=24 if not parts else 60)
            parts.extend(emitted)
        emitted, _ = split_speech(buffer, final=True)
        return parts + emitted

    def test_real_answer_trailing_bracket_is_not_a_separate_audio_segment(self):
        text = '桌上有个白底蓝字的“示例”食品盒，上面有树木和山形图案。  \n（若需确认细节或用途，请告诉我具体想问什么。）'
        parts = self.split_stream(text)
        self.assertTrue(parts)
        self.assertTrue(all(any(unicodedata.category(char)[0] not in 'PZC' for char in part)
                            for part in parts))
        self.assertIn('请告诉我具体想问什么。', ''.join(parts))

    def test_dates_decimal_and_short_answers_are_not_dropped(self):
        text = '1956。2026-10-28。37.5℃。是。'
        self.assertEqual(''.join(self.split_stream(text)), text)

    def test_only_separators_do_not_create_audio(self):
        parts, remaining = split_speech(' \n）。”！…', final=True)
        self.assertEqual(parts, [])
        self.assertEqual(remaining, '')
