"""Offline AEC format and failure-handling checks."""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import echo_audio
import numpy as np
from echo_aec import EchoAec


class AecTests(unittest.TestCase):
    def test_chunk_boundaries_do_not_pad_or_drop(self):
        samples = np.zeros((1600, 9), dtype=np.int32)
        samples[:, 6] = np.arange(1600) * 100
        whole = EchoAec().process(samples)
        aec = EchoAec()
        split = np.concatenate([aec.process(samples[i:i + 73]) for i in range(0, 1600, 73)])
        np.testing.assert_array_equal(whole, split)
        self.assertEqual(len(aec.pending), 0)
        self.assertEqual(len(aec.process(samples[:159])), 0)
        self.assertEqual(len(aec.pending), 159)

    def test_capture_stops_on_aec_failure_without_raw_fallback(self):
        bridge = echo_audio.Bridge()
        with patch.object(echo_audio, "EchoAec") as factory:
            factory.return_value.process.side_effect = RuntimeError("AEC failure")
            bridge.capture(SimpleNamespace(stdout=io.BytesIO(bytes(6912))))
        self.assertTrue(bridge.stop_event.is_set())
        self.assertEqual(bridge.error, "AEC failure")
        self.assertTrue(bridge.mic_queue.empty())

    def test_invalid_channel_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            EchoAec().process(np.zeros((160, 8), dtype=np.int32))


if __name__ == "__main__":
    unittest.main()
