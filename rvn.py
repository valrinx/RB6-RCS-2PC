"""
# RVN — Recoil Control System  v8.3
Changes from v8.2 (v8.3):
  • OPT: Macro hotkey listener no longer reads macros.json from disk on every
    tick — macros are now cached in memory (_macros_cache) and only written/read
    from disk when they actually change.  Eliminates ~60 disk reads/second that
    could cause hitches on spinning HDDs or network shares.
  • NEW: Macro Record Key — assign any F-key or navigation key (INS, DEL, HOME,
    END, PGUP, PGDN) as a hardware record toggle.  Press the key anywhere to
    start recording; press again to stop and auto-save with a timestamp name.
    Configure via the "Record Hotkey" row in the Macros tab.
  • OPT: getStatus() polling made adaptive — 800ms while tab is visible,
    completely paused when the browser tab is hidden (e.g. alt-tabbed).
    Previously polled at a fixed 1 s regardless of visibility.
  • OPT: Macro list poll is tab-aware — refreshes at 1200ms on the Macros tab
    and slows to 4000ms on other tabs.
  • OPT: Recording step-count poll interval tightened to 200ms and self-cancels
    when recording is detected as stopped.
  • OPT: Hotkey listener sleep reduced from 16ms to 10ms (~100Hz) — lower
    average key-press latency with negligible CPU impact.
"""

import threading
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
import sys, json, os, socket, time, random, shutil, zipfile, io
from contextlib import asynccontextmanager
from pathlib import Path

# ── Optional platform libs ────────────────────────────────────────────────────
try:
    import ctypes
    _HAS_CTYPES = True
except ImportError:
    _HAS_CTYPES = False

try:
    from mouse.makcu import makcu_controller as _makcu
    _HAS_MAKCU = True
except Exception:
    _HAS_MAKCU = False
    class _MakcuStub:
        def is_connected(self): return False
        def connect(self): return False
        def disconnect(self): pass
        def StartButtonListener(self): pass
        def get_button_state(self, b): return False
        def simple_move_mouse(self, x, y): pass
        def click_lmb(self): pass
    _makcu = _MakcuStub()

makcu_controller = _makcu


# ══════════════════════════════════════════════════════════════════════════════
#  SoftwareController
# ══════════════════════════════════════════════════════════════════════════════
class SoftwareController:
    _VK = {"LMB": 0x01, "RMB": 0x02, "MMB": 0x04, "M4": 0x05, "M5": 0x06}
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP   = 0x0004

    def __init__(self):
        self._user32 = None
        self._ready  = False
        self._btn: dict = {}
        self._lock   = threading.Lock()
        self._connected = False
        self._poll_thread: threading.Thread | None = None

        if _HAS_CTYPES:
            try:
                self._user32 = ctypes.windll.user32
                self._ready  = True
            except AttributeError:
                pass

        if self._ready:
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))
                ]
            class _U(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT)]
            class INPUT(ctypes.Structure):
                _anonymous_ = ("_u",)
                _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

            self._MOUSEINPUT = MOUSEINPUT
            self._INPUT      = INPUT
            self._INPUT_sz   = ctypes.sizeof(INPUT)

    def is_connected(self):   return self._connected
    def connect(self):
        self._connected = True
        self.StartButtonListener()
        return True
    def disconnect(self):     self._connected = False

    def StartButtonListener(self):
        if not self._ready: return
        if self._poll_thread and self._poll_thread.is_alive(): return
        def _poll():
            while self._connected:
                with self._lock:
                    for n, vk in self._VK.items():
                        self._btn[n] = bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
                time.sleep(0.005)
        self._poll_thread = threading.Thread(target=_poll, daemon=True)
        self._poll_thread.start()

    def get_button_state(self, btn):
        with self._lock: return self._btn.get(btn, False)

    def get_physical_lmb(self):
        """Read LMB via GetAsyncKeyState directly — bypasses cache.
        Used by the RF worker to detect physical hold/release
        even while RF is sending synthetic LEFTUP events."""
        if not self._ready: return False
        try:
            return bool(self._user32.GetAsyncKeyState(self._VK["LMB"]) & 0x8000)
        except Exception:
            return False

    def simple_move_mouse(self, x, y):
        if not self._ready or (x == 0 and y == 0): return
        try:
            inp = self._INPUT(type=0)
            inp.mi = self._MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=0x0001, time=0, dwExtraInfo=None)
            self._user32.SendInput(1, ctypes.byref(inp), self._INPUT_sz)
        except Exception as e:
            print(f"[Software] move error: {e}")

    def _send_lmb(self, flag):
        if not self._ready: return
        try:
            inp = self._INPUT(type=0)
            inp.mi = self._MOUSEINPUT(dx=0, dy=0, mouseData=0,
                dwFlags=flag, time=0, dwExtraInfo=None)
            self._user32.SendInput(1, ctypes.byref(inp), self._INPUT_sz)
        except Exception as e:
            print(f"[Software] lmb error: {e}")

    def lmb_down(self): self._send_lmb(self.MOUSEEVENTF_LEFTDOWN)
    def lmb_up(self):   self._send_lmb(self.MOUSEEVENTF_LEFTUP)

    def click_lmb(self):
        """DOWN → 10ms → UP  (only outside RF path)"""
        self.lmb_down()
        time.sleep(0.010)
        self.lmb_up()


# ══════════════════════════════════════════════════════════════════════════════
#  KMBoxController  — KMBox Net UDP Protocol
#
#  Packet format (64 bytes total):
#    [0:4]   mac       = UUID bytes (4 bytes, little-endian hex)
#    [4:8]   rand      = random uint32 (set once at connect)
#    [8:12]  indexpts  = sequence number uint32
#    [12:16] cmd       = command uint32
#    [16:64] payload   = command-specific data (48 bytes, zero-padded)
#
#  Commands:
#    0x000E  CONNECT   payload: 4 zero bytes
#    0x0001  MOVE      payload: struct hhhB (x, y, wheel, 0)
#    0x0004  LEFTCLICK payload: struct BB (state, 0)  state: 1=down 0=up
#    0x0100  MONITOR   payload: struct I (button_mask)
#               response[16:20] = current button state bitmask
#
#  Button mask: LMB=1 RMB=2 MMB=4 M4=8 M5=16
# ══════════════════════════════════════════════════════════════════════════════
import struct as _struct

class KMBoxController:
    CMD_CONNECT = 0x000E
    CMD_MOVE    = 0x0001
    CMD_CLICK   = 0x0004
    CMD_MONITOR = 0x0100
    _BTN        = {"LMB": 1, "RMB": 2, "MMB": 4, "M4": 8, "M5": 16}

    def __init__(self):
        self._sock            = None
        self._connected       = False
        self._ip              = ""
        self._port            = 57856
        self._mac             = b'\x00'*4
        self._rand            = 0
        self._seq             = 0
        self._send_lock       = threading.Lock()  # sending packets (move/click)
        self._seq_lock        = threading.Lock()  # sequence counter
        self._btn_lock        = threading.Lock()  # btn_cache
        self._click_lock      = threading.Lock()  # click_lmb only — avoids blocking monitor
        self._btn_cache: dict = {k: False for k in self._BTN}
        self._monitor_thread: threading.Thread | None = None
        self._last_err        = 0.0

    def is_connected(self): return self._connected

    def connect(self):
        cfg  = app_state.get_kmbox_config()
        ip   = cfg["ip"].strip()
        port = int(cfg["port"]) if cfg["port"] else 57856
        uuid = cfg["uuid"].replace("-","").replace(" ","").upper()

        if len(uuid) < 8:
            print("[KMBox] Invalid UUID — need 8 hex chars e.g. F2083CAB")
            return False
        try:
            uuid_int  = int(uuid[:8], 16)
            self._mac = _struct.pack('<I', uuid_int)
        except ValueError:
            print(f"[KMBox] UUID is not valid hex: {uuid!r}")
            return False

        with self._send_lock:
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

        try:
            self._rand = random.randint(1, 0xFFFFFFFF)
            self._seq  = 0
            self._ip   = ip
            self._port = port

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)

            pkt = self._build(self.CMD_CONNECT, b'\x00'*4)
            print(f"[KMBox] Connecting {ip}:{port} uuid={uuid[:8]} mac={self._mac.hex()}")
            sock.sendto(pkt, (ip, port))

            try:
                resp, addr = sock.recvfrom(1024)
                print(f"[KMBox] Handshake OK — {len(resp)} bytes from {addr}: {resp.hex()[:24]}")
            except socket.timeout:
                sock.close()
                print(f"[KMBox] Timeout — device did not respond")
                print(f"[KMBox] Check: IP={ip}, Port={port}, UUID={uuid[:8]}")
                self._connected = False
                return False

            sock.settimeout(0.05)
            with self._send_lock:
                self._sock = sock
            self._connected = True
            print(f"[KMBox] Connected ✓ {ip}:{port}")
            self.StartButtonListener()
            return True

        except socket.timeout:
            print(f"[KMBox] Timeout — device did not respond IP:{ip} Port:{port}")
            self._connected = False
            return False
        except Exception as e:
            now = time.perf_counter()
            if now - self._last_err > 5:
                print(f"[KMBox] Connection failed: {e}")
                self._last_err = now
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False
        with self._send_lock:
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

    def StartButtonListener(self):
        if self._monitor_thread and self._monitor_thread.is_alive(): return
        def _poll():
            # FIX v8.1: Use a dedicated monitor socket so the monitor thread
            # never holds _send_lock during recvfrom.  Previously the monitor
            # held _send_lock for the entire send+recv round-trip (~8 ms),
            # blocking simple_move_mouse() on every recoil tick.
            ALL_MASK = 1 | 2 | 4 | 8 | 16  # LMB|RMB|MMB|M4|M5
            mon_sock = None
            try:
                mon_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                mon_sock.settimeout(0.05)
            except Exception:
                return
            try:
                while self._connected:
                    with self._send_lock:
                        if not self._sock:
                            break
                        ip, port = self._ip, self._port
                    try:
                        mon_sock.sendto(
                            self._build(self.CMD_MONITOR, _struct.pack('<I', ALL_MASK)),
                            (ip, port)
                        )
                        resp, _ = mon_sock.recvfrom(1024)
                        bitmask = _struct.unpack_from('<I', resp, 16)[0] if len(resp) >= 20 else 0
                    except socket.timeout:
                        bitmask = 0
                    except Exception:
                        self._connected = False
                        break
                    with self._btn_lock:
                        for btn, mask in self._BTN.items():
                            self._btn_cache[btn] = bool(bitmask & mask)
                    time.sleep(0.008)
            finally:
                try:
                    mon_sock.close()
                except Exception:
                    pass
        self._monitor_thread = threading.Thread(target=_poll, daemon=True, name="KMBox_Monitor")
        self._monitor_thread.start()

    def get_button_state(self, btn):
        with self._btn_lock: return self._btn_cache.get(btn, False)

    def simple_move_mouse(self, x, y):
        if not self._connected or (x == 0 and y == 0): return
        with self._send_lock:
            if not self._sock: return
            try:
                self._sock.sendto(
                    self._build(self.CMD_MOVE, _struct.pack('<hhhB', x, y, 0, 0)),
                    (self._ip, self._port)
                )
            except Exception as e:
                print(f"[KMBox] move error: {e}")
                self._connected = False

    def click_lmb(self):
        """LMB click — click_lock separate from send_lock so monitor thread is not blocked,
        so rapid-fire clicks do not wait ~8ms monitor poll every time."""
        if not self._connected: return
        with self._click_lock:
            if not self._sock: return
            try:
                # press LMB
                self._sock.sendto(
                    self._build(self.CMD_CLICK, _struct.pack('<BB', 1, 0)),
                    (self._ip, self._port)
                )
                time.sleep(0.008)
                # release LMB
                self._sock.sendto(
                    self._build(self.CMD_CLICK, _struct.pack('<BB', 0, 0)),
                    (self._ip, self._port)
                )
            except Exception as e:
                print(f"[KMBox] click error: {e}")
                self._connected = False

    def _build(self, cmd: int, payload: bytes) -> bytes:
        """Build 64-byte KMBox Net packet — thread-safe sequence counter."""
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        header = self._mac + _struct.pack('<III', self._rand, seq, cmd)
        body   = payload + b'\x00' * max(0, 48 - len(payload))
        return header + body  # 4 + 12 + 48 = 64 bytes

