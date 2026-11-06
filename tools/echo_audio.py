"""USB audio bridge for this Echo Dot. No cloud access or audio recording."""
# ruff: noqa: BLE001 -- task boundaries report every failure and still restore hardware.

import argparse
import audioop
import json
import msvcrt
import os
import queue
import shlex
import shutil
import struct
import subprocess
import threading
import time
import tkinter as tk
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tkinter import ttk

import numpy as np
import pyaudiowpatch as pa
from echo_aec import EchoAec
from vm_audio import VoiceMeeter, routing

ROOT = Path(__file__).resolve().parent.parent
ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"
SERIAL = os.environ.get("ECHO_DOT_SERIAL", "").strip()
MIC_ENDPOINT = "Voicemeeter AUX Input (VB-Audio Voicemeeter VAIO)"
SPEAKER_ENDPOINT = "Voicemeeter Out B2 (VB-Audio Voicemeeter VAIO)"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def adb_shell(command, timeout=4):
    if not SERIAL:
        raise RuntimeError("Set ECHO_DOT_SERIAL or pass --serial to select the Echo device")
    result = subprocess.run(
        [str(ADB), "-s", SERIAL, "shell", command],
        capture_output=True, timeout=timeout, check=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return result.stdout.decode(errors="replace").strip()


def mixer_state(expected):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if adb_shell("getprop init.svc.mixer", timeout=1) == expected:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Echo mixer state is not {expected}")


def boot_identity():
    return str(uuid.UUID(adb_shell("cat /proc/sys/kernel/random/boot_id")))


def save_session(token, boot_id, changed):
    path = ROOT / "echo-audio-recovery.json"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump({"serial": SERIAL, "token": token, "boot_id": boot_id,
                   "changed": changed}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def cleanup_session(token, same_boot=True):
    if uuid.UUID(token).hex != token:
        raise ValueError("Invalid Echo cleanup token")
    directory = f"/data/local/tmp/echo-pc-{token}"
    if same_boot:
        identity = ("[ \"$(cut -d ' ' -f22 /proc/$p/stat 2>/dev/null)\" = \"$stamp\" ] && "
            f"( tr '\\000' ' ' </proc/$p/cmdline 2>/dev/null | grep -F '{directory}' >/dev/null || "
            f"tr '\\000' '\\n' </proc/$p/environ 2>/dev/null | grep -Fx ECHO_PC_SESSION={token} >/dev/null )")
        scan = (f"for role in mic play feed reader receiver; do p=; stamp=; read p stamp <{directory}/$role.pid 2>/dev/null; "
            "case $p in ''|0|1|*[!0-9]*) continue;; esac; "
            "case $stamp in ''|*[!0-9]*) continue;; esac; "
            f"if {identity}; then ")
        adb_shell(scan + "kill -KILL $p; fi; done")
        adb_shell("for n in 1 2 3 4 5 6 7 8 9 10; do alive=0; " + scan
            + "alive=1; fi; done; [ $alive = 0 ] && exit 0; sleep .1; done; exit 21")
    adb_shell(f"if [ -d {directory} ]; then rm -f {directory}/mic.pipe {directory}/play.pipe "
        f"{directory}/mic.pid {directory}/play.pid {directory}/feed.pid "
        f"{directory}/reader.pid {directory}/receiver.pid {directory}/mic.log {directory}/play.log "
        f"&& rmdir {directory}; fi")


def recover_session():
    """Under the bridge lock, recover only the recorded session of this device."""
    path = ROOT / "echo-audio-recovery.json"
    if not path.exists():
        return
    if path.stat().st_size > 2048:
        raise ValueError("Invalid Echo recovery record")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("serial") != SERIAL
            or type(record.get("changed")) is not bool
            or not isinstance(record.get("token"), str)
            or not isinstance(record.get("boot_id"), str)):
        raise ValueError("Invalid Echo recovery record")
    token = record["token"]
    if uuid.UUID(token).hex != token:
        raise ValueError("Invalid Echo recovery token")
    old_boot = str(uuid.UUID(record["boot_id"]))
    same_boot = old_boot == boot_identity()
    cleanup_session(token, same_boot=same_boot)
    if same_boot and record["changed"]:
        adb_shell("start mixer")
        mixer_state("running")
    path.unlink()


def unpack_capture(raw):
    if len(raw) % 27:
        raise ValueError("Incomplete S24_3LE frame")
    v = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 9, 3).astype(np.int32)
    n = v[:, :, 0] | (v[:, :, 1] << 8) | (v[:, :, 2] << 16)
    return (n ^ 0x800000) - 0x800000


