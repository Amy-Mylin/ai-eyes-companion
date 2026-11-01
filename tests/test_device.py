"""设备侧控制边界回归；不启动arecord、相机或扬声器。"""

import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from cm5_client.io import Api, Camera, JpegStream, Player
from cm5_client.device import Device
from eyes_companion.memory import MemoryStore
from eyes_companion.session import Session


def short_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    return output.getvalue()


class HeldPlayback:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.returncode = None

    def communicate(self, **kwargs):
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("test playback did not release")
        if self.returncode is None:
            self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.release.set()

    def wait(self, **kwargs):
        return self.returncode


class DeviceTests(unittest.TestCase):
    def test_camera_reuses_profile_and_releases_old_stream_before_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = HeldPlayback(), HeldPlayback()
            with patch("cm5_client.io.subprocess.Popen", side_effect=[first, second]) as spawn, \
                    patch.object(Camera, "_read"):
                camera = Camera(Path(directory))
                try:
                    camera.start("general")
                    camera.start("general")
                    self.assertEqual(spawn.call_count, 1)
                    camera.start("text")
                    self.assertEqual(spawn.call_count, 2)
                    self.assertEqual(first.returncode, -15)
                    args = spawn.call_args.args[0]
                    self.assertEqual(args[args.index("--width") + 1], "1920")
                    self.assertEqual(args[args.index("--height") + 1], "1440")
                    self.assertEqual(args[args.index("--framerate") + 1], "3")
                    self.assertEqual(camera.profile, "text")
                finally:
                    camera.stop()
                self.assertEqual(second.returncode, -15)

    def test_local_cue_bypasses_stalled_download_and_has_no_answer_receipt(self):
        downloading, release_download = threading.Event(), threading.Event()
        wav = short_wav()
        calls, receipts, failures = [], [], []

        class ApiDouble:
            def request(self, path, **kwargs):
                calls.append(path)
                downloading.set()
                if not release_download.wait(3):
                    raise TimeoutError("test download did not release")
                return wav

        process = HeldPlayback()
        with patch("cm5_client.io.subprocess.Popen", return_value=process) as spawn:
            player = Player(ApiDouble(), "session", "device", lambda *row: receipts.append(row), failures.append)
            try:
                player.put({"clip_id": "old-download"})
                self.assertTrue(downloading.wait(1))
                player.stop()
                self.assertTrue(player.put_local("wake", wav, speech=False))
                self.assertTrue(process.started.wait(1))
                self.assertTrue(player.playing.is_set())
                self.assertFalse(player.speech_playing.is_set())
                self.assertFalse(release_download.is_set())
                player.stop()
                self.assertTrue(player.wait_idle(1))
            finally:
                release_download.set()
                player.close()
            spawn.assert_called_once()
        self.assertEqual(calls, ["/audio"])
        self.assertEqual(receipts, [])
        self.assertEqual(failures, [])

    def test_acknowledged_local_stop_preserves_cue_but_new_remote_stop_cancels_it(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.db")
            session = Session(None, None, memory)
            session.ready = True

            class ApiDouble:
                def __init__(self):
                    self.stop_cursor = None

                def request(self, path, data=None, query=None, **kwargs):
                    if path == "/connect":
                        return session.connect(data["client_id"], data["mode"])
                    if path == "/interrupt":
                        reply = session.interrupt(data["session_id"], end=data["end"])
                        self.stop_cursor = session.events.sequence
                        return reply
                    if path == "/events":
                        return session.poll(query["session_id"], query["after"], timeout=0.05)
                    if path == "/disconnect":
                        return session.disconnect(data["session_id"])
                    raise AssertionError(path)

            transport = ApiDouble()
            process = HeldPlayback()
            device = None
            try:
                with patch("cm5_client.io.subprocess.Popen", return_value=process):
                    device = Device(transport, Path(directory), "test-device", cues={"wake": short_wav()})
                    device.interrupt()
                    device.notice("wake")
                    self.assertTrue(process.started.wait(1))
                    deadline = time.monotonic() + 1
                    while (transport.stop_cursor is None or device.cursor < transport.stop_cursor) and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertIsNotNone(transport.stop_cursor)
                    self.assertGreaterEqual(device.cursor, transport.stop_cursor)
                    self.assertIsNone(process.poll())
                    session.interrupt(device.session_id)
                    self.assertTrue(process.release.wait(1))
                    self.assertEqual(process.returncode, -15)
                    device.close()
                    self.assertTrue(all(not worker.is_alive() for worker in device.threads))
            finally:
                if device:
                    device.close()
                session.close()
                memory.close()

    def test_local_api_rejects_cloud_and_credentials(self):
        for url in ("https://example.com", "http://8.8.8.8", "http://x:y@127.0.0.1:4318"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Api(url, "test-only-token")
        self.assertEqual(Api("http://192.168.7.1:4318", "test").base_url, "http://192.168.7.1:4318")

    def test_fragmented_frames_do_not_replay_old_data(self):
        parser = JpegStream()
        first, second = b"\xff\xd8frame-one\xff\xd9", b"\xff\xd8frame-two\xff\xd9"
        self.assertEqual(parser.feed(b"noise" + first[:1]), [])
        self.assertEqual(parser.feed(first[1:] + second[:5]), [first])
        self.assertEqual(parser.feed(second[5:]), [second])
        self.assertEqual(parser.feed(b""), [])

    def test_interruption_during_download_prevents_player_launch(self):
        waiting, release = threading.Event(), threading.Event()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(3200))

        class DelayedApi:
            def request(self, *args, **kwargs):
                waiting.set()
                if not release.wait(2):
                    raise TimeoutError("test download did not release")
                return output.getvalue()

        receipts, failures = [], []
        with patch("cm5_client.io.subprocess.Popen") as spawn:
            player = Player(DelayedApi(), "test-session", "test-device",
                            lambda *item: receipts.append(item), failures.append)
            try:
                player.put({"clip_id": "old-clip"})
                self.assertTrue(waiting.wait(1))
                player.stop()
                release.set()
            finally:
                player.close()
            spawn.assert_not_called()
        self.assertEqual(receipts, [])
        self.assertEqual(failures, [])

    def test_frame_buffer_overflow_is_explicit(self):
        parser = JpegStream()
        with self.assertRaises(ValueError):
            parser.feed(b"\xff\xd8" + bytes(4_000_000))
        self.assertEqual(len(parser.buffer), 0)

    def test_prefetch_overlaps_playback_and_obeys_interruption(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(3200))

        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                first_started = threading.Event()
                release_first = threading.Event()
                second_downloaded = threading.Event()
                completed = threading.Event()
                receipts, failures = [], []

                class PrefetchApi:
                    def request(self, path, query, **kwargs):
                        if query["clip_id"] == "second":
                            second_downloaded.set()
                        return output.getvalue()

                class Playback:
                    def __init__(self, first):
                        self.first = first
                        self.returncode = None

                    def communicate(self, **kwargs):
                        if self.first:
                            first_started.set()
                            if not release_first.wait(2):
                                raise TimeoutError("test playback did not release")
                        if self.returncode is None:
                            self.returncode = 0

                    def poll(self):
                        return self.returncode

                    def terminate(self):
                        self.returncode = -15
                        release_first.set()

                    def wait(self, **kwargs):
                        return self.returncode

                def report(clip_id, state):
                    receipts.append((clip_id, state))
                    if (clip_id, state) == ("second", "completed"):
                        completed.set()

                processes = []

                def spawn(*args, **kwargs):
                    process = Playback(first=not processes)
                    processes.append(process)
                    return process

                with patch("cm5_client.io.subprocess.Popen", side_effect=spawn):
                    player = Player(PrefetchApi(), "session", "device", report, failures.append)
                    try:
                        player.put({"clip_id": "first"})
                        self.assertTrue(first_started.wait(1))
                        player.put({"clip_id": "second"})
                        self.assertTrue(second_downloaded.wait(1))
                        deadline = time.monotonic() + 1
                        while player.ready.empty() and time.monotonic() < deadline:
                            time.sleep(0.005)
                        self.assertFalse(player.ready.empty())
                        self.assertFalse(release_first.is_set())
                        self.assertTrue(player.pending())
                        if interrupted:
                            player.stop()
                        else:
                            release_first.set()
                            self.assertTrue(completed.wait(1))
                    finally:
                        release_first.set()
                        player.close()
                    self.assertFalse(player.pending())
                    self.assertFalse(player.fetcher.is_alive())
                    self.assertFalse(player.thread.is_alive())
                self.assertEqual(failures, [])
                if interrupted:
                    self.assertEqual(len(processes), 1)
                    self.assertEqual(receipts, [("first", "started"), ("first", "interrupted")])
                else:
                    self.assertEqual(receipts, [("first", "started"), ("first", "completed"),
                                                ("second", "started"), ("second", "completed")])


if __name__ == "__main__":
    unittest.main()