software_controller = SoftwareController()
kmbox_controller    = KMBoxController()


def get_active_controller():
    t = app_state.get_controller_type()
    if t == "kmbox":    return kmbox_controller
    if t == "software": return software_controller
    return makcu_controller


# ══════════════════════════════════════════════════════════════════════════════
#  Config helpers
# ══════════════════════════════════════════════════════════════════════════════
def _is_packaged_runtime() -> bool:
    """True for PyInstaller/cx_Freeze-style frozen and Nuitka-compiled runtime."""
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def _base_dir():
    override = os.environ.get("RVN_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    if _is_packaged_runtime():
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "RVN")
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _migrate_exe_configs_once():
    """If appdata configs/ is empty but exe-side configs/ has JSON, copy once."""
    if not _is_packaged_runtime():
        return
    # In onefile builds resources may be extracted to sys._MEIPASS (PyInstaller-style)
    # or live next to the executable. Try both.
    legacy_candidates = []
    try:
        legacy_candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "configs"))
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        legacy_candidates.append(os.path.join(os.path.abspath(meipass), "configs"))
    legacy_dir = next((d for d in legacy_candidates if d and os.path.isdir(d)), None)
    if not legacy_dir:
        return
    try:
        # Consider the target "empty" if it only contains default.json.
        # default.json is auto-created and should not block migration.
        existing_json = [
            f for f in os.listdir(CONFIG_DIR)
            if f.endswith(".json") and f != DEFAULT_CONFIG_FILE
        ]
        if existing_json:
            return
        legacy_json = [f for f in os.listdir(legacy_dir) if f.endswith(".json")]
        if not legacy_json:
            return
        for name in legacy_json:
            shutil.copy2(os.path.join(legacy_dir, name), os.path.join(CONFIG_DIR, name))
    except OSError:
        pass


_data_root = _base_dir()
CONFIG_DIR = os.path.join(_data_root, "configs")
os.makedirs(CONFIG_DIR, exist_ok=True)
_migrate_exe_configs_once()
DEFAULT_CONFIG_FILE = "default.json"
SETTINGS_FILE = os.path.join(_data_root, "settings.json")

def get_config_path(f):  return os.path.join(CONFIG_DIR, f)


# ══════════════════════════════════════════════════════════════════════════════
#  Persistent Settings  (v8.0)
# ══════════════════════════════════════════════════════════════════════════════
def load_settings() -> dict:
    """Load settings.json. Returns empty dict if missing or corrupt."""
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}

def save_settings(data: dict):
    """Atomically write settings.json."""
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        print(f"[SETTINGS] Write error: {e}")
        try: os.remove(tmp)
        except: pass


def _ensure_default_config_file():
    """Create default.json once if missing. Never overwrites — safe after exe rebuilds."""
    p = get_config_path(DEFAULT_CONFIG_FILE)
    if os.path.exists(p):
        return
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({}, fh, indent=4)
        os.replace(tmp, p)
    except OSError as e:
        print(f"[CONFIG] Could not create {DEFAULT_CONFIG_FILE}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


_ensure_default_config_file()

def read_configs(config_file=None):
    f = config_file or DEFAULT_CONFIG_FILE
    p = get_config_path(f)
    if not os.path.exists(p): return {}
    try:
        with open(p) as fh: return json.load(fh)
    except: return {}

def write_configs(configs, config_file=None):
    f   = config_file or DEFAULT_CONFIG_FILE
    p   = get_config_path(f)
    tmp = p + ".tmp"
    try:
        with open(tmp, 'w') as fh: json.dump(configs, fh, indent=4)
        os.replace(tmp, p)
    except OSError as e:
        print(f"[CONFIG] Write error: {e}")
        try: os.remove(tmp)
        except: pass

def list_config_files():
    try: return sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith('.json'))
    except: return []

def create_config_file(filename):
    if not filename.endswith('.json'): filename += '.json'
    fp = get_config_path(filename)
    if os.path.exists(fp): raise HTTPException(400, "Config file already exists.")
    try:
        with open(fp, 'w') as fh: json.dump({}, fh)
        return filename
    except OSError as e: raise HTTPException(500, str(e))

def delete_config_file(filename):
    if filename == DEFAULT_CONFIG_FILE: raise HTTPException(400, "Cannot delete default config.")
    fp = get_config_path(filename)
    if not os.path.exists(fp): raise HTTPException(404, "Not found.")
    try: os.remove(fp)
    except OSError as e: raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  Pydantic models
# ══════════════════════════════════════════════════════════════════════════════
VALID_TOGGLE_BTNS   = ["MMB", "M4", "M5"]
VALID_TRIGGER_MODES = ["LMB", "LMB+RMB"]
VALID_CONTROLLERS   = ["makcu", "kmbox", "software"]


class GunConfig(BaseModel):
    name:                     str  = Field(..., min_length=1, max_length=120)
    tags: Optional[Dict[str, str]] = None
    pull_down_value:          float = Field(default=0,    ge=0,    le=300)
    vertical_delay_ms:        int   = Field(default=0,    ge=0,    le=5000)
    vertical_duration_ms:     int   = Field(default=0,    ge=0,    le=10000)
    horizontal_value:         float = Field(default=0,    ge=-300, le=300)
    horizontal_delay_ms:      int   = Field(default=500,  ge=0,    le=5000)
    horizontal_duration_ms:   int   = Field(default=2000, ge=0,    le=10000)
    pull_down_curve:  Optional[List[float]] = None
    horizontal_curve: Optional[List[float]] = None
    # Hip fire overrides
    hip_pull_down:    Optional[float] = Field(default=None, ge=0, le=300)
    hip_horizontal:   Optional[float] = Field(default=None, ge=-300, le=300)


class ToggleButtonConfig(BaseModel):    button: str
class TriggerModeConfig(BaseModel):     mode: str
class ControllerTypeConfig(BaseModel):  controller: str
class ConfigFileRequest(BaseModel):     filename: str

class KMBoxConfigRequest(BaseModel):
    ip:   str = Field(..., min_length=7, max_length=64)
    port: int = Field(default=57856, ge=1, le=65535)
    uuid: str = Field(default="")

class HumanizeConfig(BaseModel):
    jitter_strength: float = Field(..., ge=0.0, le=1.0)
    smooth_factor:   float = Field(..., ge=0.0, le=0.95)

class RapidFireConfig(BaseModel):
    enabled:      bool  = False
    interval_ms:  int   = Field(default=100, ge=30, le=2000)

class HipFireConfig(BaseModel):
    enabled:      bool  = False
    pull_down:    float = Field(default=0.0, ge=0, le=300)
    horizontal:   float = Field(default=0.0, ge=-300, le=300)


# ══════════════════════════════════════════════════════════════════════════════
#  AppState
# ══════════════════════════════════════════════════════════════════════════════
# ── Beep helper ──────────────────────────────────────────────────────────────
def _play_beep(enabled: bool):
    """Toggle ON/OFF beep — winsound on Windows, otherwise silent."""
    try:
        import winsound
        if enabled:
            # ON: low → high (power-up feel)
            winsound.Beep(600, 50)
            time.sleep(0.03)
            winsound.Beep(1000, 70)
        else:
            # OFF: high → low (power-down feel)
            winsound.Beep(1000, 50)
            time.sleep(0.03)
            winsound.Beep(400, 80)
    except Exception:
        pass   # non-Windows or no audio — silent

def _play_slot_beep(slot: int):
    """Slot switch beep — short single tone per slot number, clearly different from toggle."""
    try:
        import winsound
        # Each slot has a distinct pitch: slot 1=low, slot 5=high
        freqs = {1: 700, 2: 850, 3: 1000, 4: 1200, 5: 1500}
        freq = freqs.get(slot, 900)
        winsound.Beep(freq, 55)
    except Exception:
        pass