def latest_put(q, item):
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(item)


class AudioBuffer:
    def __init__(self):
        self.data = bytearray()
        self.epoch = -1

    def pull(self, q, size, muted=False, epoch=0):
        if epoch != self.epoch or muted:
            self.data.clear()
            self.epoch = epoch
        if muted:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    return bytes(size)
        while len(self.data) < size:
            try:
                generation, content = q.get_nowait()
                if generation == epoch:
                    self.data.extend(content)
            except queue.Empty:
                break
        out = bytes(self.data[:size])
        del self.data[:size]
        return out + bytes(size - len(out))


def wave_header():
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0x7FFFF024,
                       b"WAVE", b"fmt ", 16, 1, 2, 48000, 192000,
                       4, 16, b"data", 0x7FFFF000)


class Cancelled(Exception):
    pass


class Bridge:
    def __init__(self):
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.muted = threading.Event()
        self.mute_lock = threading.Lock()
        self.mic_epoch = 0
        self.mic_resume_at = 0.0
        self.volume = 0.35
        self.state = "停止中"
        self.error = ""
        self.metrics = {"mic_frames": 0, "speaker_frames": 0,
                        "mic_peak": 0.0, "reference_peak": 0.0,
                        "reference_max": 0.0, "speaker_source_peak_max": 0.0}
        self.processes = []
        self.threads = []
        self.mic_queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=8)
        self.speaker_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self.mic_buffer = AudioBuffer()
        self.last_mic = self.last_speaker = time.monotonic()
        self.worker: threading.Thread | None = None

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self.run)
        self.worker.start()

    def stop(self):
        self.stop_event.set()

    def set_muted(self, muted):
        with self.mute_lock:
            if muted != self.muted.is_set():
                self.mic_epoch += 1
                self.mic_resume_at = time.monotonic() + 0.2
                (self.muted.set if muted else self.muted.clear)()

    def mic_state(self):
        with self.mute_lock:
            return self.mic_epoch, self.muted.is_set()

    def check_cancel(self):
        if self.stop_event.is_set():
            raise Cancelled()

    def fail(self, error):
        if not self.stop_event.is_set():
            self.error = str(error)
        self.stop_event.set()

    def spawn(self, mode, command):
        self.check_cancel()
        process = subprocess.Popen(
            [str(ADB), "-s", SERIAL, mode, command],
            stdin=subprocess.PIPE if mode == "exec-in" else subprocess.DEVNULL,
            stdout=subprocess.PIPE if mode == "exec-out" else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, bufsize=0, creationflags=CREATE_NO_WINDOW,
        )
        self.processes.append(process)
        return process

    def capture(self, process):
        rate_state = None
        partial = bytearray()
        try:
            aec = EchoAec()
            while not self.stop_event.is_set():
                raw = process.stdout.read(6912 - len(partial))
                if not raw:
                    raise RuntimeError("Echo microphone USB connection ended")
                partial.extend(raw)
                if len(partial) != 6912:
                    continue
                epoch, muted = self.mic_state()
                samples = unpack_capture(partial)
                partial.clear()
                # Keep learning during mute; never fall back to raw capture.
                mono = aec.process(samples)
                self.metrics.update(aec_raw_energy=aec.raw_energy,
                    aec_clean_energy=aec.clean_energy,
                    aec_reference_energy=aec.reference_energy,
                    aec_reference_channels_energy=aec.reference_channels_energy,
                    aec_clipped_samples=aec.clipped_samples)
                self.metrics["mic_peak"] = float(np.max(np.abs(mono.astype(np.int32)))) / 32768
                self.metrics["reference_peak"] = float(np.max(np.abs(samples[:, 7:9]))) / 8388608
                self.metrics["reference_max"] = max(self.metrics["reference_max"], self.metrics["reference_peak"])
                self.metrics["mic_frames"] += len(mono)
                pcm, rate_state = audioop.ratecv(mono.tobytes(), 2, 1, 16000, 48000, rate_state)
                if (not muted and self.mic_state() == (epoch, False)
                        and time.monotonic() >= self.mic_resume_at):
                    latest_put(self.mic_queue, (epoch, audioop.tostereo(pcm, 2, 1, 1)))
                self.last_mic = time.monotonic()
        except Exception as exc:
            self.fail(exc)

    def playback(self, process):
        try:
            process.stdin.write(wave_header())
            while not self.stop_event.is_set():
                try:
                    raw = self.speaker_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                pcm = audioop.mul(raw, 2, self.volume)
                self.metrics["speaker_source_peak_max"] = max(
                    self.metrics["speaker_source_peak_max"], audioop.max(raw, 2) / 32768)
                view = memoryview(pcm)
                while view and not self.stop_event.is_set():
                    written = process.stdin.write(view)
                    if not written:
                        raise RuntimeError("Echo speaker USB write ended")
                    view = view[written:]
                self.metrics["speaker_frames"] += len(raw) // 4
                self.last_speaker = time.monotonic()
        except Exception as exc:
            self.fail(exc)

    def input_callback(self, data, frames, timing, status):
        if self.stop_event.is_set():
            return None, pa.paComplete
        latest_put(self.speaker_queue, data)
        return None, pa.paContinue

    def output_callback(self, data, frames, timing, status):
        stopped = self.stop_event.is_set()
        epoch, muted = self.mic_state()
        result = self.mic_buffer.pull(self.mic_queue, frames * 4, muted or stopped, epoch)
        return result, pa.paComplete if stopped else pa.paContinue

    def run(self):
        audio = None
        streams = []
        resources = ExitStack()
        token = uuid.uuid4().hex
        directory = f"/data/local/tmp/echo-pc-{token}"
        created = False
        try:
            self.state = "接続中"
            self.check_cancel()
            lock = resources.enter_context(open(ROOT / "echo-audio.lock", "a+b"))  # noqa: SIM115 -- ExitStack owns lifetime.
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            self.check_cancel()
            if adb_shell("getprop ro.product.device") != "biscuit_puffin":
                raise RuntimeError("Unexpected Echo model")
            if not adb_shell("id").startswith("uid=0(root)"):
                raise RuntimeError("Echo root ADB is unavailable")
            recover_session()
            boot_id = boot_identity()
            mixer_state("running")
            self.check_cancel()
            vm = VoiceMeeter(launch=True)
            try:
                for key, value in routing().items():
                    if abs(vm.get(key) - value) > 0.01:
                        raise RuntimeError(f"Voicemeeter routing changed: {key}")
            finally:
                vm.close()
            self.check_cancel()
            audio = pa.PyAudio()
            wasapi = audio.get_host_api_info_by_type(pa.paWASAPI)["index"]
            devices = [audio.get_device_info_by_index(i) for i in range(audio.get_device_count())]
            def endpoint(name, direction):
                found = [d for d in devices if d["hostApi"] == wasapi and d["name"] == name
                         and not d["isLoopbackDevice"] and d[direction] >= 2]
                if len(found) != 1 or int(found[0]["defaultSampleRate"]) != 48000:
                    raise RuntimeError(f"Expected one 48 kHz Windows endpoint: {name}")
                return int(found[0]["index"])
            speaker_index = endpoint(SPEAKER_ENDPOINT, "maxInputChannels")
            mic_index = endpoint(MIC_ENDPOINT, "maxOutputChannels")
            self.check_cancel()
            streams.append(audio.open(format=pa.paInt16, channels=2, rate=48000,
                input=True, input_device_index=speaker_index, frames_per_buffer=960,
                stream_callback=self.input_callback, start=False))
            self.check_cancel()
            streams.append(audio.open(format=pa.paInt16, channels=2, rate=48000,
                output=True, output_device_index=mic_index, frames_per_buffer=960,
                stream_callback=self.output_callback, start=False))
            self.check_cancel()
            save_session(token, boot_id, changed=False)
            created = True
            adb_shell(f"mkdir {directory}")
            self.check_cancel()
            adb_shell(f"mknod {directory}/mic.pipe p && mknod {directory}/play.pipe p")
            self.check_cancel()
            save_session(token, boot_id, changed=True)
            adb_shell("stop mixer")
            mixer_state("stopped")
            self.check_cancel()
            remember = ("remember() { echo \"$1 $(cut -d ' ' -f22 /proc/$1/stat 2>/dev/null)\" >\"$2\"; }; ")
            mic_command = (remember + f"export ECHO_PC_SESSION={token}; remember $$ {directory}/reader.pid; "
                f"tinycap {directory}/mic.pipe -D 0 -d 24 -c 9 -r 16000 "
                f"-b 24 -p 256 -n 4 -f >{directory}/mic.log 2>&1 & "
                f"remember $! {directory}/mic.pid; exec cat {directory}/mic.pipe")
            play_command = (remember + f"export ECHO_PC_SESSION={token}; remember $$ {directory}/receiver.pid; "
                f"exec 3<&0; cat <&3 >{directory}/play.pipe & feed=$!; remember $feed {directory}/feed.pid; "
                f"tinyplay {directory}/play.pipe -D 0 -d 23 >{directory}/play.log 2>&1 & "
                f"play=$!; remember $play {directory}/play.pid; wait $play; "
                "kill $feed 2>/dev/null; wait $feed 2>/dev/null")
            mic = self.spawn("exec-out", "sh -c " + shlex.quote(mic_command))
            speaker = self.spawn("exec-in", "sh -c " + shlex.quote(play_command))
            self.check_cancel()
            self.last_mic = self.last_speaker = time.monotonic()
            for target, process in ((self.capture, mic), (self.playback, speaker)):
                thread = threading.Thread(target=target, args=(process,), daemon=True)
                self.threads.append(thread)
                thread.start()
            for stream in streams:
                self.check_cancel()
                stream.start_stream()
            self.state = "接続済み"
            self.ready.set()
            while not self.stop_event.wait(0.2):
                if any(p.poll() is not None for p in self.processes):
                    raise RuntimeError("Echo USB audio process ended")
                if time.monotonic() - min(self.last_mic, self.last_speaker) > 5:
                    raise RuntimeError("Audio transfer stopped for 5 seconds")
        except Cancelled:
            pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.stop_event.set()
            self.state = "停止処理中"
            def attempt(action):
                try:
                    action()
                except Exception as exc:
                    self.error += f"; cleanup: {exc}"
            for process in self.processes:
                if process.poll() is None:
                    attempt(process.kill)
            for process in self.processes:
                attempt(lambda p=process: p.wait(timeout=2))
            for thread in self.threads:
                attempt(lambda t=thread: t.join(timeout=2))
            if created:
                try:
                    recover_session()
                except Exception as exc:
                    self.error = f"Echo recovery pending (reconnect USB): {exc}"
            for stream in streams:
                attempt(stream.close)
            if audio:
                attempt(audio.terminate)
            attempt(resources.close)
            self.state = "エラー" if self.error else "停止中"


