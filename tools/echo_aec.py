"""WebRTC AEC3 on the Echo's synchronized 16 kHz mic and render reference."""

import numpy as np
from pywebrtc_audio import AudioProcessor


class EchoAec:
    FRAME = 160
    POST_GAIN = 10 ** (24 / 20)
    PROCESS_GAIN = 16.0

    def __init__(self):
        self.processor = AudioProcessor(sample_rate=16000, num_channels=1,
            echo_cancellation=True, noise_suppression=False, auto_gain_control=False,
            stream_delay_ms=0)
        self.pending = np.empty((0, 9), dtype=np.int32)
        self.frames = 0
        self.raw_energy = 0.0
        self.clean_energy = 0.0
        self.reference_energy = 0.0
        self.reference_channels_energy = 0.0
        self.clipped_samples = 0

    def process(self, samples):
        if samples.ndim != 2 or samples.shape[1] != 9 or samples.dtype != np.int32:
            raise ValueError("Expected packed Echo channels decoded as int32 N x 9")
        self.pending = np.concatenate((self.pending, samples), axis=0)
        count = (len(self.pending) // self.FRAME) * self.FRAME
        chunks = []
        for offset in range(0, count, self.FRAME):
            block = self.pending[offset:offset + self.FRAME]
            # Copy strided columns: the native binding requires contiguous arrays.
            near = np.ascontiguousarray(block[:, 6] / 8388608, dtype=np.float32)
            far = np.ascontiguousarray(block[:, 7:9].mean(axis=1) / 8388608, dtype=np.float32)
            # Echo raw PCM is quiet. Match WebRTC's speech operating level,
            # then undo this processing gain before applying the existing gain.
            self.clipped_samples += int(np.count_nonzero(np.abs(near) * self.PROCESS_GAIN > 1))
            clean = self.processor.process(
                np.clip(near * self.PROCESS_GAIN, -1, 1),
                far) / self.PROCESS_GAIN
            if clean.shape != near.shape or not np.all(np.isfinite(clean)):
                raise RuntimeError("AEC returned invalid audio")
            self.frames += len(near)
            self.raw_energy += float(np.dot(near, near))
            self.clean_energy += float(np.dot(clean, clean))
            self.reference_energy += float(np.dot(far, far))
            refs = block[:, 7:9].astype(np.float64) / 8388608
            self.reference_channels_energy += float(np.sum(refs ** 2) / 2)
            chunks.append(np.clip(clean * (32768 * self.POST_GAIN), -32768, 32767).astype("<i2"))
        self.pending = self.pending[count:].copy()
        return np.concatenate(chunks) if chunks else np.empty(0, dtype="<i2")