class AppState:
    def __init__(self):
        self.active_pull_down_value  = 1.0
        self.active_horizontal_value = 0.0
        self.horizontal_delay_ms     = 500
        self.horizontal_duration_ms  = 2000
        self.vertical_delay_ms       = 0
        self.vertical_duration_ms    = 0
        self.pull_down_curve         = None
        self.horizontal_curve        = None
        self.jitter_strength         = 0.15
        self.smooth_factor           = 0.60
        self.is_enabled              = False
        self.beep_enabled            = True   # beep on toggle on/off
        self.toggle_button           = "M5"
        self.current_config_file     = DEFAULT_CONFIG_FILE
        self.trigger_mode            = "LMB"
        self.controller_type         = "makcu"
        self.kmbox_ip                = "192.168.2.188"
        self.kmbox_port              = 57856
        self.kmbox_uuid              = ""
        # ── Rapid Fire ───────────────────────────────────────────────────────
        self.rapid_fire_enabled      = False
        self.rapid_fire_interval_ms  = 100
        # Global RF baseline — set only by the UI/API, never overwritten by slot activation.
        # Per-slot RF reads this when inherit (slot_rf is None).
        self._global_rf_enabled      = False
        self._global_rf_interval_ms  = 100
        # ── Hip Fire ─────────────────────────────────────────────────────────
        self.hip_fire_enabled        = False
        self.hip_pull_down           = 0.0
        self.hip_horizontal          = 0.0
        self.lock                    = threading.Lock()

    def _g(self, a):
        with self.lock: return getattr(self, a)
    def _s(self, a, v):
        with self.lock: setattr(self, a, v)

    def set_active_value(self, v):
        if v != v: return
        self._s('active_pull_down_value', max(0, min(300, v)))
    def get_active_value(self):          return self._g('active_pull_down_value')

    def set_horizontal_value(self, v):
        if v != v: return
        self._s('active_horizontal_value', max(-300, min(300, v)))
    def get_horizontal_value(self):      return self._g('active_horizontal_value')

    def set_horizontal_delay(self, ms):  self._s('horizontal_delay_ms', max(0, min(5000, int(ms))))
    def get_horizontal_delay(self):      return self._g('horizontal_delay_ms')
    def set_horizontal_duration(self, ms): self._s('horizontal_duration_ms', max(0, min(10000, int(ms))))
    def get_horizontal_duration(self):   return self._g('horizontal_duration_ms')
    def set_vertical_delay(self, ms):    self._s('vertical_delay_ms', max(0, min(5000, int(ms))))
    def get_vertical_delay(self):        return self._g('vertical_delay_ms')
    def set_vertical_duration(self, ms): self._s('vertical_duration_ms', max(0, min(10000, int(ms))))
    def get_vertical_duration(self):     return self._g('vertical_duration_ms')

    def set_curves(self, pd, hz):
        with self.lock:
            self.pull_down_curve  = pd
            self.horizontal_curve = hz
    def get_curves(self):
        with self.lock: return self.pull_down_curve, self.horizontal_curve

    def set_jitter(self, v):   self._s('jitter_strength', max(0.0, min(1.0, v))); self.persist()
    def get_jitter(self):      return self._g('jitter_strength')
    def set_smooth(self, v):   self._s('smooth_factor', max(0.0, min(0.94, v))); self.persist()
    def get_smooth(self):      return self._g('smooth_factor')
    def get_enabled(self):     return self._g('is_enabled')

    def toggle_enabled(self):
        with self.lock:
            self.is_enabled = not self.is_enabled
            state = self.is_enabled
            beep  = self.beep_enabled
        if beep:
            # beep in a separate thread so the loop is not blocked
            threading.Thread(target=_play_beep, args=(state,), daemon=True).start()
        return state

    def set_beep(self, v):   self._s('beep_enabled', bool(v)); self.persist()
    def get_beep(self):      return self._g('beep_enabled')

    def set_toggle_button(self, b):
        with self.lock:
            if b in VALID_TOGGLE_BTNS: self.toggle_button = b; r = b
            else: return None
        self.persist(); return r
    def get_toggle_button(self): return self._g('toggle_button')

    def set_current_config_file(self, f):
        if not f.endswith('.json'): f += '.json'
        self._s('current_config_file', f)
        self.persist()
        return f
    def get_current_config_file(self): return self._g('current_config_file')

    def set_trigger_mode(self, m):
        with self.lock:
            if m in VALID_TRIGGER_MODES: self.trigger_mode = m; r = m
            else: return None
        self.persist(); return r
    def get_trigger_mode(self): return self._g('trigger_mode')

    def set_controller_type(self, c):
        with self.lock:
            if c in VALID_CONTROLLERS: self.controller_type = c; r = c
            else: return None
        self.persist(); return r
    def get_controller_type(self): return self._g('controller_type')

    def set_kmbox_config(self, ip, port, uuid):
        with self.lock:
            self.kmbox_ip = ip; self.kmbox_port = port; self.kmbox_uuid = uuid
        self.persist()
    def get_kmbox_config(self):
        with self.lock: return {"ip": self.kmbox_ip, "port": self.kmbox_port, "uuid": self.kmbox_uuid}

    # ── Rapid Fire ────────────────────────────────────────────────────────────
    def set_rapid_fire(self, enabled, interval_ms, from_slot=False):
        """Set rapid fire. from_slot=True preserves the global baseline (used by slot activation)."""
        with self.lock:
            self.rapid_fire_enabled     = enabled
            self.rapid_fire_interval_ms = max(30, min(2000, int(interval_ms)))
            if not from_slot:
                self._global_rf_enabled     = self.rapid_fire_enabled
                self._global_rf_interval_ms = self.rapid_fire_interval_ms
        if not from_slot: self.persist()
    def get_rapid_fire(self):
        with self.lock: return self.rapid_fire_enabled, self.rapid_fire_interval_ms
    def get_global_rapid_fire(self):
        """Global RF as set by UI/API — never overwritten by per-slot activation."""
        with self.lock: return self._global_rf_enabled, self._global_rf_interval_ms

    # ── Hip Fire ──────────────────────────────────────────────────────────────
    def set_hip_fire(self, enabled, pull_down, horizontal):
        with self.lock:
            self.hip_fire_enabled = enabled
            self.hip_pull_down    = max(0, min(300, pull_down))
            self.hip_horizontal   = max(-300, min(300, horizontal))
        self.persist()
    def get_hip_fire(self):
        with self.lock: return self.hip_fire_enabled, self.hip_pull_down, self.hip_horizontal

    def get_status(self):
        ctrl = get_active_controller()
        rf_en, rf_ms = self.get_rapid_fire()
        hf_en, hf_pd, hf_hz = self.get_hip_fire()
        with self.lock:
            return {
                "is_enabled":             self.is_enabled,
                "toggle_button":          self.toggle_button,
                "pull_down":              self.active_pull_down_value,
                "horizontal":             self.active_horizontal_value,
                "horizontal_delay_ms":    self.horizontal_delay_ms,
                "horizontal_duration_ms": self.horizontal_duration_ms,
                "vertical_delay_ms":      self.vertical_delay_ms,
                "vertical_duration_ms":   self.vertical_duration_ms,
                "jitter_strength":        self.jitter_strength,
                "smooth_factor":          self.smooth_factor,
                "current_config_file":    self.current_config_file,
                "trigger_mode":           self.trigger_mode,
                "controller_type":        self.controller_type,
                "kmbox_ip":               self.kmbox_ip,
                "kmbox_port":             self.kmbox_port,
                "has_pull_curve":         self.pull_down_curve is not None,
                "has_horiz_curve":        self.horizontal_curve is not None,
                "ctrl_connected":         ctrl.is_connected(),
                "rapid_fire_enabled":     rf_en,
                "rapid_fire_interval_ms": rf_ms,
                "hip_fire_enabled":       hf_en,
                "hip_pull_down":          hf_pd,
                "hip_horizontal":         hf_hz,
                "beep_enabled":           self.beep_enabled,
                # weapon slot — populated after weapon_slot_mgr exists
                "weapon_slot_enabled":    False,
                "active_slot":            0,
            }

    # ── Persistent save/restore (v8.0) ───────────────────────────────────────
    def to_settings(self) -> dict:
        with self.lock:
            return {
                "controller_type":        self.controller_type,
                "toggle_button":          self.toggle_button,
                "trigger_mode":           self.trigger_mode,
                "beep_enabled":           self.beep_enabled,
                "current_config_file":    self.current_config_file,
                "kmbox_ip":               self.kmbox_ip,
                "kmbox_port":             self.kmbox_port,
                "kmbox_uuid":             self.kmbox_uuid,
                "jitter_strength":        self.jitter_strength,
                "smooth_factor":          self.smooth_factor,
                "rapid_fire_enabled":     self._global_rf_enabled,
                "rapid_fire_interval_ms": self._global_rf_interval_ms,
                "hip_fire_enabled":       self.hip_fire_enabled,
                "hip_pull_down":          self.hip_pull_down,
                "hip_horizontal":         self.hip_horizontal,
            }

    def from_settings(self, d: dict):
        with self.lock:
            if "controller_type"    in d and d["controller_type"]    in VALID_CONTROLLERS:
                self.controller_type = d["controller_type"]
            if "toggle_button"      in d and d["toggle_button"]      in VALID_TOGGLE_BTNS:
                self.toggle_button = d["toggle_button"]
            if "trigger_mode"       in d and d["trigger_mode"]       in VALID_TRIGGER_MODES:
                self.trigger_mode = d["trigger_mode"]
            if "beep_enabled"       in d: self.beep_enabled           = bool(d["beep_enabled"])
            if "current_config_file" in d: self.current_config_file   = d["current_config_file"]
            if "kmbox_ip"           in d: self.kmbox_ip               = d["kmbox_ip"]
            if "kmbox_port"         in d: self.kmbox_port             = int(d["kmbox_port"])
            if "kmbox_uuid"         in d: self.kmbox_uuid             = d["kmbox_uuid"]
            if "jitter_strength"    in d: self.jitter_strength        = float(d["jitter_strength"])
            if "smooth_factor"      in d: self.smooth_factor          = float(d["smooth_factor"])
            if "rapid_fire_enabled" in d:
                self.rapid_fire_enabled  = bool(d["rapid_fire_enabled"])
                self._global_rf_enabled  = self.rapid_fire_enabled
            if "rapid_fire_interval_ms" in d:
                self.rapid_fire_interval_ms  = int(d["rapid_fire_interval_ms"])
                self._global_rf_interval_ms  = self.rapid_fire_interval_ms
            if "hip_fire_enabled"   in d: self.hip_fire_enabled       = bool(d["hip_fire_enabled"])
            if "hip_pull_down"      in d: self.hip_pull_down          = float(d["hip_pull_down"])
            if "hip_horizontal"     in d: self.hip_horizontal         = float(d["hip_horizontal"])

    def persist(self):
        """Save current settings to disk. Called automatically on every change."""
        save_settings({"app": self.to_settings(), "weapon_slots": _get_slot_settings()})


app_state = AppState()

def _ensure_current_config_file_valid(prefer_non_default_if_empty: bool = False):
    """
    Ensure AppState.current_config_file points to an existing config file.

    When running from source with bundled example profiles, default.json may be empty.
    In that case, optionally select the first non-default profile so the UI isn't blank.
    """
    try:
        current = app_state.get_current_config_file() or DEFAULT_CONFIG_FILE
    except Exception:
        current = DEFAULT_CONFIG_FILE

    # Fix invalid/missing file references (e.g. settings.json points to a deleted profile)
    if not os.path.exists(get_config_path(current)):
        app_state.current_config_file = DEFAULT_CONFIG_FILE
        current = DEFAULT_CONFIG_FILE

    if prefer_non_default_if_empty and current == DEFAULT_CONFIG_FILE:
        try:
            cfgs = read_configs(DEFAULT_CONFIG_FILE)
        except Exception:
            cfgs = {}
        if not cfgs:
            # Pick a non-default config file that actually has entries (best-effort)
            others = [f for f in list_config_files() if f.endswith(".json") and f != DEFAULT_CONFIG_FILE]
            best = None
            best_count = -1
            for f in others:
                try:
                    c = read_configs(f)
                    n = len(c) if isinstance(c, dict) else 0
                except Exception:
                    n = 0
                if n > best_count:
                    best_count = n
                    best = f
            if best and best_count > 0:
                app_state.current_config_file = best


# ── Settings helper used by AppState.persist() ────────────────────────────────
def _get_slot_settings() -> dict:
    """Snapshot weapon slot state for persistence. Safe to call before weapon_slot_mgr exists."""
    try:
        return {
            "enabled":  weapon_slot_mgr.get_enabled(),
            "slots":    weapon_slot_mgr.get_slots(),
            "slot_rf":  {str(k): v for k, v in weapon_slot_mgr.get_slot_rf().items()},
        }
    except NameError:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  WeaponSlotManager  — per-slot RCS config + keyboard hotkey switching
#
#  Usage:
#    • Assign a saved gun config name to each slot key (1–5) via the UI or API
#    • Enable weapon slot mode
#    • Press 1 in-game → slot 1 config loads automatically into RCS
#    • Press 2 in-game → slot 2 config loads (e.g. pistol with lower pull_down)
#    • Slots without an assigned config are silently skipped
#
#  Detection method: GetAsyncKeyState on VK codes 0x31–0x35 (keys '1'–'5')
#  Edge-detect (rising edge only) so holding the key does not spam-switch.
# ══════════════════════════════════════════════════════════════════════════════

# Virtual-key codes for keyboard keys '1'–'5'
_VK_SLOT = {
    1: 0x31,  # '1'
    2: 0x32,  # '2'
    3: 0x33,  # '3'
    4: 0x34,  # '4'
    5: 0x35,  # '5'
}