class AutoReconnect:
    """Three starts per boot/failure episode; unavailable probes never reset it."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.identity = None
        self.attempts = 0
        self.next_attempt = 0.0
        self.connected_since = None
        self.generation = 0

    def pause(self):
        self.enabled = False
        self.generation += 1
        self.connected_since = None

    def resume(self):
        self.enabled = True
        self.generation += 1
        self.attempts = 0
        self.next_attempt = 0.0
        self.connected_since = None

    def reserve(self, identity, now):
        self.connected_since = None
        if not self.enabled or identity is None:
            return False
        if identity != self.identity:
            self.identity = identity
            self.attempts = 0
            self.next_attempt = 0.0
        if self.attempts >= 3 or now < self.next_attempt:
            return False
        self.attempts += 1
        self.next_attempt = now + (5, 15, 45)[self.attempts - 1]
        return True

    def connected(self, now):
        if self.connected_since is None:
            self.connected_since = now
        if now - self.connected_since >= 30:
            # A verified stable connection is new evidence for a later retry.
            self.attempts = 0
            self.next_attempt = 0.0


def ready_boot_identity():
    """Read only this serial; no device changes during the connection wait."""
    try:
        lines = adb_shell("cat /proc/sys/kernel/random/boot_id; getprop sys.boot_completed").splitlines()
        if len(lines) == 2 and lines[1].strip() == "1":
            return str(uuid.UUID(lines[0].strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def gui(autostart=False, auto=False, minimized=False):
    # Keep a separate GUI lock: the bridge lock is released while waiting for USB.
    with open(ROOT / "echo-audio-gui.lock", "a+b") as lock:
        try:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        _gui(autostart, auto, minimized)


def _gui(autostart=False, auto=False, minimized=False):
    window = tk.Tk()
    window.title("Echo Dot スピーカー・マイク")
    window.geometry("540x330")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Echo Dot USB オーディオ", font=("Yu Gothic UI", 16, "bold")).pack(anchor="w")
    status = tk.StringVar(value="停止中")
    ttk.Label(frame, textvariable=status, wraplength=490).pack(anchor="w", pady=(12, 8))
    ttk.Label(frame, text="アプリのスピーカー：Voicemeeter Input\nアプリのマイク：Voicemeeter Out B1").pack(anchor="w")
    meter = ttk.Progressbar(frame, maximum=1, length=480)
    meter.pack(pady=12)
    controls = ttk.Frame(frame)
    controls.pack(fill="x")
    current = [Bridge()]
    muted = tk.BooleanVar(value=False)
    volume = tk.DoubleVar(value=35)
    closing = [False]
    policy = AutoReconnect(auto)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="echo-usb-check")
    pending: list[Future[str | None] | None] = [None]
    pending_generation = [0]
    next_probe = [0.0]
    def start_bridge():
        if current[0].worker and current[0].worker.is_alive():
            return
        current[0] = Bridge()
        if muted.get():
            current[0].set_muted(True)
        current[0].volume = volume.get() / 100
        current[0].start()
    def start():
        if auto:
            policy.resume()
            next_probe[0] = 0.0
        else:
            start_bridge()
    def stop():
        policy.pause()
        current[0].stop()
    ttk.Button(controls, text="接続する", command=start).pack(side="left")
    ttk.Button(controls, text="停止する", command=stop).pack(side="left", padx=8)
    def toggle_mute():
        current[0].set_muted(muted.get())
    ttk.Checkbutton(controls, text="マイクをミュート", variable=muted, command=toggle_mute).pack(side="left")
    ttk.Label(frame, text="AEC3エコー除去 有効 · Echoの再生音量").pack(anchor="w", pady=(12, 0))
    ttk.Scale(frame, from_=0, to=100, variable=volume,
              command=lambda v: setattr(current[0], "volume", float(v) / 100)).pack(fill="x")
    def close():
        closing[0] = True
        stop()
    def update():
        bridge = current[0]
        now = time.monotonic()
        alive = bool(bridge.worker and bridge.worker.is_alive())
        if alive and bridge.state == "接続済み":
            policy.connected(now)
        else:
            policy.connected_since = None
        if pending[0] is not None and pending[0].done():
            identity = pending[0].result()
            pending[0] = None
            if (not closing[0] and not alive
                    and pending_generation[0] == policy.generation
                    and policy.reserve(identity, now)):
                start_bridge()
                bridge = current[0]
                alive = True
        if (policy.enabled and not closing[0] and not alive
                and pending[0] is None and now >= next_probe[0]):
            pending_generation[0] = policy.generation
            pending[0] = executor.submit(ready_boot_identity)
            next_probe[0] = now + 5
        display = bridge.state
        if policy.enabled and not alive and not closing[0]:
            display = ("自動接続を保留（3回）。接続するボタンで再試行"
                       if policy.attempts >= 3 else "USB自動接続待ち")
        status.set(display + (" — " + bridge.error if bridge.error else ""))
        meter["value"] = 0 if bridge.muted.is_set() or not alive else bridge.metrics["mic_peak"]
        if closing[0] and not alive and pending[0] is None:
            window.destroy()
            return
        window.after(200, update)
    window.protocol("WM_DELETE_WINDOW", close)
    if autostart and not auto:
        window.after(300, start)
    if minimized:
        window.iconify()
    update()
    try:
        window.mainloop()
    finally:
        stop()
        if current[0].worker:
            current[0].worker.join(timeout=30)
        executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default=SERIAL, help="ADB serial for the Echo Dot")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--auto", action="store_true", help="Wait for USB and reconnect with bounded retries")
    parser.add_argument("--minimized", action="store_true", help="Start the GUI minimized")
    parser.add_argument("--seconds", type=int, default=0, help="Bounded headless validation")
    args = parser.parse_args()
    if (not args.serial or len(args.serial) > 128
            or any(ch.isspace() for ch in args.serial)):
        parser.error("pass a valid device serial with --serial or ECHO_DOT_SERIAL")
    SERIAL = args.serial
    if args.seconds:
        if not 1 <= args.seconds <= 600:
            parser.error("--seconds must be 1..600")
        bridge = Bridge()
        bridge.start()
        assert bridge.worker is not None
        startup_deadline = time.monotonic() + 12
        while not bridge.ready.is_set() and bridge.worker.is_alive() and time.monotonic() < startup_deadline:
            time.sleep(0.1)
        if not bridge.ready.is_set():
            bridge.fail("Audio startup did not complete")
        else:
            print("READY", flush=True)
        until = time.monotonic() + args.seconds
        while time.monotonic() < until and bridge.worker.is_alive():
            time.sleep(0.25)
        bridge.stop()
        bridge.worker.join(timeout=25)
        result = {"state": bridge.state, "error": bridge.error, **bridge.metrics}
        print(json.dumps(result, ensure_ascii=False))
        if bridge.worker.is_alive() or bridge.error:
            raise SystemExit(1)
    else:
        gui(args.start, args.auto, args.minimized)
