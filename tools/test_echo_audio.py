"""Safety boundaries and audio format regressions for the Echo USB bridge."""

import queue
import struct
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

import echo_audio as audio
import vm_audio
from vm_audio import routing


class AudioTests(unittest.TestCase):
    def test_same_boot_recovery_and_failed_recovery_retains_record(self):
        boot = "00000000-0000-0000-0000-000000000001"
        token = "00000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio, "boot_identity", return_value=boot), \
                patch.object(audio, "cleanup_session") as cleanup, \
                patch.object(audio, "adb_shell") as shell, \
                patch.object(audio, "mixer_state") as mixer:
            audio.save_session(token, boot, True)
            cleanup.side_effect = subprocess.TimeoutExpired("adb", 4)
            with self.assertRaises(subprocess.TimeoutExpired):
                audio.recover_session()
            self.assertTrue((Path(temp) / "echo-audio-recovery.json").exists())
            shell.assert_not_called()
            cleanup.side_effect = None
            audio.recover_session()
            cleanup.assert_called_with(token, same_boot=True)
            shell.assert_called_once_with("start mixer")
            mixer.assert_called_once_with("running")
            self.assertFalse((Path(temp) / "echo-audio-recovery.json").exists())

    def test_new_boot_recovery_does_not_kill_or_change_mixer(self):
        boot = "00000000-0000-0000-0000-000000000001"
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio, "boot_identity", return_value="other-boot"), \
                patch.object(audio, "adb_shell") as shell:
            audio.save_session("00000000000000000000000000000000", boot, True)
            audio.recover_session()
            commands = [call.args[0] for call in shell.call_args_list]
            self.assertEqual(len(commands), 1)
            self.assertNotIn("kill", commands[0])
            self.assertNotIn("start mixer", commands[0])

    def test_invalid_recovery_cannot_execute_commands(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio, "adb_shell") as shell:
            audio.save_session("../../arbitrary", "00000000-0000-0000-0000-000000000001", True)
            with self.assertRaises(ValueError):
                audio.recover_session()
            shell.assert_not_called()
            self.assertTrue((Path(temp) / "echo-audio-recovery.json").exists())

    def test_gui_stop_ignores_pending_probe(self):
        self.check_gui_pending("stop")

    def test_gui_stop_resume_ignores_old_probe(self):
        self.check_gui_pending("resume")

    def test_gui_close_waits_for_pending_probe(self):
        self.check_gui_pending("close")

    def check_gui_pending(self, action):
        future = Future()
        bridge = MagicMock(worker=None, state="停止中", error="")
        bridge.metrics = {"mic_peak": 0}
        buttons = {}
        def button(*args, **kwargs):
            buttons[kwargs["text"]] = kwargs["command"]
            return MagicMock()
        with patch.object(audio, "tk") as tk, patch.object(audio, "ttk") as ttk, \
                patch.object(audio, "Bridge", return_value=bridge) as factory, \
                patch.object(audio, "ThreadPoolExecutor") as pool:
            window = tk.Tk.return_value
            ttk.Button.side_effect = button
            pool.return_value.submit.return_value = future
            def mainloop():
                update = window.after.call_args.args[1]
                if action == "close":
                    window.protocol.call_args.args[1]()
                    update()
                    window.destroy.assert_not_called()
                else:
                    buttons["停止する"]()
                    if action == "resume":
                        buttons["接続する"]()
                future.set_result("00000000-0000-0000-0000-000000000001")
                update()
                if action == "close":
                    window.destroy.assert_called_once()
            window.mainloop.side_effect = mainloop
            audio._gui(auto=True)
            self.assertEqual(factory.call_count, 1)
            bridge.start.assert_not_called()
            pool.return_value.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_auto_wait_budget_and_new_boot(self):
        policy = audio.AutoReconnect(True)
        self.assertFalse(policy.reserve(None, 0))
        self.assertTrue(policy.reserve("boot-a", 0))
        self.assertFalse(policy.reserve("boot-a", 4))
        self.assertTrue(policy.reserve("boot-a", 5))
        self.assertFalse(policy.reserve(None, 10))
        self.assertFalse(policy.reserve("boot-a", 19))
        self.assertTrue(policy.reserve("boot-a", 20))
        self.assertFalse(policy.reserve("boot-a", 1000))
        self.assertFalse(policy.reserve(None, 1001))
        self.assertFalse(policy.reserve("boot-a", 1002))
        self.assertTrue(policy.reserve("boot-b", 1003))
        self.assertEqual(policy.attempts, 1)

    def test_auto_stop_and_explicit_resume(self):
        policy = audio.AutoReconnect(True)
        policy.reserve("boot-a", 0)
        generation = policy.generation
        policy.pause()
        self.assertNotEqual(policy.generation, generation)
        self.assertFalse(policy.reserve("boot-b", 100))
        policy.resume()
        self.assertTrue(policy.reserve("boot-a", 101))

    def test_auto_short_success_does_not_reset_budget(self):
        policy = audio.AutoReconnect(True)
        policy.reserve("boot-a", 0)
        policy.connected(1)
        policy.connected(30)
        self.assertEqual(policy.attempts, 1)
        policy.reserve(None, 31)
        policy.connected(32)
        policy.connected(61)
        self.assertEqual(policy.attempts, 1)
        policy.connected(62)
        self.assertEqual(policy.attempts, 0)

    def test_readiness_requires_boot_complete_and_valid_identity(self):
        boot = "00000000-0000-0000-0000-000000000001"
        for value in ("", boot + "\n0", "invalid\n1", boot + "\n1\nextra"):
            with patch.object(audio, "adb_shell", return_value=value):
                self.assertIsNone(audio.ready_boot_identity())
        with patch.object(audio, "adb_shell", return_value=boot + "\n1"):
            self.assertEqual(audio.ready_boot_identity(), boot)
        with patch.object(audio, "adb_shell", side_effect=subprocess.TimeoutExpired("adb", 4)):
            self.assertIsNone(audio.ready_boot_identity())

    def test_gui_duplicate_does_not_start_ui(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio.msvcrt, "locking", side_effect=OSError("already locked")), \
                patch.object(audio, "_gui") as ui:
            audio.gui(auto=True)
        ui.assert_not_called()

    def test_gui_duplicate_byte_read_denied_does_not_start_ui(self):
        locked = MagicMock()
        locked.__enter__.return_value.read.side_effect = PermissionError("byte locked")
        with patch("builtins.open", return_value=locked), patch.object(audio, "_gui") as ui:
            audio.gui(auto=True)
        ui.assert_not_called()

    def test_signed_packed_channels(self):
        values = [-8388608, -1, 0, 1, 8388607, 16, -16, 255, -255]
        packed = b"".join(n.to_bytes(3, "little", signed=True) for n in values)
        self.assertEqual(audio.unpack_capture(packed).tolist(), [values])
        with self.assertRaises(ValueError):
            audio.unpack_capture(packed[:-1])

    def test_latest_audio_and_silence(self):
        q = queue.Queue(maxsize=2)
        for value in (b"old!", b"1234", b"5678"):
            audio.latest_put(q, (0, value))
        buffer = audio.AudioBuffer()
        self.assertEqual(buffer.pull(q, 6), b"123456")
        self.assertEqual(buffer.pull(q, 4), b"78\0\0")
        audio.latest_put(q, (0, b"abcd"))
        self.assertEqual(buffer.pull(q, 4, muted=True), bytes(4))
        self.assertTrue(q.empty())

    def test_muted_audio_cannot_reappear(self):
        q = queue.Queue(maxsize=8)
        buffer = audio.AudioBuffer()
        audio.latest_put(q, (0, b"PREVIOUS_AUDIO"))
        buffer.pull(q, 4)
        for text in (b"MUTED_AUDIO", b"MORE_MUTED_AUDIO"):
            audio.latest_put(q, (1, text))
        self.assertEqual(buffer.pull(q, 4, muted=True, epoch=1), bytes(4))
        # Includes a late producer from the old generation.
        audio.latest_put(q, (1, b"LATE_OLD_AUDIO"))
        self.assertEqual(buffer.pull(q, 8, epoch=2), bytes(8))
        audio.latest_put(q, (2, b"NEW!"))
        self.assertEqual(buffer.pull(q, 4, epoch=2), b"NEW!")

    def test_wave_header_describes_48k_stereo_pcm(self):
        header = audio.wave_header()
        self.assertEqual(len(header), 44)
        self.assertEqual(struct.unpack_from("<HHI", header, 20), (1, 2, 48000))
        self.assertEqual(struct.unpack_from("<HH", header, 32), (4, 16))

    def test_routes_cannot_feed_themselves(self):
        routes = routing()
        for strip, target in ((3, "B2"), (4, "B1")):
            enabled = [bus for bus in ("A1", "A2", "A3", "B1", "B2")
                       if routes[f"Strip[{strip}].{bus}"]]
            self.assertEqual(enabled, [target])

    def test_wrong_device_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio, "adb_shell", return_value="wrong-model") as shell:
            bridge = audio.Bridge()
            bridge.run()
        self.assertEqual(bridge.state, "エラー")
        self.assertIn("Unexpected Echo model", bridge.error)
        shell.assert_called_once_with("getprop ro.product.device")

    def test_startup_failure_restores_mixer(self):
        self.run_startup_case()

    def test_close_failure_still_restores_mixer(self):
        self.run_startup_case(close_failure=True)

    def test_cancel_before_device_start_does_not_spawn(self):
        self.run_startup_case(cancel=True)

    def run_startup_case(self, close_failure=False, cancel=False):
        calls = []
        bridge = audio.Bridge()
        def shell(command, **kwargs):
            calls.append(command)
            if cancel and command.startswith("mkdir "):
                bridge.stop()
            if command == "getprop ro.product.device":
                return "biscuit_puffin"
            if command == "id":
                return "uid=0(root)"
            if command == "cat /proc/sys/kernel/random/boot_id":
                return "00000000-0000-0000-0000-000000000001"
            return ""
        backend = MagicMock()
        backend.get_host_api_info_by_type.return_value = {"index": 2}
        backend.get_device_count.return_value = 2
        backend.get_device_info_by_index.side_effect = [
            {"hostApi": 2, "name": audio.SPEAKER_ENDPOINT, "isLoopbackDevice": False,
             "maxInputChannels": 2, "maxOutputChannels": 0, "index": 1, "defaultSampleRate": 48000},
            {"hostApi": 2, "name": audio.MIC_ENDPOINT, "isLoopbackDevice": False,
             "maxInputChannels": 0, "maxOutputChannels": 2, "index": 2, "defaultSampleRate": 48000},
        ]
        if close_failure:
            backend.open.return_value.close.side_effect = RuntimeError("close test failure")
        vm = MagicMock()
        vm.get.side_effect = lambda key: routing()[key]
        with tempfile.TemporaryDirectory() as temp, patch.object(audio, "ROOT", Path(temp)), \
                patch.object(audio, "adb_shell", side_effect=shell), \
                patch.object(audio, "mixer_state"), \
                patch.object(audio, "VoiceMeeter", return_value=vm), \
                patch.object(audio.pa, "PyAudio", return_value=backend), \
                patch.object(audio.Bridge, "spawn", side_effect=RuntimeError("test failure")) as spawn:
            bridge.run()
        if cancel:
            spawn.assert_not_called()
            self.assertNotIn("stop mixer", calls)
        else:
            self.assertIn("stop mixer", calls)
            self.assertIn("start mixer", calls)
            self.assertIn("test failure", bridge.error)
        self.assertTrue(bridge.stop_event.is_set())

    def test_configuration_failure_restores_immediate_snapshot(self):
        original = {key: (0 if value else 1) for key, value in routing().items()}
        values = dict(original)
        vm = MagicMock()
        vm.get.side_effect = values.__getitem__
        vm.text.return_value = ""
        failure = [True]
        def setter(key, value):
            values[key] = value
            if key == "Strip[4].B1" and failure[0]:
                failure[0] = False
                raise RuntimeError("set failed after mutation")
        vm.set.side_effect = setter
        with tempfile.TemporaryDirectory() as temp, patch.object(vm_audio, "ROOT", Path(temp)), \
                patch.object(vm_audio, "VoiceMeeter", return_value=vm):
            # A previous backup is deliberately unrelated to the immediate snapshot.
            (Path(temp) / "voicemeeter-before-echo.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "set failed"):
                vm_audio.configure()
        self.assertEqual(values, original)
        vm.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