class WeaponSlotManager:
    """Stores per-slot config assignments and loads them into app_state on slot switch."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._enabled = False
        self._active_slot = 0        # 0 = none selected yet
        # slot_num → config name (str) or None
        self._slots: Dict[int, Optional[str]] = {i: None for i in range(1, 6)}
        # slot_num → rapid fire settings: {"enabled": bool, "interval_ms": int} or None (= inherit global)
        self._slot_rf: Dict[int, Optional[dict]] = {i: None for i in range(1, 6)}

    # ── Public API ────────────────────────────────────────────────────────────
    def set_enabled(self, v: bool):
        with self._lock:
            self._enabled = bool(v)

    def get_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_slot(self, slot: int, config_name: Optional[str]):
        """Assign a gun config name to a slot (1–5). Pass None to clear."""
        if slot not in range(1, 6):
            return False
        with self._lock:
            self._slots[slot] = config_name or None
        return True

    def get_slots(self) -> Dict[int, Optional[str]]:
        with self._lock:
            return dict(self._slots)

    def set_slot_rf(self, slot: int, enabled: Optional[bool], interval_ms: Optional[int]):
        """Set per-slot rapid fire override. Pass enabled=None to clear (inherit global)."""
        if slot not in range(1, 6):
            return False
        with self._lock:
            if enabled is None:
                self._slot_rf[slot] = None
            else:
                self._slot_rf[slot] = {
                    "enabled": bool(enabled),
                    "interval_ms": max(30, min(2000, int(interval_ms or 100)))
                }
        return True

    def get_slot_rf(self) -> Dict[int, Optional[dict]]:
        with self._lock:
            return dict(self._slot_rf)

    def get_active_slot(self) -> int:
        with self._lock:
            return self._active_slot

    def activate_slot(self, slot: int) -> bool:
        """Load the config assigned to *slot* into app_state. Returns True on success."""
        with self._lock:
            if not self._enabled:
                return False
            name = self._slots.get(slot)
            if not name:
                return False
            self._active_slot = slot

        # Read config outside lock — I/O can be slow
        cf   = app_state.get_current_config_file()
        cfgs = read_configs(cf)
        cfg  = cfgs.get(name)
        if cfg is None:
            print(f"[SLOT] Config '{name}' not found in {cf}")
            return False

        # Apply all fields that exist in the saved config
        def _f(key, default=0.0): return float(cfg.get(key, default))
        def _i(key, default=0):   return int(cfg.get(key, default))

        app_state.set_active_value(_f("pull_down", 1.0))
        app_state.set_horizontal_value(_f("horizontal", 0.0))
        app_state.set_horizontal_delay(_i("horizontal_delay_ms", 500))
        app_state.set_horizontal_duration(_i("horizontal_duration_ms", 2000))
        app_state.set_vertical_delay(_i("vertical_delay_ms", 0))
        app_state.set_vertical_duration(_i("vertical_duration_ms", 0))

        pd_curve = cfg.get("pull_down_curve")
        hz_curve = cfg.get("horizontal_curve")
        app_state.set_curves(
            pd_curve if isinstance(pd_curve, list) and pd_curve else None,
            hz_curve if isinstance(hz_curve, list) and hz_curve else None,
        )

        # Hip fire overrides (if present in config)
        if "hip_pull_down" in cfg or "hip_horizontal" in cfg:
            hf_en, hf_pd, hf_hz = app_state.get_hip_fire()
            app_state.set_hip_fire(
                hf_en,
                float(cfg.get("hip_pull_down", hf_pd)),
                float(cfg.get("hip_horizontal", hf_hz)),
            )

        print(f"[SLOT] Slot {slot} → '{name}' loaded")

        # Apply per-slot rapid fire if configured
        with self._lock:
            rf_override = self._slot_rf.get(slot)
        if rf_override is not None:
            # Slot has an explicit RF setting — apply it (marks as from_slot so global baseline is preserved)
            app_state.set_rapid_fire(rf_override["enabled"], rf_override["interval_ms"], from_slot=True)
            print(f"[SLOT] Slot {slot} rapid fire: enabled={rf_override['enabled']} interval={rf_override['interval_ms']}ms")
        else:
            # No slot override → inherit: restore the global RF baseline
            g_en, g_ms = app_state.get_global_rapid_fire()
            app_state.set_rapid_fire(g_en, g_ms, from_slot=True)
            print(f"[SLOT] Slot {slot} rapid fire: inherit global (enabled={g_en} interval={g_ms}ms)")

        _play_slot_beep(slot)   # distinct per-slot tone
        return True


weapon_slot_mgr = WeaponSlotManager()


# ── Restore persistent settings (v8.0) ────────────────────────────────────────
def _restore_settings():
    d = load_settings()
    if not d:
        print("[SETTINGS] No saved settings found — using defaults")
        _ensure_current_config_file_valid(prefer_non_default_if_empty=True)
        return
    try:
        if "app" in d:
            app_state.from_settings(d["app"])
            print("[SETTINGS] App settings restored")
        _ensure_current_config_file_valid(prefer_non_default_if_empty=False)
        if "weapon_slots" in d:
            ws = d["weapon_slots"]
            if "enabled" in ws:
                weapon_slot_mgr.set_enabled(ws["enabled"])
            for slot_str, cfg_name in (ws.get("slots") or {}).items():
                weapon_slot_mgr.set_slot(int(slot_str), cfg_name)
            for slot_str, rf in (ws.get("slot_rf") or {}).items():
                if rf:
                    weapon_slot_mgr.set_slot_rf(int(slot_str), rf.get("enabled"), rf.get("interval_ms"))
            print("[SETTINGS] Weapon slot settings restored")
    except Exception as e:
        print(f"[SETTINGS] Restore error: {e}")

_restore_settings()


def _weapon_slot_detector():
    """Background thread — polls keyboard for slot keys 1–5.
    Uses GetAsyncKeyState from ctypes (same as SoftwareController).
    Requires Windows; silently does nothing on other platforms."""
    if not _HAS_CTYPES:
        return
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return

    prev = {slot: False for slot in _VK_SLOT}

    while True:
        try:
            if weapon_slot_mgr.get_enabled():
                for slot, vk in _VK_SLOT.items():
                    pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    # Rising edge only
                    if pressed and not prev[slot]:
                        weapon_slot_mgr.activate_slot(slot)
                    prev[slot] = pressed
            else:
                # Reset edge-detect state when disabled
                for slot in _VK_SLOT:
                    prev[slot] = False
        except Exception as e:
            print(f"[SLOT_DETECT] {e}")
        time.sleep(0.015)   # ~67 Hz poll — fast enough, won't miss a keypress


threading.Thread(target=_weapon_slot_detector, daemon=True, name="WeaponSlotDetector").start()


# ══════════════════════════════════════════════════════════════════════════════
#  Humanization
# ══════════════════════════════════════════════════════════════════════════════
class _Smoother:
    def __init__(self):
        self.x = 0.0; self.y = 0.0
    def update(self, tx, ty, alpha):
        alpha = max(0.06, min(1.0, alpha))
        self.x = alpha * tx + (1 - alpha) * self.x
        self.y = alpha * ty + (1 - alpha) * self.y
        return int(round(self.x)), int(round(self.y))
    def reset(self):
        self.x = 0.0; self.y = 0.0

def humanize(rx, ry, jitter, smoother, smooth):
    if jitter > 0:
        sc = max(abs(rx), abs(ry)) * jitter
        rx += random.gauss(0, sc * 0.5)
        ry += random.gauss(0, sc * 0.5)
    alpha = max(0.06, min(1.0, 1.0 - smooth))
    return smoother.update(rx, ry, alpha)


# ══════════════════════════════════════════════════════════════════════════════
#  Main control loop
# ══════════════════════════════════════════════════════════════════════════════
TICK_S = 0.010
# Rapid Fire console logs ([RF] activate / click / …) — off by default
RF_DEBUG = False


def _rf_log(msg: str) -> None:
    if RF_DEBUG:
        print(msg)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=mouse_control_loop, daemon=True).start()
    yield


def mouse_control_loop():
    toggle_was    = False
    hold_start    = None
    curve_tick    = 0
    smoother      = _Smoother()
    _listener_started = {"makcu": False, "kmbox": False, "software": False}

    # retry backoff
    _retry_delay          = 0.5
    _last_connect_attempt = 0.0
    _last_ctrl_key        = None


    _rf_lmb_held   = [False]   # shared flag for KMBox/MAKCU
    _rf_lmb_false_count = [0]  # count consecutive False from hardware poll

    def _rf_lmb_monitor():
        """Separate thread for KMBox/MAKCU — tracks physical LMB only.
        Unaffected by click_lmb() because hardware does not see synthetic input."""
        while True:
            try:
                ctrl = get_active_controller()
                # SoftwareController uses get_physical_lmb() instead
                if isinstance(ctrl, SoftwareController):
                    time.sleep(0.020)
                    continue
                raw = ctrl.get_button_state("LMB")
                if raw:
                    _rf_lmb_held[0] = True
                    _rf_lmb_false_count[0] = 0
                else:
                    _rf_lmb_false_count[0] += 1
                    # require 3 consecutive False (~24ms) before treating as real release
                    # avoids false negatives from KMBox monitor poll gaps
                    if _rf_lmb_false_count[0] >= 3:
                        _rf_lmb_held[0] = False
                time.sleep(0.008)
            except Exception:
                time.sleep(0.020)

    threading.Thread(target=_rf_lmb_monitor, daemon=True, name="RF_LMB_Monitor").start()

    def _rf_worker():
        # ── v5.5 RF hold mechanic ─────────────────────────────────────────────
        # LMB+RMB: hold RMB + LMB edge → RF until RMB released
        # LMB: hold LMB → RF until LMB released
        # Works alongside RCS — main loop still applies recoil as usual
        # ─────────────────────────────────────────────────────────────────────
        _last_click_time = 0.0
        _click_count     = 0
        _sw_lmb_down     = False   # track synthetic DOWN/UP for SoftwareController

        # arm/active state
        _rf_armed      = False
        _rf_active     = False
        _prev_lmb      = False
        _prev_rmb      = False

        def _reset_rf_state():
            nonlocal _rf_armed, _rf_active, _prev_lmb, _prev_rmb
            nonlocal _last_click_time, _click_count
            _rf_armed  = False
            _rf_active = False
            _prev_lmb  = False
            _prev_rmb  = False
            _last_click_time = 0.0
            _click_count = 0

        while True:
            try:
                # ── Per-slot RF override ──────────────────────────────────────
                # Read the global RF baseline (set by UI/API only).
                # If the active slot has its own RF setting, use that instead.
                # This keeps _rf_worker in sync with activate_slot() and avoids
                # the previous bug where switching to a non-RF slot still fired
                # because app_state still held the previous slot's RF values.
                rf_en, rf_ms = app_state.get_global_rapid_fire()
                if weapon_slot_mgr.get_enabled():
                    active_slot = weapon_slot_mgr.get_active_slot()
                    if active_slot > 0:
                        slot_rf = weapon_slot_mgr.get_slot_rf().get(active_slot)
                        if slot_rf is not None:
                            rf_en = slot_rf["enabled"]
                            rf_ms = slot_rf["interval_ms"]

                if not rf_en:
                    # RF off — if SW controller left LMB down, send UP first
                    if _sw_lmb_down:
                        ctrl = get_active_controller()
                        if isinstance(ctrl, SoftwareController):
                            ctrl.lmb_up()
                        _sw_lmb_down = False
                    _reset_rf_state()
                    _rf_lmb_held[0] = False
                    _rf_lmb_false_count[0] = 0
                    time.sleep(0.020)
                    continue

                ctrl         = get_active_controller()
                trigger_mode = app_state.get_trigger_mode()
                is_sw        = isinstance(ctrl, SoftwareController)

                # ── Read physical buttons ─────────────────────────────────────
                # SW: get_physical_lmb() = GetAsyncKeyState directly
                #     → not affected by synthetic LEFTUP during RF clicks
                # KMBox/MAKCU: _rf_lmb_held (debounced monitor thread)
                if is_sw:
                    lmb_phys = ctrl.get_physical_lmb()
                else:
                    lmb_phys = _rf_lmb_held[0]

                rmb = ctrl.get_button_state("RMB")

                # ── Arm / Active logic ────────────────────────────────────────
                if trigger_mode == "LMB+RMB":
                    # Behavior:
                    #   Hold RMB + press LMB (need not be simultaneous) → activate
                    #   Release RMB → deactivate (LMB state ignored)
                    #   Release LMB while still holding RMB → deactivate

                    # activate: RMB held + LMB rising edge
                    if rmb and lmb_phys and not _prev_lmb:
                        _rf_active = True
                        _last_click_time = 0.0
                        _click_count = 0
                        _rf_log("[RF] activated (LMB edge while RMB held)")

                    if _rf_active:
                        # deactivate when either button is released
                        if not rmb or not lmb_phys:
                            _rf_log(f"[RF] deactivated (lmb={lmb_phys} rmb={rmb}) clicks={_click_count}")
                            _rf_active = False

                    firing = _rf_active

                else:
                    # LMB mode: hold LMB → active until release
                    # Use lmb_phys directly — no edge detect needed
                    # get_physical_lmb() reads real hardware
                    # Not disturbed by synthetic UP between clicks
                    if lmb_phys and not _rf_active:
                        _rf_active = True
                        _last_click_time = 0.0
                        _click_count = 0
                        _rf_log("[RF] activated (LMB held)")

                    if _rf_active and not lmb_phys:
                        _rf_log(f"[RF] deactivated (LMB released) clicks={_click_count}")
                        _rf_active = False

                    firing = _rf_active

                _prev_lmb = lmb_phys
                _prev_rmb = rmb

                # ── Fire ──────────────────────────────────────────────────────
                if firing:
                    now        = time.perf_counter()
                    interval_s = max(0.030, rf_ms / 1000.0)

                    if now - _last_click_time >= interval_s:
                        if is_sw:
                            if _sw_lmb_down:
                                ctrl.lmb_up()
                                time.sleep(0.008)
                            ctrl.lmb_down()
                            _sw_lmb_down = True
                        else:
                            ctrl.click_lmb()
                        _click_count += 1
                        _last_click_time = now
                        _rf_log(f"[RF] click #{_click_count}  interval={rf_ms}ms")

                    time.sleep(0.002)
                else:
                    if is_sw and _sw_lmb_down:
                        ctrl.lmb_up()
                        _sw_lmb_down = False
                    time.sleep(0.005)

            except Exception as e:
                _rf_log(f"[RF] exception: {e}")
                time.sleep(0.010)

    _rf_thread = threading.Thread(target=_rf_worker, daemon=True, name="RapidFireWorker")
    _rf_thread.start()

    while True:
        t0 = time.perf_counter()
        try:
            ctrl     = get_active_controller()
            ctrl_key = app_state.get_controller_type()

            # reset backoff when user changes controller type
            if ctrl_key != _last_ctrl_key:
                _retry_delay  = 0.5
                _last_ctrl_key = ctrl_key

            if not ctrl.is_connected():
                now = time.perf_counter()
                if now - _last_connect_attempt >= _retry_delay:
                    _last_connect_attempt = now
                    ok = ctrl.connect()
                    if ok:
                        _retry_delay = 0.5
                        if not _listener_started[ctrl_key]:
                            ctrl.StartButtonListener()
                            _listener_started[ctrl_key] = True
                    else:
                        _retry_delay = min(_retry_delay * 2, 10.0)
                time.sleep(0.1)
                continue

            if not _listener_started[ctrl_key]:
                ctrl.StartButtonListener()
                _listener_started[ctrl_key] = True

            # ── Toggle RCS on/off ─────────────────────────────────────────────
            btn     = app_state.get_toggle_button()
            pressed = ctrl.get_button_state(btn)
            if pressed and not toggle_was:
                app_state.toggle_enabled()
                smoother.reset()
                hold_start = None
            toggle_was = pressed

            # ── FIX v5.5: physical LMB like RF worker (fixes cached state) ──
            if isinstance(ctrl, SoftwareController):
                lmb_phys = ctrl.get_physical_lmb()          # direct GetAsyncKeyState
            else:
                lmb_phys = _rf_lmb_held[0]                  # KMBox / MAKCU monitor thread

            raw_lmb = ctrl.get_button_state("LMB")          # kept for possible future use
            rmb     = ctrl.get_button_state("RMB")

            lmb = lmb_phys   # use physical state for fire condition

            # ── Hip Fire detection ────────────────────────────────────────────
            hf_en, hf_pd, hf_hz = app_state.get_hip_fire()
            is_hip = hf_en and not rmb

            # ── Fire condition ────────────────────────────────────────────────
            trigger_mode = app_state.get_trigger_mode()
            if is_hip:
                fire = lmb
            else:
                fire = (lmb and rmb) if trigger_mode == "LMB+RMB" else lmb

            # ── RF + RCS together ─────────────────────────────────────────
            # RF worker (separate thread) handles clicks
            # RCS main loop uses same fire condition — recoil moves apply normally
            # Both can run: RF spam-clicks, RCS pulls recoil
            if app_state.get_enabled() and fire:
                now = time.perf_counter()
                if hold_start is None:
                    hold_start = now
                    curve_tick = 0
                hold_ms = (now - hold_start) * 1000.0

                pd_curve, hz_curve = app_state.get_curves()

                # Choose values: hip or normal
                if is_hip:
                    raw_y = hf_pd / 5.0
                    raw_x = hf_hz / 5.0
                else:
                    # Vertical
                    v_delay  = app_state.get_vertical_delay()
                    v_dur    = app_state.get_vertical_duration()
                    v_active = (hold_ms >= v_delay) and (v_dur == 0 or hold_ms <= v_delay + v_dur)
                    if v_active:
                        if pd_curve and curve_tick < len(pd_curve):
                            raw_y = pd_curve[curve_tick] / 5.0
                        else:
                            raw_y = app_state.get_active_value() / 5.0
                    else:
                        raw_y = 0.0
                    # Horizontal
                    h_delay = app_state.get_horizontal_delay()
                    h_dur   = app_state.get_horizontal_duration()
                    raw_x   = 0.0
                    if hold_ms >= h_delay and (h_dur == 0 or hold_ms <= h_delay + h_dur):
                        if hz_curve and curve_tick < len(hz_curve):
                            raw_x = hz_curve[curve_tick] / 5.0
                        else:
                            raw_x = app_state.get_horizontal_value() / 5.0

                curve_tick += 1
                mx, my = humanize(raw_x, raw_y, app_state.get_jitter(), smoother, app_state.get_smooth())
                if mx or my:
                    ctrl.simple_move_mouse(mx, my)
            else:
                hold_start = None
                curve_tick = 0
                smoother.reset()

        except Exception as e:
            print(f"[LOOP] {e}")
            time.sleep(1)
            continue

        elapsed = time.perf_counter() - t0
        coarse  = TICK_S - elapsed - 0.001
        if coarse > 0: time.sleep(coarse)
        while (time.perf_counter() - t0) < TICK_S: pass


# ══════════════════════════════════════════════════════════════════════════════
#  FastAPI
# ══════════════════════════════════════════════════════════════════════════════
app = FastAPI(lifespan=lifespan)
def _resource_base_dir() -> Path:
    """
    Resolve bundled resource base directory.

    - Source run: beside this file.
    - PyInstaller onefile: extracted payload is exposed via sys._MEIPASS.
    - Nuitka onefile: embedded --include-data-dir assets live next to the extracted
      main module (dirname(__file__)), not next to the outer .exe (see Nuitka manual:
      onefile-finding-files). sys.argv[0] / sys.executable point at the .exe path.
    - Other frozen layouts: fall back to the executable directory.
    """
    if _is_packaged_runtime():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # Nuitka sets __compiled__ on the main module; use __file__ for bundled data.
        if "__compiled__" in globals():
            return Path(__file__).resolve().parent
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

RES_DIR = _resource_base_dir()
TEMPLATE_FILE = RES_DIR / "templates" / "index.html"
STATIC_DIR = RES_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                M = {
                    "pull_down":              ("set_active_value",        float),
                    "horizontal":             ("set_horizontal_value",    float),
                    "horizontal_delay_ms":    ("set_horizontal_delay",    int),
                    "horizontal_duration_ms": ("set_horizontal_duration", int),
                    "vertical_delay_ms":      ("set_vertical_delay",      int),
                    "vertical_duration_ms":   ("set_vertical_duration",   int),
                    "jitter_strength":        ("set_jitter",              float),
                    "smooth_factor":          ("set_smooth",              float),
                }
                for k, (fn, conv) in M.items():
                    if k in msg:
                        try: getattr(app_state, fn)(conv(msg[k]))
                        except: pass

                # BUG FIX: horizontal_curve handler in WebSocket
                # v5.0 lacked this so horizontal_curve was not set via WS
                pd_curve, hz_curve = app_state.get_curves()

                if "pull_down_curve" in msg:
                    v = msg["pull_down_curve"]
                    pd_curve = v if isinstance(v, list) and len(v) > 0 else None

                if "horizontal_curve" in msg:
                    v = msg["horizontal_curve"]
                    hz_curve = v if isinstance(v, list) and len(v) > 0 else None

                # update both curves together when either changes
                if "pull_down_curve" in msg or "horizontal_curve" in msg:
                    app_state.set_curves(pd_curve, hz_curve)

            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass


@app.get("/status")
async def status():
    s = app_state.get_status()
    # Inject live weapon slot data (manager created after app_state)
    s["weapon_slot_enabled"] = weapon_slot_mgr.get_enabled()
    s["active_slot"]         = weapon_slot_mgr.get_active_slot()
    # Include active slot's config name so UI can highlight it
    active = weapon_slot_mgr.get_active_slot()
    slots  = weapon_slot_mgr.get_slots()
    s["active_slot_config"] = slots.get(active) if active > 0 else None
    return s


@app.get("/diag")
async def diag():
    """Lightweight diagnostics for troubleshooting packaging and paths."""
    try:
        exe = os.path.abspath(sys.executable)
    except Exception:
        exe = None
    try:
        argv0 = os.path.abspath(sys.argv[0]) if sys.argv else None
    except Exception:
        argv0 = None

    return {
        "version": "8.3",
        "packaged": _is_packaged_runtime(),
        "nuitka": ("__compiled__" in globals()),
        "pyinstaller_meipass": getattr(sys, "_MEIPASS", None),
        "exe": exe,
        "argv0": argv0,
        "res_dir": str(RES_DIR),
        "static_dir": str(STATIC_DIR),
        "static_exists": STATIC_DIR.exists(),
        "template_file": str(TEMPLATE_FILE),
        "template_exists": TEMPLATE_FILE.exists(),
        "data_root": _data_root,
        "config_dir": CONFIG_DIR,
        "settings_file": SETTINGS_FILE,
        "macros_file": MACROS_FILE,
    }

@app.post("/toggle")
async def toggle():
    return {"is_enabled": app_state.toggle_enabled()}

@app.post("/toggle-button")
async def set_toggle_button(c: ToggleButtonConfig):
    r = app_state.set_toggle_button(c.button)
    if r is None: raise HTTPException(400, f"Must be one of {VALID_TOGGLE_BTNS}")
    return {"toggle_button": r}

@app.post("/trigger-mode")
async def set_trigger_mode(c: TriggerModeConfig):
    r = app_state.set_trigger_mode(c.mode)
    if r is None: raise HTTPException(400, f"Must be one of {VALID_TRIGGER_MODES}")
    return {"trigger_mode": r}

@app.post("/controller-type")
async def set_controller_type(c: ControllerTypeConfig):
    r = app_state.set_controller_type(c.controller)
    if r is None: raise HTTPException(400, f"Must be one of {VALID_CONTROLLERS}")
    return {"controller_type": r}

@app.get("/kmbox-config")
async def get_kmbox():
    c = app_state.get_kmbox_config()
    return {"ip": c["ip"], "port": c["port"]}

@app.post("/kmbox-config")
async def save_kmbox(r: KMBoxConfigRequest):
    app_state.set_kmbox_config(r.ip, r.port, r.uuid)
    if app_state.get_controller_type() == "kmbox":
        kmbox_controller.disconnect()
    return {"message": "KMBox config saved."}

@app.post("/kmbox-connect")
async def kmbox_connect():
    kmbox_controller.disconnect()
    cfg = app_state.get_kmbox_config()
    uuid = cfg["uuid"].replace("-","").replace(" ","")
    if len(uuid) < 8:
        return {"connected": False, "message": "UUID missing — need 8 hex chars e.g. 4BD95C53"}
    ok = kmbox_controller.connect()
    if ok:
        return {"connected": True, "message": f"Connected {cfg['ip']}:{cfg['port']}"}
    return {"connected": False, "message": "Could not connect — check IP/Port/UUID"}

@app.get("/config-files")
async def get_config_files():
    return {"files": list_config_files(), "current": app_state.get_current_config_file()}

@app.post("/config-files")
async def create_cfg_file(req: ConfigFileRequest):
    f = create_config_file(req.filename)
    return {"message": f"'{f}' created.", "files": list_config_files()}

@app.post("/config-files/switch")
async def switch_cfg_file(req: ConfigFileRequest):
    f = req.filename if req.filename.endswith('.json') else req.filename + '.json'
    if not os.path.exists(get_config_path(f)): raise HTTPException(404, "Not found.")
    app_state.set_current_config_file(f)
    return {"current_config_file": f, "guns": read_configs(f)}

@app.delete("/config-files/{filename}")
async def delete_cfg_file(filename: str):
    delete_config_file(filename)
    return {"message": "Deleted.", "files": list_config_files()}

@app.get("/configs")
async def get_configs():
    return read_configs(app_state.get_current_config_file())

@app.post("/configs")
async def save_config(config: GunConfig):
    cf   = app_state.get_current_config_file()
    cfgs = read_configs(cf)
    key  = config.name.strip()
    if not key: raise HTTPException(400, "Name cannot be empty.")
    entry: dict = {
        "name":                   config.name,
        "tags":                   config.tags or {},
        "pull_down":              config.pull_down_value,
        "vertical_delay_ms":      config.vertical_delay_ms,
        "vertical_duration_ms":   config.vertical_duration_ms,
        "horizontal":             config.horizontal_value,
        "horizontal_delay_ms":    config.horizontal_delay_ms,
        "horizontal_duration_ms": config.horizontal_duration_ms,
    }
    if config.pull_down_curve  is not None: entry["pull_down_curve"]  = config.pull_down_curve
    if config.horizontal_curve is not None: entry["horizontal_curve"] = config.horizontal_curve
    if config.hip_pull_down    is not None: entry["hip_pull_down"]    = config.hip_pull_down
    if config.hip_horizontal   is not None: entry["hip_horizontal"]   = config.hip_horizontal
    cfgs[key] = entry
    write_configs(cfgs, cf)
    return {"message": f"Saved '{key}'", "key": key}

@app.delete("/configs/{key:path}")
async def delete_config(key: str):
    cf   = app_state.get_current_config_file()
    cfgs = read_configs(cf)
    if key not in cfgs: raise HTTPException(404, "Config not found.")
    del cfgs[key]
    write_configs(cfgs, cf)
    return {"message": "Deleted."}

@app.post("/humanize")
async def set_humanize(cfg: HumanizeConfig):
    app_state.set_jitter(cfg.jitter_strength)
    app_state.set_smooth(cfg.smooth_factor)
    return {"jitter_strength": app_state.get_jitter(), "smooth_factor": app_state.get_smooth()}

@app.post("/rapid-fire")
async def set_rapid_fire(cfg: RapidFireConfig):
    app_state.set_rapid_fire(cfg.enabled, cfg.interval_ms)
    en, ms = app_state.get_rapid_fire()
    return {"rapid_fire_enabled": en, "rapid_fire_interval_ms": ms}

@app.post("/hip-fire")
async def set_hip_fire(cfg: HipFireConfig):
    app_state.set_hip_fire(cfg.enabled, cfg.pull_down, cfg.horizontal)
    en, pd, hz = app_state.get_hip_fire()
    return {"hip_fire_enabled": en, "hip_pull_down": pd, "hip_horizontal": hz}

class BeepConfig(BaseModel):
    enabled: bool = True

class WeaponSlotAssign(BaseModel):
    slot:        int            = Field(..., ge=1, le=5)
    config_name: Optional[str] = None   # None / "" = clear slot

class WeaponSlotEnabled(BaseModel):
    enabled: bool

class WeaponSlotRF(BaseModel):
    slot:        int            = Field(..., ge=1, le=5)
    enabled:     Optional[bool] = None  # None = clear override (inherit global)
    interval_ms: Optional[int]  = None

@app.post("/beep")
async def set_beep(cfg: BeepConfig):
    app_state.set_beep(cfg.enabled)
    return {"beep_enabled": app_state.get_beep()}


# ── Weapon Slot endpoints ─────────────────────────────────────────────────────
@app.get("/weapon-slots")
async def get_weapon_slots():
    return {
        "enabled":     weapon_slot_mgr.get_enabled(),
        "active_slot": weapon_slot_mgr.get_active_slot(),
        "slots":       weapon_slot_mgr.get_slots(),
        "slot_rf":     weapon_slot_mgr.get_slot_rf(),
    }

@app.post("/weapon-slots/enabled")
async def set_weapon_slots_enabled(cfg: WeaponSlotEnabled):
    weapon_slot_mgr.set_enabled(cfg.enabled)
    return {"enabled": weapon_slot_mgr.get_enabled()}

@app.post("/weapon-slots/assign")
async def assign_weapon_slot(req: WeaponSlotAssign):
    ok = weapon_slot_mgr.set_slot(req.slot, req.config_name)
    if not ok:
        raise HTTPException(400, "Slot must be 1–5")
    return {"slot": req.slot, "config_name": req.config_name, "slots": weapon_slot_mgr.get_slots()}

@app.post("/weapon-slots/assign-rf")
async def assign_weapon_slot_rf(req: WeaponSlotRF):
    ok = weapon_slot_mgr.set_slot_rf(req.slot, req.enabled, req.interval_ms)
    if not ok:
        raise HTTPException(400, "Slot must be 1–5")
    return {"slot": req.slot, "slot_rf": weapon_slot_mgr.get_slot_rf()}

@app.post("/weapon-slots/activate/{slot}")
async def activate_weapon_slot(slot: int):
    if slot not in range(1, 6):
        raise HTTPException(400, "Slot must be 1–5")
    ok = weapon_slot_mgr.activate_slot(slot)
    if not ok:
        raise HTTPException(404, f"Slot {slot} has no config assigned or weapon slots disabled")
    return {"activated": slot, "active_slot": weapon_slot_mgr.get_active_slot()}


# ══════════════════════════════════════════════════════════════════════════════
#  KMBox Auto-Reconnect (v8.0)
#  Runs in background, silently reconnects when KMBox drops without user action
# ══════════════════════════════════════════════════════════════════════════════
def _kmbox_watchdog():
    _backoff = 2.0
    while True:
        try:
            time.sleep(_backoff)
            if app_state.get_controller_type() != "kmbox":
                _backoff = 2.0
                continue
            if kmbox_controller.is_connected():
                _backoff = 2.0
                continue
            cfg  = app_state.get_kmbox_config()
            uuid = cfg["uuid"].replace("-","").replace(" ","")
            if len(uuid) < 8:
                _backoff = min(_backoff * 2, 30.0)
                continue
            print(f"[WATCHDOG] KMBox disconnected — reconnecting ({cfg['ip']})…")
            ok = kmbox_controller.connect()
            if ok:
                print("[WATCHDOG] KMBox reconnected ✓")
                _backoff = 2.0
            else:
                _backoff = min(_backoff * 2, 30.0)
                print(f"[WATCHDOG] Reconnect failed — retry in {_backoff:.0f}s")
        except Exception as e:
            print(f"[WATCHDOG] {e}")
            _backoff = min(_backoff * 2, 30.0)

threading.Thread(target=_kmbox_watchdog, daemon=True, name="KMBox_Watchdog").start()




# ══════════════════════════════════════════════════════════════════════════════
#  Macro System (v8.0)
#  Record mouse-movement sequences and replay them on demand.
#  Storage: configs/macros.json  (list of named macro objects)
#  Each macro: {"name": str, "key": str|null, "steps": [{"dx","dy","dt_ms"}]}
# ══════════════════════════════════════════════════════════════════════════════
MACROS_FILE = os.path.join(_data_root, "macros.json")

# ── In-memory macro cache — avoids disk I/O inside the 60Hz hotkey loop ──────
_macros_cache: dict = {}
_macros_cache_lock = threading.Lock()

def _read_macros() -> dict:
    with _macros_cache_lock:
        return dict(_macros_cache)

def _load_macros_from_disk() -> dict:
    """Read from disk and update cache. Called once on startup and after every write."""
    try:
        with open(MACROS_FILE) as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    with _macros_cache_lock:
        _macros_cache.clear()
        _macros_cache.update(data)
    return data

def _write_macros(macros: dict):
    tmp = MACROS_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh: json.dump(macros, fh, indent=2)
        os.replace(tmp, MACROS_FILE)
    except OSError as e:
        print(f"[MACRO] Write error: {e}")
        return
    # Update in-memory cache after successful write
    with _macros_cache_lock:
        _macros_cache.clear()
        _macros_cache.update(macros)

# Populate cache at startup
_load_macros_from_disk()

# ── Macro Record Key — hotkey to start/stop recording without clicking UI ─────
_macro_record_key: str = ""          # e.g. "F12" — empty = disabled
_macro_record_key_lock = threading.Lock()

# Shared state for in-progress keyboard-triggered recording session
_rec_pending_save: dict = {
    "active":      False,
    "name":        "",
    "trigger_key": None,   # hotkey to assign to the saved macro
    "loop":        False,
}

# Valid keys for record trigger (must not overlap with slot keys 1-5)
_MACRO_RECORD_VALID_KEYS = {f"F{i}" for i in range(1, 13)} | {
    "INS", "DEL", "HOME", "END", "PGUP", "PGDN"
}

def get_macro_record_key() -> str:
    with _macro_record_key_lock: return _macro_record_key

def set_macro_record_key(key: str) -> str:
    global _macro_record_key
    with _macro_record_key_lock:
        _macro_record_key = key if key in _MACRO_RECORD_VALID_KEYS else ""
    return _macro_record_key

class MacroRecorder:
    """Thread-safe macro recorder + player.

    Step types:
      {"type":"move",  "dx":int, "dy":int,  "dt_ms":int}  — relative mouse move
      {"type":"click", "btn":str,"state":str,"dt_ms":int}  — mouse button (state: "down"|"up")
      {"type":"kdown", "key":str,            "dt_ms":int}  — keyboard key press
      {"type":"kup",   "key":str,            "dt_ms":int}  — keyboard key release
      {"type":"delay", "dt_ms":int}                        — explicit pause (manual insert via UI)
    """
    def __init__(self):
        self._lock      = threading.Lock()
        self._recording = False
        self._steps: list = []
        self._last_t    = 0.0
        self._playing   = set()
        # mouse hook thread handle
        self._mouse_hook_thread: threading.Thread | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _dt_ms(self) -> int:
        """Elapsed ms since last event. Must be called with self._lock held."""
        now = time.perf_counter()
        dt  = int((now - self._last_t) * 1000)
        self._last_t = now
        return max(0, dt)

    # ── Recording control ─────────────────────────────────────────────────────
    def start_recording(self):
        with self._lock:
            self._recording = True
            self._steps     = []
            self._last_t    = time.perf_counter()
        self._start_mouse_hook()

    def _start_mouse_hook(self):
        """Dedicated thread: polls raw mouse delta + button states every ~8ms."""
        if self._mouse_hook_thread and self._mouse_hook_thread.is_alive():
            return
        def _hook():
            if not _HAS_CTYPES:
                return
            try:
                user32 = ctypes.windll.user32
            except AttributeError:
                return

            # POINT struct for GetCursorPos
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            _BTN_VK = {"LMB": 0x01, "RMB": 0x02, "MMB": 0x04, "M4": 0x05, "M5": 0x06}
            prev_btn = {b: False for b in _BTN_VK}
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            prev_x, prev_y = pt.x, pt.y

            while True:
                with self._lock:
                    if not self._recording:
                        break

                # Mouse position delta
                user32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - prev_x
                dy = pt.y - prev_y
                prev_x, prev_y = pt.x, pt.y

                now_t = time.perf_counter()

                if dx != 0 or dy != 0:
                    with self._lock:
                        if self._recording:
                            dt = int((now_t - self._last_t) * 1000)
                            self._last_t = now_t
                            self._steps.append({
                                "type": "move", "dx": dx, "dy": dy,
                                "dt_ms": max(0, dt)
                            })

                # Mouse buttons
                for btn, vk in _BTN_VK.items():
                    pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if pressed != prev_btn[btn]:
                        with self._lock:
                            if self._recording:
                                dt = int((now_t - self._last_t) * 1000)
                                self._last_t = now_t
                                self._steps.append({
                                    "type": "click", "btn": btn,
                                    "state": "down" if pressed else "up",
                                    "dt_ms": max(0, dt)
                                })
                        prev_btn[btn] = pressed

                time.sleep(0.008)  # ~125 Hz

        self._mouse_hook_thread = threading.Thread(
            target=_hook, daemon=True, name="MacroMouseHook"
        )
        self._mouse_hook_thread.start()

    def record_key_down(self, key: str):
        with self._lock:
            if not self._recording: return
            self._steps.append({"type": "kdown", "key": key, "dt_ms": self._dt_ms()})

    def record_key_up(self, key: str):
        with self._lock:
            if not self._recording: return
            self._steps.append({"type": "kup", "key": key, "dt_ms": self._dt_ms()})

    # Compatibility helpers (used by older tests/callers)
    def record_move(self, dx: int, dy: int):
        with self._lock:
            if not self._recording: return
            self._steps.append({"type": "move", "dx": int(dx), "dy": int(dy), "dt_ms": self._dt_ms()})

    def stop_recording(self) -> list:
        with self._lock:
            self._recording = False
            return list(self._steps)

    def is_recording(self) -> bool:
        with self._lock: return self._recording

    def get_steps(self) -> list:
        with self._lock: return list(self._steps)

    # ── Playback ─────────────────────────────────────────────────────────────
    def play(self, name: str, steps: list, loop: bool = False):
        def _worker():
            with self._lock: self._playing.add(name)
            try:
                ctrl   = get_active_controller()
                user32 = None
                if _HAS_CTYPES:
                    try: user32 = ctypes.windll.user32
                    except: pass

                while True:
                    for step in steps:
                        with self._lock:
                            if name not in self._playing: return
                        dt    = max(0, int(step.get("dt_ms", 0))) / 1000.0
                        stype = step.get("type", "move")

                        if dt > 0:
                            time.sleep(dt)

                        if stype == "move":
                            ctrl.simple_move_mouse(
                                int(step.get("dx", 0)), int(step.get("dy", 0))
                            )
                        elif stype == "delay":
                            pass  # delay already consumed above
                        elif stype == "click" and user32:
                            btn   = step.get("btn", "LMB")
                            state = step.get("state", "down")
                            _CLICK_FLAGS = {
                                ("LMB","down"): 0x0002, ("LMB","up"): 0x0004,
                                ("RMB","down"): 0x0008, ("RMB","up"): 0x0010,
                                ("MMB","down"): 0x0020, ("MMB","up"): 0x0040,
                            }
                            flag = _CLICK_FLAGS.get((btn, state))
                            if flag:
                                user32.mouse_event(flag, 0, 0, 0, 0)
                        elif stype in ("kdown","kup") and user32:
                            vk = _MACRO_VK.get(step.get("key",""))
                            if vk:
                                flags = 0 if stype == "kdown" else 2
                                user32.keybd_event(vk, 0, flags, 0)

                    if not loop: break
            finally:
                with self._lock: self._playing.discard(name)

        threading.Thread(target=_worker, daemon=True, name=f"Macro_{name}").start()

    def stop(self, name: str):
        with self._lock: self._playing.discard(name)

    def stop_all(self):
        with self._lock: self._playing.clear()

    def get_playing(self) -> list:
        with self._lock: return list(self._playing)

macro_recorder = MacroRecorder()


# ── VK map — used by hotkey listener, keyboard recording, and playback ─────────
_MACRO_VK = {f"F{i}": 0x70 + (i-1) for i in range(1, 13)}
_MACRO_VK.update({
    "INS": 0x2D, "DEL": 0x2E, "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PGDN": 0x22,
    # Arrow keys
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    # Common keys
    "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12, "TAB": 0x09,
    "SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "BACKSPACE": 0x08,
    # Number row
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    # Letters A–Z
    **{chr(c): c for c in range(0x41, 0x5B)},
})

# Reverse map vk → name (for recording)
_VK_TO_NAME = {v: k for k, v in _MACRO_VK.items()}

# Keys that trigger macro hotkeys only (not recorded during keyboard capture)
_HOTKEY_ONLY_KEYS = {f"F{i}" for i in range(1, 13)} | {"INS", "DEL", "HOME", "END", "PGUP", "PGDN"}


def _macro_hotkey_listener():
    """Listens for macro trigger keys AND records keyboard input during recording.

    OPT v8.3:
    • Uses _read_macros() which now reads from in-memory cache (no disk I/O per tick).
    • Record key (e.g. F12) starts/stops recording without touching the UI.
    • Sleep reduced to 10ms (~100Hz) — imperceptible latency improvement.
    """
    if not _HAS_CTYPES: return
    try:
        user32 = ctypes.windll.user32
    except AttributeError: return

    prev = {vk: False for vk in _MACRO_VK.values()}

    while True:
        try:
            is_rec = macro_recorder.is_recording()
            macros = _read_macros()   # fast — reads from in-memory cache
            rec_key = get_macro_record_key()

            for key_name, vk in _MACRO_VK.items():
                pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was     = prev.get(vk, False)
                rising  = pressed and not was

                # ── Record key: start / stop-and-save recording ───────────────
                if rec_key and key_name == rec_key and rising:
                    if not is_rec:
                        macro_recorder.start_recording()
                        # generate a timestamped auto-name; trigger_key/loop filled in by UI via set_macro_record_key
                        _rec_pending_save["active"] = True
                        _rec_pending_save["name"] = f"rec_{int(time.time())}"
                        # trigger_key and loop are set by the UI when user clicks Set
                        print(f"[MACRO_HK] Recording started via key {rec_key}")
                    else:
                        steps = macro_recorder.stop_recording()
                        auto_name = _rec_pending_save.get("name") or f"rec_{int(time.time())}"
                        _rec_pending_save["active"] = False
                        all_macros = _read_macros()
                        all_macros[auto_name] = {
                            "name":  auto_name,
                            "key":   _rec_pending_save.get("trigger_key") or None,
                            "loop":  bool(_rec_pending_save.get("loop", False)),
                            "steps": steps,
                        }
                        _write_macros(all_macros)
                        print(f"[MACRO_HK] Recording saved as '{auto_name}' key={all_macros[auto_name]['key']} ({len(steps)} events)")
                    prev[vk] = pressed
                    continue

                if is_rec and key_name not in _HOTKEY_ONLY_KEYS and key_name != rec_key:
                    # ── Record keyboard events ────────────────────────────────
                    if rising:
                        macro_recorder.record_key_down(key_name)
                    elif not pressed and was:
                        macro_recorder.record_key_up(key_name)
                else:
                    # ── Trigger macro on rising edge ──────────────────────────
                    if rising:
                        for name, macro in macros.items():
                            if macro.get("key") != key_name: continue
                            steps = macro.get("steps", [])
                            loop  = bool(macro.get("loop", False))
                            if name in macro_recorder.get_playing():
                                macro_recorder.stop(name)
                            else:
                                macro_recorder.play(name, steps, loop)

                prev[vk] = pressed

        except Exception as e:
            print(f"[MACRO_HK] {e}")
        time.sleep(0.010)  # ~100Hz — snappier key detection, still CPU-light

threading.Thread(target=_macro_hotkey_listener, daemon=True, name="MacroHotkey").start()


class MacroSave(BaseModel):
    name:  str
    key:   Optional[str] = None
    loop:  bool = False
    steps: List[dict]

class MacroPlay(BaseModel):
    name: str
    loop: bool = False

class MacroStop(BaseModel):
    name: Optional[str] = None  # None = stop all


# ── Config Export / Import endpoints (v8.0) ────────────────────────────────────
@app.get("/export")
async def export_configs():
    """Download all profile JSON files + settings.json as a ZIP archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in list_config_files():
            fp = get_config_path(fname)
            if os.path.exists(fp):
                zf.write(fp, arcname=f"configs/{fname}")
        if os.path.exists(SETTINGS_FILE):
            zf.write(SETTINGS_FILE, arcname="settings.json")
        if os.path.exists(MACROS_FILE):
            zf.write(MACROS_FILE, arcname="macros.json")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=rvn_backup.zip"}
    )

