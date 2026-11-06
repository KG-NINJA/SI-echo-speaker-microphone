"""Small wrapper for the installed, official Voicemeeter Remote API."""

import ctypes as ct
import json
import os
import subprocess
import time
from pathlib import Path

VM_DIR = Path(os.environ.get("VOICEMEETER_DIR", r"C:\Program Files (x86)\VB\Voicemeeter"))
ROOT = Path(__file__).resolve().parent.parent


class VoiceMeeter:
    def __init__(self, launch=False):
        self.dll = ct.WinDLL(str(VM_DIR / "VoicemeeterRemote64.dll"))
        signatures = {
            "VBVMR_GetParameterFloat": [ct.c_char_p, ct.POINTER(ct.c_float)],
            "VBVMR_SetParameterFloat": [ct.c_char_p, ct.c_float],
            "VBVMR_GetParameterStringW": [ct.c_char_p, ct.c_wchar_p],
            "VBVMR_GetVoicemeeterType": [ct.POINTER(ct.c_long)],
        }
        for name, args in signatures.items():
            getattr(self.dll, name).argtypes = args
            getattr(self.dll, name).restype = ct.c_long
        result = self.dll.VBVMR_Login()
        if result < 0:
            raise RuntimeError(f"Voicemeeter login failed: {result}")
        self.connected = True
        try:
            if result == 1 and launch:
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                subprocess.Popen([str(VM_DIR / "voicemeeterpro_x64.exe")], startupinfo=startup)
            deadline = time.monotonic() + 5
            while self.dll.VBVMR_IsParametersDirty() < 0:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Voicemeeter Banana is not running")
                time.sleep(0.1)
            kind = ct.c_long()
            self.check(self.dll.VBVMR_GetVoicemeeterType(ct.byref(kind)))
            if kind.value != 2:
                raise RuntimeError("Expected Voicemeeter Banana")
        except Exception:
            self.close()
            raise

    @staticmethod
    def check(code):
        if code != 0:
            raise RuntimeError(f"Voicemeeter API error {code}")

    def get(self, name):
        self.dll.VBVMR_IsParametersDirty()
        value = ct.c_float()
        self.check(self.dll.VBVMR_GetParameterFloat(name.encode(), ct.byref(value)))
        return value.value

    def text(self, name):
        self.dll.VBVMR_IsParametersDirty()
        value = ct.create_unicode_buffer(512)
        self.check(self.dll.VBVMR_GetParameterStringW(name.encode(), value))
        return value.value

    def set(self, name, value):
        self.check(self.dll.VBVMR_SetParameterFloat(name.encode(), float(value)))
        deadline = time.monotonic() + 2
        while abs(self.get(name) - value) > 0.01:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Voicemeeter setting did not apply: {name}")
            time.sleep(0.03)

    def close(self):
        if self.connected:
            self.dll.VBVMR_Logout()
            self.connected = False


def routing():
    """Virtual speaker goes only to B2; virtual mic goes only to B1."""
    result = {}
    for strip, target in ((3, "B2"), (4, "B1")):
        for bus in ("A1", "A2", "A3", "B1", "B2"):
            result[f"Strip[{strip}].{bus}"] = int(bus == target)
        result[f"Strip[{strip}].Gain"] = 0
        result[f"Strip[{strip}].Mute"] = 0
        result[f"Strip[{strip}].Solo"] = 0
    for bus_index in (3, 4):
        result[f"Bus[{bus_index}].Gain"] = 0
        result[f"Bus[{bus_index}].Mute"] = 0
    return result


def configure():
    vm = VoiceMeeter()
    try:
        settings = routing()
        previous = {key: vm.get(key) for key in settings}
        backup = ROOT / "voicemeeter-before-echo.json"
        if not backup.exists():
            backup.write_text(json.dumps({
                "floats": previous,
                "A1": vm.text("Bus[0].device.name"),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            # Disable every route before enabling the two intentional routes.
            for key in settings:
                if key.rsplit(".", 1)[-1] in {"A1", "A2", "A3", "B1", "B2"}:
                    vm.set(key, 0)
            for key, value in settings.items():
                vm.set(key, value)
            return {key: vm.get(key) for key in settings}
        except Exception as original:
            rollback_errors = []
            for key, value in previous.items():
                try:
                    vm.set(key, value)
                except (RuntimeError, OSError) as exc:
                    rollback_errors.append(str(exc))
            if rollback_errors:
                raise RuntimeError(f"Configuration failed; rollback incomplete: {rollback_errors}") from original
            raise
    finally:
        vm.close()


if __name__ == "__main__":
    print(json.dumps(configure(), indent=2))