class ImportRequest(BaseModel):
    data:  str  = ""
    merge: bool = True

@app.post("/import")
async def import_configs(req: ImportRequest):
    """
    Import configs from a base64-encoded ZIP.
    Body: {"data": "<base64 zip>", "merge": true|false}
    merge=true  → keeps existing configs, only overwrites matching keys
    merge=false → replaces entire profile file content
    """
    import base64
    # FIX v8.1: was typed as `dict` which causes FastAPI to reject the body
    # unless Content-Type is application/json AND the raw dict is passed.
    # Now uses a proper Pydantic model for reliable body parsing.
    try:
        raw_bytes = base64.b64decode(req.data)
    except Exception:
        raise HTTPException(400, "Invalid base64 data")
    merge = req.merge
    try:
        buf = io.BytesIO(raw_bytes)
        imported = {"profiles": [], "settings": False, "macros": False}
        with zipfile.ZipFile(buf, "r") as zf:
            for name in zf.namelist():
                if name.startswith("configs/") and name.endswith(".json"):
                    fname = os.path.basename(name)
                    if not fname: continue
                    content = json.loads(zf.read(name))
                    if merge:
                        existing = read_configs(fname)
                        existing.update(content)
                        write_configs(existing, fname)
                    else:
                        write_configs(content, fname)
                    imported["profiles"].append(fname)
                elif name == "settings.json":
                    # Never blindly overwrite settings — just note it was present
                    imported["settings"] = True
                elif name == "macros.json":
                    content = json.loads(zf.read(name))
                    existing = _read_macros() if merge else {}
                    existing.update(content)
                    _write_macros(existing)
                    imported["macros"] = True
        return {"imported": imported}
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a valid ZIP file")
    except Exception as e:
        raise HTTPException(500, str(e))




# ── Macro endpoints ────────────────────────────────────────────────────────────
@app.get("/macros")
async def list_macros():
    return {"macros": _read_macros(), "playing": macro_recorder.get_playing(),
            "recording": macro_recorder.is_recording()}

@app.post("/macros")
async def save_macro(req: MacroSave):
    if not req.name.strip(): raise HTTPException(400, "Name required")
    macros = _read_macros()
    macros[req.name] = {
        "name": req.name, "key": req.key,
        "loop": req.loop, "steps": req.steps
    }
    _write_macros(macros)
    return {"saved": req.name}

@app.delete("/macros/{name}")
async def delete_macro(name: str):
    macros = _read_macros()
    if name not in macros: raise HTTPException(404, "Macro not found")
    del macros[name]
    _write_macros(macros)
    return {"deleted": name}

@app.post("/macros/record/start")
async def macro_record_start():
    macro_recorder.start_recording()
    return {"recording": True}

@app.post("/macros/record/stop")
async def macro_record_stop(req: MacroSave):
    steps = macro_recorder.stop_recording()
    if not req.name.strip(): raise HTTPException(400, "Name required")
    macros = _read_macros()
    macros[req.name] = {"name": req.name, "key": req.key, "loop": req.loop, "steps": steps}
    _write_macros(macros)
    return {"saved": req.name, "steps": len(steps)}

@app.post("/macros/play")
async def play_macro(req: MacroPlay):
    macros = _read_macros()
    if req.name not in macros: raise HTTPException(404, "Macro not found")
    macro_recorder.play(req.name, macros[req.name].get("steps", []), req.loop)
    return {"playing": req.name}

@app.post("/macros/stop")
async def stop_macro(req: MacroStop):
    if req.name: macro_recorder.stop(req.name)
    else:        macro_recorder.stop_all()
    return {"stopped": req.name or "all"}

@app.get("/macros/record/status")
async def macro_record_status():
    """Live step count while recording — polled by UI."""
    return {
        "recording": macro_recorder.is_recording(),
        "steps":     len(macro_recorder.get_steps()),
    }

@app.post("/macros/record/discard")
async def macro_record_discard():
    """Abort recording without saving."""
    macro_recorder.stop_recording()
    return {"recording": False}

class MacroStepsUpdate(BaseModel):
    steps: List[dict]

class MacroRename(BaseModel):
    new_name: str

class MacroDuplicate(BaseModel):
    new_name: str

class MacroRecordKeyConfig(BaseModel):
    key:         Optional[str]  = None   # None or "" = disable record hotkey
    trigger_key: Optional[str]  = None   # hotkey assigned to the saved macro
    loop:        bool           = False  # whether saved macro loops

@app.get("/macros/record-key")
async def get_record_key():
    return {
        "record_key":  get_macro_record_key(),
        "trigger_key": _rec_pending_save.get("trigger_key") or None,
        "loop":        bool(_rec_pending_save.get("loop", False)),
    }

@app.post("/macros/record-key")
async def set_record_key(cfg: MacroRecordKeyConfig):
    k = set_macro_record_key(cfg.key or "")
    # Store trigger_key and loop so the hotkey listener can use them on stop
    _rec_pending_save["trigger_key"] = cfg.trigger_key or None
    _rec_pending_save["loop"]        = bool(cfg.loop)
    return {
        "record_key":  k,
        "trigger_key": _rec_pending_save["trigger_key"],
        "loop":        _rec_pending_save["loop"],
    }

@app.put("/macros/{name}/steps")
async def update_macro_steps(name: str, req: MacroStepsUpdate):
    """Replace steps for an existing macro (used by delay editor)."""
    macros = _read_macros()
    if name not in macros: raise HTTPException(404, "Macro not found")
    macros[name]["steps"] = req.steps
    _write_macros(macros)
    return {"saved": name, "steps": len(req.steps)}


@app.get("/macros/{name}/export")
async def export_macro(name: str):
    """Export a single macro as JSON."""
    macros = _read_macros()
    if name not in macros:
        raise HTTPException(404, "Macro not found")
    return {"macro": macros[name]}


@app.post("/macros/import")
async def import_macro(req: MacroSave):
    """Import a single macro (same payload shape as MacroSave)."""
    if not req.name.strip():
        raise HTTPException(400, "Name required")
    macros = _read_macros()
    macros[req.name] = {"name": req.name, "key": req.key, "loop": req.loop, "steps": req.steps}
    _write_macros(macros)
    return {"imported": req.name}


@app.post("/macros/{name}/rename")
async def rename_macro(name: str, req: MacroRename):
    macros = _read_macros()
    if name not in macros:
        raise HTTPException(404, "Macro not found")
    nn = (req.new_name or "").strip()
    if not nn:
        raise HTTPException(400, "New name required")
    if nn in macros and nn != name:
        raise HTTPException(409, "Macro name already exists")
    macro = macros.pop(name)
    macro["name"] = nn
    macros[nn] = macro
    _write_macros(macros)
    return {"renamed": name, "saved": nn}


@app.post("/macros/{name}/duplicate")
async def duplicate_macro(name: str, req: MacroDuplicate):
    macros = _read_macros()
    if name not in macros:
        raise HTTPException(404, "Macro not found")
    nn = (req.new_name or "").strip()
    if not nn:
        raise HTTPException(400, "New name required")
    if nn in macros:
        raise HTTPException(409, "Macro name already exists")
    src = macros[name]
    macros[nn] = {"name": nn, "key": src.get("key"), "loop": bool(src.get("loop", False)), "steps": list(src.get("steps", []))}
    _write_macros(macros)
    return {"duplicated": name, "saved": nn}


# ══════════════════════════════════════════════════════════════════════════════
#  UI  v8.0
# ══════════════════════════════════════════════════════════════════════════════



@app.get("/", response_class=HTMLResponse)
async def ui():
    if not TEMPLATE_FILE.exists():
        raise HTTPException(
            500,
            f"UI template missing: {TEMPLATE_FILE}. Ensure templates/ is bundled with the build.",
        )
    return HTMLResponse(TEMPLATE_FILE.read_text(encoding="utf-8"))


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


if __name__ == "__main__":
    # ── Unit test runner  (python rvn.py --test) ──────────────────────────────
    if "--test" in sys.argv:
        import unittest, struct as _ts

        class TestAppState(unittest.TestCase):
            def setUp(self):
                self.s = AppState()

            def test_pull_down_clamp(self):
                self.s.set_active_value(999); self.assertEqual(self.s.get_active_value(), 300)
                self.s.set_active_value(-1);  self.assertEqual(self.s.get_active_value(), 0)
                self.s.set_active_value(5);   self.assertEqual(self.s.get_active_value(), 5)

            def test_horizontal_clamp(self):
                self.s.set_horizontal_value(999);  self.assertEqual(self.s.get_horizontal_value(), 300)
                self.s.set_horizontal_value(-999); self.assertEqual(self.s.get_horizontal_value(), -300)

            def test_toggle(self):
                self.assertFalse(self.s.get_enabled())
                self.s.toggle_enabled()
                self.assertTrue(self.s.get_enabled())
                self.s.toggle_enabled()
                self.assertFalse(self.s.get_enabled())

            def test_toggle_button_validation(self):
                self.assertIsNone(self.s.set_toggle_button("INVALID"))
                self.assertEqual(self.s.set_toggle_button("M4"), "M4")

            def test_controller_type_validation(self):
                self.assertIsNone(self.s.set_controller_type("joystick"))
                self.assertEqual(self.s.set_controller_type("kmbox"), "kmbox")

            def test_rapid_fire_clamp(self):
                self.s.set_rapid_fire(True, 5)
                en, ms = self.s.get_rapid_fire()
                self.assertTrue(en); self.assertEqual(ms, 30)   # min clamp
                self.s.set_rapid_fire(True, 9999)
                _, ms = self.s.get_rapid_fire()
                self.assertEqual(ms, 2000)

            def test_global_rf_preserved_by_slot(self):
                self.s.set_rapid_fire(True, 150)
                self.s.set_rapid_fire(False, 80, from_slot=True)  # slot override
                g_en, g_ms = self.s.get_global_rapid_fire()
                self.assertTrue(g_en); self.assertEqual(g_ms, 150)

            def test_hip_fire_clamp(self):
                self.s.set_hip_fire(True, 999, -999)
                _, pd, hz = self.s.get_hip_fire()
                self.assertEqual(pd, 300); self.assertEqual(hz, -300)

            def test_persist_roundtrip(self):
                self.s.set_rapid_fire(True, 75)
                d = self.s.to_settings()
                s2 = AppState()
                s2.from_settings(d)
                en, ms = s2.get_rapid_fire()
                self.assertTrue(en); self.assertEqual(ms, 75)

        class TestKMBoxPacket(unittest.TestCase):
            def _make_ctrl(self):
                c = KMBoxController()
                c._mac  = b'\x01\x02\x03\x04'
                c._rand = 0xDEADBEEF
                c._seq  = 0
                return c

            def test_packet_length(self):
                c = self._make_ctrl()
                pkt = c._build(KMBoxController.CMD_MOVE, b'\x00'*8)
                self.assertEqual(len(pkt), 64)

            def test_packet_mac(self):
                c = self._make_ctrl()
                pkt = c._build(KMBoxController.CMD_CONNECT, b'\x00'*4)
                self.assertEqual(pkt[:4], b'\x01\x02\x03\x04')

            def test_packet_sequence_increment(self):
                c = self._make_ctrl()
                p1 = c._build(KMBoxController.CMD_MOVE, b'\x00'*8)
                p2 = c._build(KMBoxController.CMD_MOVE, b'\x00'*8)
                seq1 = _ts.unpack_from('<I', p1, 8)[0]
                seq2 = _ts.unpack_from('<I', p2, 8)[0]
                self.assertEqual(seq2, seq1 + 1)

            def test_packet_command(self):
                c = self._make_ctrl()
                pkt = c._build(KMBoxController.CMD_MONITOR, b'\x00'*4)
                cmd = _ts.unpack_from('<I', pkt, 12)[0]
                self.assertEqual(cmd, KMBoxController.CMD_MONITOR)

            def test_move_payload(self):
                c = self._make_ctrl()
                payload = _ts.pack('<hhhB', 5, -3, 0, 0)
                pkt = c._build(KMBoxController.CMD_MOVE, payload)
                x, y = _ts.unpack_from('<hh', pkt, 16)
                self.assertEqual(x, 5); self.assertEqual(y, -3)

        class TestWeaponSlotManager(unittest.TestCase):
            def setUp(self):
                self.m = WeaponSlotManager()

            def test_slot_assign_and_get(self):
                self.m.set_slot(1, "AK-47")
                self.assertEqual(self.m.get_slots()[1], "AK-47")

            def test_slot_clear(self):
                self.m.set_slot(2, "M4A1")
                self.m.set_slot(2, None)
                self.assertIsNone(self.m.get_slots()[2])

            def test_invalid_slot(self):
                self.assertFalse(self.m.set_slot(0, "gun"))
                self.assertFalse(self.m.set_slot(6, "gun"))

            def test_slot_rf_inherit(self):
                self.m.set_slot_rf(1, None, None)
                rf = self.m.get_slot_rf()[1]
                self.assertIsNone(rf)

            def test_slot_rf_set(self):
                self.m.set_slot_rf(3, True, 80)
                rf = self.m.get_slot_rf()[3]
                self.assertTrue(rf["enabled"]); self.assertEqual(rf["interval_ms"], 80)

        class TestMacroRecorder(unittest.TestCase):
            def test_record_stop(self):
                mr = MacroRecorder()
                mr.start_recording()
                self.assertTrue(mr.is_recording())
                mr.record_move(5, -3)
                mr.record_move(0, 2)
                steps = mr.stop_recording()
                self.assertFalse(mr.is_recording())
                self.assertEqual(len(steps), 2)
                self.assertEqual(steps[0]["dx"], 5)
                self.assertEqual(steps[1]["dy"], 2)

        # Windows console codepages (e.g. cp874) may not support box-drawing chars.
        print("\n== RVN v8.2 Unit Tests ==")
        loader = unittest.TestLoader()
        suite  = unittest.TestSuite()
        for cls in [TestAppState, TestKMBoxPacket, TestWeaponSlotManager,
                    TestMacroRecorder]:
            suite.addTests(loader.loadTestsFromTestCase(cls))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    # ── Normal server startup ──────────────────────────────────────────────────
    ip = get_local_ip()
    info_lines = [
        "RVN v8.0",
        "Local  : http://localhost:8000",
        f"Network: http://{ip}:8000",
    ]
    if _is_packaged_runtime():
        info_lines.append(f"Configs: {CONFIG_DIR}")
        info_lines.append(f"Settings: {SETTINGS_FILE}")

    width = max(len(line) for line in info_lines)
    border = "+" + ("-" * (width + 2)) + "+"

    print()
    print(border)
    for line in info_lines:
        print(f"| {line.ljust(width)} |")
    print(border)
    print()
    if _is_packaged_runtime():
        import webbrowser

        def _open_browser():
            time.sleep(1.25)
            webbrowser.open("http://127.0.0.1:8000/")

        threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")