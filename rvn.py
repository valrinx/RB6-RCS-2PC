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

Changes from v8.1 (v8.2):
  • REMOVED: Weapon Auto-Detect (Pixel Color) — PixelDetector class, all
    /pixel-detect/* endpoints, PixelDetectConfig model, and UI card removed.
  • FIX: Recoil loop restored to correct v8.0 behavior — attempts to add
    real-time sensitivity scaling to the control loop introduced a deadlock
    (threading.Lock non-reentrant) then a NameError (_sens_scale undefined),
    both silently caught by the loop's except clause, causing recoil to stop
    working entirely. The loop is now identical to v8.0.
  • CHANGED: Sensitivity Scaling is now a client-side calculator only.
    Enter your DPI + in-game sens + a reference pull-down value, click
    Calculate, and apply the result manually to the pull-down slider.
    This matches the original design intent (pre-compute, then save config)
    and avoids any runtime interference with the recoil loop.
  • FIX: /import endpoint now uses a Pydantic model (ImportRequest) instead
    of raw dict — FastAPI was silently rejecting requests in strict mode.
  • FIX: Removed dead AppState._auto_persist inner function.

Changes from v7.1 (v8.0):
  • NEW: Persistent settings — AppState saved to settings.json on every change,
    restored automatically on startup (controller type, kmbox IP/port/uuid,
    toggle button, trigger mode, beep, rapid fire, hip fire, humanize, weapon slots)
  • NEW: KMBox auto-reconnect — background thread detects disconnect and reconnects
    automatically with exponential backoff (no more manual reconnect button needed)
  • OPT: KMBox monitor now queries all buttons in a single bitmask UDP packet
    instead of 5 separate queries per poll (~5x less UDP traffic, ~40ms → ~8ms latency)
  • NEW: Config Import/Export — download all profiles as a ZIP, import ZIP to restore
  • NEW: Curve Visualizer — realtime animated preview of pull-down curve while firing
  • NEW: Sensitivity Scaling — enter DPI + in-game sens, pull-down auto-scales
  • NEW: Macro system — record mouse-move sequences and replay them on a key
  • NEW: Unit tests — run python rvn.py --test to validate AppState + KMBox packets

Changes from v7.0 (v7.1):
  • FIX: Per-slot Rapid Fire no longer bleeds into slots that have RF off

Changes from v6.0 (v7.0):
  • FIX: Weapon Slot dropdowns now correctly show saved gun configs
  • NEW: Per-slot Rapid Fire
"""

import threading
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
import sys, json, os, socket, time, random, shutil, zipfile, io
from contextlib import asynccontextmanager

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
    legacy_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "configs")
    if not os.path.isdir(legacy_dir):
        return
    try:
        if any(f.endswith(".json") for f in os.listdir(CONFIG_DIR)):
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
        return
    try:
        if "app" in d:
            app_state.from_settings(d["app"])
            print("[SETTINGS] App settings restored")
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


# ══════════════════════════════════════════════════════════════════════════════
#  UI  v8.0
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>RVN v8.3</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#09090c;--sf:#111116;--bd:#1c1c24;--bd2:#252530;
  --tx:#c0c0d0;--mu:#3a3a4c;
  --ac:#5bf0a0;--ac2:#3cd880;--bl:#4ea8ff;--rd:#ff4d6a;--yl:#ffc84a;--vi:#c084fc;--or:#ff9944;
  --mo:'JetBrains Mono',monospace;--sa:'Inter',system-ui,sans-serif;
  --r:10px;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:var(--sa);background:var(--bg);color:var(--tx);min-height:100vh;
  display:flex;justify-content:center;padding:28px 16px 80px;
  background-image:
    radial-gradient(ellipse 70% 35% at 50% -8%,rgba(91,240,160,.055) 0%,transparent 70%),
    radial-gradient(ellipse 45% 28% at 90% 110%,rgba(78,168,255,.04) 0%,transparent 60%);}
.w{max-width:455px;width:100%;}
.hdr{display:flex;align-items:baseline;gap:10px;margin-bottom:22px;}
.logo{font-family:var(--mo);font-size:1.9rem;font-weight:700;color:#fff;letter-spacing:-2px;}
.logo em{color:var(--ac);font-style:normal;}
.vtag{font-family:var(--mo);font-size:.57rem;color:var(--mu);border:1px solid var(--bd2);padding:2px 8px;border-radius:4px;letter-spacing:1px;}
.conn-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--mu);margin-left:auto;transition:background .4s,box-shadow .4s;}
.conn-dot.ok{background:var(--ac);box-shadow:0 0 7px rgba(91,240,160,.5);}
.conn-dot.bad{background:var(--rd);box-shadow:0 0 7px rgba(255,77,106,.35);}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:15px 17px;margin-bottom:8px;transition:border-color .2s;}
.card:hover{border-color:var(--bd2);}
.clabel{font-family:var(--mo);font-size:.57rem;letter-spacing:2px;text-transform:uppercase;color:var(--mu);margin-bottom:11px;display:flex;align-items:center;flex-wrap:wrap;gap:6px;}
#toggle-btn{width:100%;padding:13px;border-radius:8px;font-family:var(--mo);font-size:.9rem;font-weight:700;letter-spacing:3px;cursor:pointer;border:1.5px solid var(--bd2);background:var(--sf);color:var(--mu);transition:all .3s;}
#toggle-btn.enabled{background:#03120a;border-color:#195230;color:var(--ac);box-shadow:0 0 28px rgba(91,240,160,.1);}
#toggle-btn.disabled{background:#130408;border-color:#521428;color:var(--rd);}
#toggle-btn:active{transform:scale(.98);}
.trow{display:flex;align-items:center;gap:8px;margin-top:9px;font-size:.75rem;color:var(--mu);}
.tabs{display:flex;border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin-bottom:8px;}
.tab{flex:1;padding:10px 4px;background:var(--sf);border:none;color:var(--mu);font-family:var(--mo);font-size:.57rem;letter-spacing:1.5px;text-transform:uppercase;cursor:pointer;transition:all .2s;}
.tab:not(:first-child){border-left:1px solid var(--bd);}
.tab.active{background:var(--bd2);color:#fff;}
.tc{display:none;}.tc.active{display:block;}
select,input[type=text]{background:var(--bg);border:1px solid var(--bd2);border-radius:7px;color:var(--tx);padding:8px 11px;font-size:.84rem;font-family:var(--sa);outline:none;transition:border-color .2s;width:100%;}
select:focus,input[type=text]:focus{border-color:#2a4060;}
select{cursor:pointer;}
#tbs{width:auto;padding:4px 8px;font-size:.8rem;background:var(--bg);border:1px solid var(--bd2);border-radius:5px;color:var(--mu);}
.num{background:transparent;border:1px solid transparent;border-radius:6px;color:#fff;font-family:var(--mo);font-size:1.7rem;font-weight:700;width:100%;padding:0 4px 2px;outline:none;-moz-appearance:textfield;transition:border-color .2s,background .2s;}
.num::-webkit-outer-spin-button,.num::-webkit-inner-spin-button{-webkit-appearance:none;margin:0;}
.num:hover{border-color:var(--bd2);}.num:focus{border-color:#2a4060;background:#0a1018;}
.num-sm{font-size:1.15rem !important;}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;border-radius:2px;background:var(--bd2);outline:none;}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#fff;cursor:pointer;transition:transform .15s,box-shadow .15s;}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.2);box-shadow:0 0 10px rgba(255,255,255,.28);}
input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;background:#fff;cursor:pointer;border:none;}
.hint{font-size:.68rem;color:var(--mu);margin-top:6px;line-height:1.6;}
.btn{padding:8px 13px;border:1px solid var(--bd2);border-radius:7px;font-family:var(--mo);font-size:.68rem;font-weight:700;letter-spacing:.7px;cursor:pointer;transition:all .2s;background:var(--sf);color:#bbb;}
.btn:hover{background:var(--bd2);color:#fff;transform:translateY(-1px);}.btn:active{transform:scale(.98);}
.btn:disabled{opacity:.25;cursor:not-allowed;transform:none;pointer-events:none;}
.btn-s{background:#06091a;border-color:#181a4a;color:#88aaff;}.btn-s:hover{background:#0c1030;}
.btn-d{background:#150508;border-color:#4a1018;color:#ff7070;}.btn-d:hover{background:#200810;}
.btn-g{background:#031008;border-color:#0d3020;color:var(--ac);}.btn-g:hover{background:#071a10;}
.btn-or{background:#120804;border-color:#3a1e08;color:var(--or);}.btn-or:hover{background:#1e0e04;}
.row{display:flex;gap:8px;align-items:center;}.row>*{min-width:0;}
.sdiv{font-family:var(--mo);font-size:.54rem;letter-spacing:2px;text-transform:uppercase;color:var(--mu);margin:12px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--bd);}
.tgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.tgrid-lbl{font-size:.62rem;color:var(--mu);margin-bottom:4px;font-family:var(--mo);letter-spacing:.4px;text-transform:uppercase;}
.hrow{display:flex;align-items:center;gap:12px;margin-bottom:9px;}
.hlbl{font-size:.75rem;color:var(--tx);min-width:58px;}
.hval{font-family:var(--mo);font-size:.76rem;color:var(--ac);min-width:36px;text-align:right;}
.cbadge{display:inline-block;padding:3px 9px;border-radius:4px;font-family:var(--mo);font-size:.6rem;margin-top:6px;}
.cbadge.sw{background:#030c16;border:1px solid #0a2030;color:var(--bl);}
.cbadge.km{background:#120e04;border:1px solid #403a14;color:var(--yl);}
.cbadge.mk{background:#031009;border:1px solid #0b2a16;color:var(--ac);}
.cpill{display:inline-flex;align-items:center;gap:4px;background:#031210;border:1px solid #0a3020;color:var(--ac);font-family:var(--mo);font-size:.54rem;padding:2px 7px;border-radius:8px;}
.ceditor{background:var(--bg);border:1px solid var(--bd2);border-radius:8px;padding:10px;margin-top:10px;}
#curve-canvas{width:100%;height:100px;display:block;border-radius:4px;cursor:crosshair;}
/* v8.0 Curve Visualizer */
#curve-viz{width:100%;height:80px;display:block;border-radius:6px;background:#06090e;border:1px solid var(--bd2);margin-top:8px;}
.viz-wrap{position:relative;}
.viz-label{font-family:var(--mo);font-size:.54rem;color:var(--mu);position:absolute;top:4px;left:8px;letter-spacing:1px;text-transform:uppercase;}
.viz-tick{font-family:var(--mo);font-size:.52rem;color:#22ee7a;position:absolute;top:4px;right:8px;}
/* Macro cards */
.macro-item{display:flex;align-items:center;gap:7px;padding:7px 10px;background:var(--bg);border:1px solid var(--bd);border-radius:7px;margin-bottom:5px;}
.macro-name{font-family:var(--mo);font-size:.72rem;color:#fff;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.macro-key{font-family:var(--mo);font-size:.6rem;color:var(--vi);background:#0e091a;border:1px solid #2a1a4a;border-radius:4px;padding:2px 6px;}
.macro-steps{font-size:.6rem;color:var(--mu);}
.macro-playing{color:var(--or);animation:pulse .7s ease-in-out infinite alternate;}
@keyframes pulse{from{opacity:.5}to{opacity:1}}
/* Export/Import */
.io-row{display:flex;gap:8px;margin-bottom:8px;}
.cacts{display:flex;gap:6px;margin-top:7px;align-items:center;flex-wrap:wrap;}
.tag-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;min-height:22px;}
.tag-chip{display:inline-flex;align-items:center;gap:3px;background:#0a0a14;border:1px solid var(--bd2);border-radius:20px;padding:3px 10px;font-size:.7rem;color:#aaa;font-family:var(--mo);}
.tag-chip .rm{cursor:pointer;color:var(--rd);font-size:.78em;margin-left:2px;line-height:1;}
.tag-chip .rm:hover{color:#ff9090;}
.add-tag-row{display:flex;gap:6px;margin-bottom:8px;}
.add-tag-row input{flex:1;}
.preset-section{margin-bottom:10px;}
.preset-lbl{font-family:var(--mo);font-size:.54rem;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu);margin-bottom:5px;}
.preset-row{display:flex;gap:5px;flex-wrap:wrap;}
.pbtn{padding:3px 10px;border-radius:12px;font-family:var(--mo);font-size:.63rem;cursor:pointer;border:1px solid;transition:all .15s;background:transparent;}
.pbtn:hover{transform:translateY(-1px);}
.pbtn.game{border-color:#1a3a60;color:#6aadff;}.pbtn.game:hover{background:#060f1e;}
.pbtn.attach{border-color:#2a3a14;color:#88cc44;}.pbtn.attach:hover{background:#0c140a;}
.pbtn.scope{border-color:#3a1a60;color:#b080ff;}.pbtn.scope:hover{background:#0e0620;}
.pbtn.grip{border-color:#3a2a10;color:#ddaa44;}.pbtn.grip:hover{background:#120e04;}
.feat-card{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:14px 17px;margin-bottom:8px;}
.feat-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.feat-title{font-family:var(--mo);font-size:.65rem;letter-spacing:2px;text-transform:uppercase;color:var(--mu);}
.toggle-pill{display:flex;align-items:center;gap:8px;cursor:pointer;padding:5px 13px;border-radius:20px;font-family:var(--mo);font-size:.68rem;font-weight:700;border:1.5px solid var(--bd2);background:var(--bg);color:var(--mu);transition:all .25s;}
.toggle-pill.on{background:#031210;border-color:#1a5535;color:var(--ac);}
.toggle-pill.on.rf{background:#120805;border-color:#451808;color:var(--or);}
.toggle-pill .dot{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.6;}
.sp-preview{font-family:var(--mo);font-size:.64rem;color:var(--mu);min-height:1.3em;margin-bottom:8px;word-break:break-all;padding:6px 9px;background:var(--bg);border-radius:5px;border:1px solid var(--bd);}
.sp-preview.ready{color:var(--ac);border-color:#0c2e20;}
.sp-actions{display:flex;gap:8px;margin-top:10px;}
.sp-actions .btn{flex:1;}
.warn{background:#120802;border:1px solid #481a08;border-radius:7px;color:#cc7744;font-size:.73rem;padding:8px 12px;margin-bottom:10px;display:none;align-items:center;gap:8px;font-family:var(--mo);}
.warn.show{display:flex;}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
  background:#111820;border:1px solid #1a3040;border-radius:8px;color:var(--ac);
  font-family:var(--mo);font-size:.72rem;padding:9px 18px;opacity:0;
  transition:opacity .25s,transform .25s;pointer-events:none;z-index:999;white-space:nowrap;}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
.cfrow{display:flex;gap:8px;align-items:center;}.cfrow select{flex:1;}
#cfgdd{height:auto;font-family:var(--mo);font-size:.74rem;}
.tfilters{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px;}
.tfchip{cursor:pointer;padding:3px 9px;border-radius:14px;font-size:.65rem;font-family:var(--mo);background:var(--bg);border:1px solid var(--bd2);color:var(--mu);transition:all .15s;user-select:none;}
.tfchip.active{background:#041410;border-color:#1a5a35;color:var(--ac);}
.sw-notice{background:#03080f;border:1px solid #0a2035;border-radius:7px;padding:9px 12px;font-size:.7rem;color:var(--bl);line-height:1.7;font-family:var(--mo);}
.sw-notice .w-line{color:#5a3a10;display:block;margin-top:4px;}
.hf-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;}
.hf-lbl{font-size:.6rem;color:var(--mu);margin-bottom:3px;font-family:var(--mo);text-transform:uppercase;letter-spacing:.5px;}
/* v5.5 fix indicator */
.num-row{display:flex;align-items:baseline;gap:4px;margin-bottom:6px;}
.unit{font-family:var(--mo);font-size:.62rem;color:var(--mu);}
</style>
</head>
<body>
<div class="w">
  <div class="hdr">
    <div class="logo">R<em>V</em>N</div>
    <span class="vtag">v8.3 — RCS</span>
    <span id="conn-dot" class="conn-dot" title="Controller connection"></span>
  </div>

  <div class="card">
    <div class="clabel">Status</div>
    <button id="toggle-btn">LOADING</button>
    <div class="trow">
      Toggle key
      <select id="tbs">
        <option value="MMB">Middle Mouse</option>
        <option value="M4">M4 (Side Back)</option>
        <option value="M5" selected>M5 (Side Forward)</option>
      </select>
      <button id="beep-btn" title="Beep on/off" style="margin-left:auto;padding:3px 10px;border-radius:12px;font-size:.7rem;font-family:var(--mo);cursor:pointer;border:1px solid var(--bd2);background:var(--bg);color:var(--mu);transition:all .2s;">🔔</button>
    </div>
  </div>

  <!-- ── Feature Pills Row ─────────────────────────────────────────────────── -->
  <div style="display:flex;gap:8px;margin-bottom:8px;">
    <!-- Rapid Fire pill -->
    <div class="feat-card" style="flex:1;margin-bottom:0;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <span class="feat-title" style="color:var(--or);">Rapid Fire</span>
        <button id="rf-pill" class="toggle-pill rf">
          <span class="dot"></span><span id="rf-lbl">OFF</span>
        </button>
      </div>
      <div class="hf-lbl">Interval</div>
      <div style="display:flex;align-items:baseline;gap:4px;">
        <!-- BUG FIX: oninput wired in JS instead of hardcoded -->
        <input type="number" class="num num-sm" id="rf-ms" value="100" min="30" max="2000" style="width:80px;">
        <span class="unit">ms</span>
      </div>
      <input type="range" id="rf-sl" min="30" max="500" value="100" style="margin-top:5px;">
      <div class="hint" style="margin-top:4px;">30ms = ~33cps</div>
    </div>

    <!-- Hip Fire pill -->
    <div class="feat-card" style="flex:1;margin-bottom:0;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <span class="feat-title" style="color:var(--vi);">Hip Fire</span>
        <button id="hf-pill" class="toggle-pill">
          <span class="dot"></span><span id="hf-lbl">OFF</span>
        </button>
      </div>
      <div class="hf-row" style="margin-top:0;">
        <div>
          <div class="hf-lbl">Pull ↓</div>
          <input type="number" class="num num-sm" id="hf-pd" value="0" min="0" max="300" step="0.5">
        </div>
        <div>
          <div class="hf-lbl">Horiz ←→</div>
          <input type="number" class="num num-sm" id="hf-hz" value="0" min="-300" max="300" step="0.5">
        </div>
      </div>
      <div class="hint" style="margin-top:5px;">When RMB is not held (no ADS)</div>
    </div>
  </div>

  <!-- ── Weapon Slots card ──────────────────────────────────────────────────── -->
  <div class="card" id="ws-card">
    <div class="clabel" style="justify-content:space-between;">
      <span>Weapon Slots <span style="color:var(--ac);font-size:.85em;">KEY 1–5</span></span>
      <button id="ws-pill" class="toggle-pill" style="margin-left:auto;">
        <span class="dot"></span><span id="ws-lbl">OFF</span>
      </button>
    </div>
    <div class="hint" style="margin-bottom:10px;">
      Search weapon name per slot. | Select from the list.<br>
      Press <b style="color:#fff;">1</b>/<b style="color:#fff;">2</b>/… in-game → RCS switches automatically.
    </div>
    <div id="ws-slots-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
      <!-- rows injected by JS -->
    </div>
    <div id="ws-active-badge" style="margin-top:9px;font-family:var(--mo);font-size:.64rem;color:var(--mu);">
      Active slot: <span id="ws-active-num" style="color:var(--ac);">—</span>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="recoil">Recoil</button>
    <button class="tab" data-tab="humanize">Humanize</button>
    <button class="tab" data-tab="macros">Macros</button>
    <button class="tab" data-tab="tools">Tools</button>
    <button class="tab" data-tab="settings">Settings</button>
  </div>

  <!-- ═══ RECOIL ════════════════════════════════════════════════════════════ -->
  <div id="tab-recoil" class="tc active">
    <div class="card">
      <div class="clabel">Vertical — Pull-down <span id="cpv" class="cpill" style="display:none">CURVE</span></div>
      <input type="number" class="num" id="sv" value="1" min="0" max="300" step="0.001">
      <input type="range" min="0" max="300" value="1" id="sl">
      <div class="sdiv">Vertical Timing</div>
      <div class="tgrid">
        <div>
          <div class="tgrid-lbl">Delay</div>
          <div class="num-row"><input type="number" class="num num-sm" id="vdv" value="0" min="0" max="5000"><span class="unit">ms</span></div>
          <input type="range" min="0" max="5000" step="1" value="0" id="vds">
        </div>
        <div>
          <div class="tgrid-lbl">Duration</div>
          <div class="num-row"><input type="number" class="num num-sm" id="vduv" value="0" min="0" max="10000"><span class="unit">ms</span></div>
          <input type="range" min="0" max="10000" step="1" value="0" id="vdus">
        </div>
      </div>
      <div class="hint">Delay = wait before start · Duration = how long · 0 = unlimited</div>
    </div>

    <div class="card">
      <div class="clabel">Horizontal <span id="cph" class="cpill" style="display:none">CURVE</span></div>
      <input type="number" class="num" id="hv" value="0" min="-300" max="300" step="0.001">
      <input type="range" min="-300" max="300" value="0" id="hs">
      <div class="hint" style="margin-bottom:10px">Negative = left · Positive = right · 0 = off</div>
      <div class="sdiv">Horizontal Timing</div>
      <div class="tgrid">
        <div>
          <div class="tgrid-lbl">Delay</div>
          <div class="num-row"><input type="number" class="num num-sm" id="dv" value="500" min="0" max="5000"><span class="unit">ms</span></div>
          <input type="range" min="0" max="5000" step="1" value="500" id="ds">
        </div>
        <div>
          <div class="tgrid-lbl">Duration</div>
          <div class="num-row"><input type="number" class="num num-sm" id="uv" value="2000" min="0" max="10000"><span class="unit">ms</span></div>
          <input type="range" min="0" max="10000" step="1" value="2000" id="us">
        </div>
      </div>
      <div class="hint">0 Duration = entire hold</div>
    </div>

    <div class="card">
      <div class="clabel">Recoil Curve <span style="font-size:.85em;color:var(--mu);letter-spacing:0;font-family:var(--sa);">— overrides constant value</span></div>
      <div class="hint" style="margin-bottom:9px">Drag to draw · X = time · Y = pull strength<br>
        <span style="color:#223a28;">— — —</span> dashed = constant &nbsp;<span style="color:#3ab070;">——</span> solid = curve</div>
      <div class="ceditor">
        <canvas id="curve-canvas" height="120"></canvas>
        <div class="cacts">
          <button class="btn btn-s" id="curve-load-btn" style="font-size:.6rem;padding:5px 9px">Decay</button>
          <button class="btn btn-s" id="curve-flat-btn" style="font-size:.6rem;padding:5px 9px;border-color:#1a3a50;color:#60c8f0;">Flat</button>
          <button class="btn btn-g" id="curve-apply-btn" style="font-size:.6rem;padding:5px 9px">Apply</button>
          <button class="btn btn-d" id="curve-clear-btn" style="font-size:.6rem;padding:5px 9px">Clear</button>
          <span id="curve-pts" style="font-size:.6rem;color:var(--mu);margin-left:auto;font-family:var(--mo)">0 pts</span>
        </div>
      </div>
      <!-- v8.0 Curve Visualizer -->
      <div class="viz-wrap" style="margin-top:10px;">
        <canvas id="curve-viz" width="420" height="80"></canvas>
        <span class="viz-label">LIVE PREVIEW</span>
        <span class="viz-tick" id="viz-tick-lbl"></span>
      </div>
      <div style="font-size:.62rem;color:var(--mu);margin-top:5px;line-height:1.5;">
        Animates the curve playback in real time while firing. White line = current position.
      </div>
    </div>
  </div>

  <!-- ═══ MACROS ════════════════════════════════════════════════════════════ -->
  <div id="tab-macros" class="tc">
    <div class="card">
      <div class="clabel">Macros</div>
      <div class="hint" style="margin-bottom:10px;">
        Records mouse movement, mouse clicks, and keyboard input. Replay with a hotkey or the Play button.<br>
        <span style="color:var(--yl);">⚠ During recording — F1–F12, INS, HOME, PGUP, PGDN are reserved for hotkeys and will not be recorded.</span>
      </div>

      <div class="sdiv">Record New</div>
      <div class="row" style="margin-bottom:8px;gap:8px;">
        <input type="text" id="mac-name" placeholder="Macro name" style="flex:1;">
      </div>
      <div class="row" style="gap:8px;">
        <button class="btn" id="mac-rec-btn" style="flex:1;background:#0d0404;border-color:#3a0808;color:#ff6060;">⏺ Record</button>
        <button class="btn btn-d" id="mac-discard-btn" style="flex:0 0 auto;display:none;">✕ Discard</button>
      </div>
      <div id="mac-rec-status" style="font-family:var(--mo);font-size:.62rem;color:var(--mu);margin-top:6px;min-height:1.2em;"></div>

      <div class="sdiv" style="margin-top:12px;">Record Hotkey <span style="color:var(--vi);font-size:.85em;letter-spacing:0;font-family:var(--sa);">— start/stop recording with a key</span></div>
      <div class="row" style="gap:8px;align-items:center;margin-bottom:7px;">
        <div style="flex:1;">
          <div class="tgrid-lbl">Record Toggle Key</div>
          <select id="mac-rec-key" style="width:100%;">
            <option value="">Disabled (use button only)</option>
            <optgroup label="Function Keys">
              <option>F1</option><option>F2</option><option>F3</option><option>F4</option>
              <option>F5</option><option>F6</option><option>F7</option><option>F8</option>
              <option>F9</option><option>F10</option><option>F11</option><option>F12</option>
            </optgroup>
            <optgroup label="Navigation">
              <option>INS</option><option>DEL</option><option>HOME</option><option>END</option>
              <option>PGUP</option><option>PGDN</option>
            </optgroup>
          </select>
        </div>
      </div>
      <div class="row" style="gap:8px;align-items:flex-end;margin-bottom:7px;">
        <div style="flex:1;">
          <div class="tgrid-lbl">Trigger Hotkey (saved with macro)</div>
          <select id="mac-rec-trigger-key" style="width:100%;">
            <option value="">No hotkey</option>
            <optgroup label="Function Keys">
              <option>F1</option><option>F2</option><option>F3</option><option>F4</option>
              <option>F5</option><option>F6</option><option>F7</option><option>F8</option>
              <option>F9</option><option>F10</option><option>F11</option><option>F12</option>
            </optgroup>
            <optgroup label="Navigation">
              <option>INS</option><option>DEL</option><option>HOME</option><option>END</option>
              <option>PGUP</option><option>PGDN</option>
            </optgroup>
          </select>
        </div>
        <div style="display:flex;align-items:flex-end;padding-bottom:2px;">
          <label style="font-size:.72rem;color:var(--tx);display:flex;align-items:center;gap:5px;cursor:pointer;">
            <input type="checkbox" id="mac-rec-loop"> Loop
          </label>
        </div>
        <button class="btn btn-g" id="mac-rec-key-save" style="flex:0 0 auto;padding:8px 14px;">Set</button>
      </div>
      <div id="mac-rec-key-status" style="font-family:var(--mo);font-size:.6rem;color:var(--mu);margin-top:3px;min-height:1em;"></div>
      <div class="hint" style="margin-top:4px;">Press the toggle key anywhere to start/stop recording. Saved automatically — rename afterward if needed.</div>

      <div class="sdiv" style="margin-top:14px;">Saved Macros</div>
      <div id="mac-list" style="min-height:30px;"></div>
    </div>

    <!-- Delay Editor modal -->
    <div id="delay-editor" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;display:none;align-items:center;justify-content:center;">
      <div style="background:var(--bg2);border:1px solid var(--bd2);border-radius:10px;padding:18px;width:340px;max-height:80vh;overflow-y:auto;">
        <div style="font-size:.8rem;font-weight:600;color:var(--tx);margin-bottom:12px;">Edit Delays — <span id="delay-macro-name" style="color:var(--ac);"></span></div>
        <div class="hint" style="margin-bottom:10px;">Add or adjust delay (ms) between steps. You can insert explicit pauses anywhere.</div>
        <div id="delay-step-list" style="font-family:var(--mo);font-size:.68rem;max-height:340px;overflow-y:auto;"></div>
        <div style="margin-top:12px;display:flex;gap:8px;">
          <button class="btn btn-g" id="delay-add-btn" style="flex:1;">+ Add Delay at End</button>
          <input type="number" id="delay-add-ms" value="500" min="1" max="60000" style="width:80px;font-size:.72rem;">
          <span style="align-self:center;font-size:.68rem;color:var(--mu);">ms</span>
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;">
          <button class="btn btn-g" id="delay-save-btn" style="flex:1;">💾 Save Changes</button>
          <button class="btn btn-d" id="delay-close-btn" style="flex:1;">Cancel</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ TOOLS ════════════════════════════════════════════════════════════ -->
  <div id="tab-tools" class="tc">

    <!-- Sensitivity Scaling -->
    <!-- Export / Import -->
    <div class="card">
      <div class="clabel">Backup &amp; Restore</div>
      <div class="hint" style="margin-bottom:10px;">Export all profiles + macros as a ZIP. Import to restore or share configs.</div>
      <div class="io-row">
        <a href="/export" download="rvn_backup.zip" class="btn btn-s" style="flex:1;text-align:center;text-decoration:none;display:inline-block;padding:8px 13px;">⬇ Export ZIP</a>
        <label class="btn btn-or" style="flex:1;text-align:center;cursor:pointer;">
          ⬆ Import ZIP
          <input type="file" id="import-file" accept=".zip" style="display:none;">
        </label>
      </div>
      <label style="display:flex;align-items:center;gap:8px;font-size:.72rem;color:var(--tx);margin-top:8px;cursor:pointer;">
        <input type="checkbox" id="import-merge" checked> Merge (keep existing configs, only overwrite duplicates)
      </label>
      <div id="import-status" style="font-family:var(--mo);font-size:.62rem;color:var(--mu);margin-top:7px;min-height:1.2em;"></div>
    </div>

  </div>

  <!-- ═══ HUMANIZE ══════════════════════════════════════════════════════════ -->
  <div id="tab-humanize" class="tc">
    <div class="card">
      <div class="clabel">Humanization</div>
      <div class="hrow">
        <span class="hlbl">Jitter</span>
        <input type="range" id="js" min="0" max="1" step="0.01" value="0.15" style="flex:1">
        <span class="hval" id="jv">0.15</span>
      </div>
      <div class="hint" style="margin-bottom:13px">Gaussian noise per tick · 0 = off · 1 = max</div>
      <div class="hrow">
        <span class="hlbl">Smooth</span>
        <input type="range" id="ss" min="0" max="0.94" step="0.01" value="0.60" style="flex:1">
        <span class="hval" id="sv2">0.60</span>
      </div>
      <div class="hint">Exponential smoothing · 0 = raw · 0.94 = very smooth</div>
    </div>
  </div>

  <!-- ═══ SETTINGS ═══════════════════════════════════════════════════════════ -->
  <div id="tab-settings" class="tc">

    <div class="card">
      <div class="clabel">Trigger Mode</div>
      <select id="trig">
        <option value="LMB">LMB only</option>
        <option value="LMB+RMB">LMB + RMB together (ADS + fire)</option>
      </select>
    </div>

    <div class="card">
      <div class="clabel">Controller <span id="conn-dot2" class="conn-dot" style="margin-left:4px"></span></div>
      <select id="ctrl">
        <option value="makcu">MAKCU (2-PC Hardware)</option>
        <option value="kmbox">KMBox Net / Pro (2-PC Hardware)</option>
        <option value="software">No Hardware — Software Direct (1-PC)</option>
      </select>
      <div id="cbw" style="margin-top:7px;"></div>
    </div>

    <div class="card" id="kmbox-card" style="display:none">
      <div class="clabel">KMBox Net — UDP</div>
      <label style="font-size:.64rem;color:var(--mu);display:block;margin-bottom:5px;text-transform:uppercase;">IP Address</label>
      <input type="text" id="km-ip" placeholder="192.168.2.188" style="margin-bottom:8px;">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:9px;">
        <div>
          <label style="font-size:.64rem;color:var(--mu);display:block;margin-bottom:4px;text-transform:uppercase;">Port</label>
          <input type="text" id="km-port" placeholder="57856">
        </div>
        <div>
          <label style="font-size:.64rem;color:var(--rd);display:block;margin-bottom:4px;text-transform:uppercase;">UUID ★ required</label>
          <input type="text" id="km-uuid" placeholder="3AC07019" style="border-color:#3a1820;">
        </div>
      </div>
      <div class="row">
        <button class="btn btn-s" id="km-save" style="flex:1">Save</button>
        <button class="btn btn-g" id="km-conn" style="flex:1">Connect</button>
      </div>
      <div id="km-msg" style="margin-top:7px;font-size:.7rem;color:var(--mu);min-height:1.2em;font-family:var(--mo)"></div>
      <div class="hint" style="margin-top:8px;line-height:1.9">
        Find UUID in <strong>KMBox Client → Device Info</strong><br>
        Port: check the KMBox device screen<br>
        No extra libraries — raw UDP only
      </div>
    </div>

    <div class="card" id="sw-card" style="display:none">
      <div class="clabel">Software Direct Mode</div>
      <div class="sw-notice">
        SendInput Windows API<br>Runs on a <strong>single PC</strong> with no external hardware
        <span class="w-line">⚠ Python must run on Windows only<br>⚠ May be detected by anti-cheat</span>
      </div>
      <div id="sw-status" style="margin-top:8px;font-size:.68rem;font-family:var(--mo);color:var(--mu);">—</div>
    </div>

    <div class="card">
      <div class="clabel">
        Profile
        <span id="cfg-badge" style="background:#031410;border:1px solid #093820;color:var(--ac2);padding:2px 8px;border-radius:8px;font-size:.84em;letter-spacing:0;font-family:var(--mo);"></span>
      </div>
      <div class="cfrow"><select id="cfgfd"></select></div>
      <div class="row" style="margin-top:8px;">
        <input type="text" id="new-cfg-name" placeholder="New profile name…" style="flex:1;">
        <button class="btn btn-s" id="create-cfg">New</button>
        <button class="btn btn-d" id="delete-cfg">Del</button>
      </div>
    </div>

    <div class="card">
      <div class="clabel" id="browse-lbl">Browse Configs</div>
      <input type="text" id="search" placeholder="Search name or tag…" style="margin-bottom:8px;">
      <div id="tag-filters" class="tfilters"></div>
      <select id="cfgdd" size="6" style="font-family:var(--mo);font-size:.74rem;"></select>
    </div>

    <div class="card">
      <div class="clabel">Save Config</div>
      <div class="warn" id="warn"><span>!</span><span id="warn-txt">error</span></div>

      <div style="margin-bottom:10px;">
        <label style="font-size:.62rem;color:var(--mu);display:block;margin-bottom:5px;letter-spacing:.5px;text-transform:uppercase;font-family:var(--mo);">Name</label>
        <input type="text" id="cfg-name" placeholder="e.g. AK47 · MP5 Comp · M249 Iron">
      </div>

      <div class="sdiv">Tags — Quick Presets</div>

      <div class="preset-section">
        <div class="preset-lbl" style="color:#6aadff;">Game</div>
        <div class="preset-row" id="preset-game">
          <button class="pbtn game" data-k="game" data-v="R6">R6</button>
          <button class="pbtn game" data-k="game" data-v="Rust">Rust</button>
          <button class="pbtn game" data-k="game" data-v="CS2">CS2</button>
          <button class="pbtn game" data-k="game" data-v="Valorant">Valorant</button>
          <button class="pbtn game" data-k="game" data-v="Apex">Apex</button>
        </div>
      </div>
      <div class="preset-section">
        <div class="preset-lbl" style="color:#88cc44;">Barrel / Attach</div>
        <div class="preset-row" id="preset-attach">
          <button class="pbtn attach" data-k="barrel" data-v="Comp">Comp</button>
          <button class="pbtn attach" data-k="barrel" data-v="Muzzle">Muzzle</button>
          <button class="pbtn attach" data-k="barrel" data-v="Flash">Flash</button>
          <button class="pbtn attach" data-k="barrel" data-v="Ext">Ext</button>
          <button class="pbtn attach" data-k="barrel" data-v="Silencer">Silencer</button>
          <button class="pbtn attach" data-k="barrel" data-v="None">None</button>
        </div>
      </div>
      <div class="preset-section">
        <div class="preset-lbl" style="color:#b080ff;">Scope</div>
        <div class="preset-row" id="preset-scope">
          <button class="pbtn scope" data-k="scope" data-v="Iron">Iron</button>
          <button class="pbtn scope" data-k="scope" data-v="Holo">Holo</button>
          <button class="pbtn scope" data-k="scope" data-v="ACOG">ACOG</button>
          <button class="pbtn scope" data-k="scope" data-v="2x">2x</button>
          <button class="pbtn scope" data-k="scope" data-v="3x">3x</button>
          <button class="pbtn scope" data-k="scope" data-v="4x">4x</button>
        </div>
      </div>
      <div class="preset-section" style="margin-bottom:12px;">
        <div class="preset-lbl" style="color:#ddaa44;">Grip</div>
        <div class="preset-row" id="preset-grip">
          <button class="pbtn grip" data-k="grip" data-v="Vertical">Vertical</button>
          <button class="pbtn grip" data-k="grip" data-v="Angled">Angled</button>
          <button class="pbtn grip" data-k="grip" data-v="Half">Half</button>
          <button class="pbtn grip" data-k="grip" data-v="None">None</button>
        </div>
      </div>

      <div class="sdiv">Custom Tags</div>
      <div id="tag-chips" class="tag-row"></div>
      <div class="add-tag-row">
        <input type="text" id="tag-key" placeholder="key" style="flex:0 0 88px;">
        <input type="text" id="tag-val" placeholder="value">
        <button class="btn" id="add-tag-btn" style="flex:0 0 auto;padding:7px 10px;font-size:.67rem;">+ Tag</button>
      </div>

      <div class="sp-preview" id="sp-preview">Enter a name first…</div>
      <div class="sp-actions">
        <button class="btn btn-s" id="save-btn">Save</button>
        <button class="btn" id="overwrite-btn" style="background:#050c14;border-color:#10283a;color:#5599cc;">Overwrite</button>
        <button class="btn btn-d" id="delete-btn">Delete</button>
      </div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
document.addEventListener('DOMContentLoaded', async () => {
const $ = id => document.getElementById(id);

// ── Toast ─────────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, color='var(--ac)') {
  const t=$('toast'); t.textContent=msg; t.style.color=color;
  t.classList.add('show'); clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>t.classList.remove('show'), 2200);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws;
function connectWs() {
  const p = location.protocol==='https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(p+'//'+location.host+'/ws');
  ws.onopen = sendAll;
  ws.onclose = ()=>setTimeout(connectWs, 2000);
}
connectWs();

function sendAll() {
  if (ws && ws.readyState===1) ws.send(JSON.stringify({
    pull_down:              +$('sv').value  ||0,
    horizontal:             +$('hv').value  ||0,
    horizontal_delay_ms:    +$('dv').value  ||0,
    horizontal_duration_ms: +$('uv').value  ||0,
    vertical_delay_ms:      +$('vdv').value ||0,
    vertical_duration_ms:   +$('vduv').value||0,
    jitter_strength:        +$('js').value  ||0,
    smooth_factor:          +$('ss').value  ||0,
  }));
}

function safeNum(v, fb=0) { const n=parseFloat(v); return isNaN(n)?fb:n; }

function sync(r, i, cb) {
  r.oninput = ()=>{ i.value=r.value; if(cb)cb(); sendAll(); };
  i.oninput = ()=>{ const v=safeNum(i.value); r.value=v; i.value=v; if(cb)cb(); sendAll(); };
}
sync($('sl'),$('sv'),drawCurve); sync($('hs'),$('hv'));
sync($('ds'),$('dv')); sync($('us'),$('uv'));
sync($('vds'),$('vdv')); sync($('vdus'),$('vduv'));
$('js').oninput=()=>{ $('jv').textContent=parseFloat($('js').value).toFixed(2); sendAll(); };
$('ss').oninput=()=>{ $('sv2').textContent=parseFloat($('ss').value).toFixed(2); sendAll(); };

// ── BUG FIX: rapid fire slider/input sync — send interval as soon as value changes ──
const rfMs=$('rf-ms'), rfSl=$('rf-sl');

function sendRFInterval() {
  // Always send interval to server (whether rfEnabled is true or false)
  // so the server has the latest value when RF is enabled
  fetch('/rapid-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:rfEnabled, interval_ms:safeNum(rfMs.value,100)})
  }).catch(()=>{});
}

// BUG FIX: use 'input' not 'change' — 'change' fires only after blur
rfSl.oninput = ()=>{
  rfMs.value = rfSl.value;
  sendRFInterval();
};
rfMs.oninput = ()=>{
  const v = safeNum(rfMs.value, 100);
  rfSl.value = Math.min(v, 500);
  sendRFInterval();
};

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS=['recoil','humanize','settings'];
function switchTab(name) {
  document.querySelectorAll('.tab,.tc').forEach(e=>e.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
  $('tab-'+name).classList.add('active');
}
document.querySelectorAll('.tab').forEach(t=>{ t.onclick=()=>switchTab(t.dataset.tab); });
document.addEventListener('keydown', e=>{
  if(e.altKey && e.key>='1' && e.key<='3'){ e.preventDefault(); switchTab(TABS[+e.key-1]); }
});

// ── Conn dot ──────────────────────────────────────────────────────────────────
function setConnDot(ok) {
  [$('conn-dot'),$('conn-dot2')].forEach(d=>{
    d.classList.toggle('ok',ok); d.classList.toggle('bad',!ok);
    d.title = ok ? 'Connected' : 'Disconnected';
  });
}

// ── Status ────────────────────────────────────────────────────────────────────
function setBtn(on) {
  $('toggle-btn').textContent = on ? '■ ON' : '○ OFF';
  $('toggle-btn').className   = on ? 'enabled' : 'disabled';
}

$('toggle-btn').onclick = ()=>
  fetch('/toggle',{method:'POST'}).then(r=>r.json()).then(d=>setBtn(d.is_enabled));

$('tbs').onchange = ()=>
  fetch('/toggle-button',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({button:$('tbs').value})});

// ── Beep toggle ───────────────────────────────────────────────────────────────
let beepEnabled = true;
function updateBeepBtn() {
  const b = $('beep-btn');
  b.textContent  = beepEnabled ? '🔔' : '🔕';
  b.style.color  = beepEnabled ? 'var(--ac)' : 'var(--mu)';
  b.style.borderColor = beepEnabled ? '#1a5535' : 'var(--bd2)';
}
$('beep-btn').onclick = ()=>{
  beepEnabled = !beepEnabled;
  updateBeepBtn();
  fetch('/beep',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:beepEnabled})});
};

// ── BUG FIX: Rapid Fire pill — send interval_ms on every pill click ───────────────
let rfEnabled = false;
function syncRF() {
  rfEnabled = !rfEnabled;
  $('rf-pill').classList.toggle('on', rfEnabled);
  $('rf-lbl').textContent = rfEnabled ? 'ON' : 'OFF';
  // BUG FIX: send interval_ms from current input every time (not default 100)
  fetch('/rapid-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:rfEnabled, interval_ms:safeNum(rfMs.value,100)})
  }).then(r=>r.json()).then(d=>{
    if(d.rapid_fire_enabled) toast('⚡ Rapid Fire ON  '+d.rapid_fire_interval_ms+'ms','var(--or)');
    else toast('Rapid Fire OFF','var(--mu)');
  });
}
$('rf-pill').onclick = syncRF;

// ── BUG FIX: Hip Fire — always send values even when pill is off (persist) ───────
let hfEnabled = false;
function sendHF() {
  // BUG FIX: always send; do not gate on hfEnabled
  fetch('/hip-fire',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:hfEnabled, pull_down:safeNum($('hf-pd').value), horizontal:safeNum($('hf-hz').value)})
  }).catch(()=>{});
}
$('hf-pill').onclick = ()=>{
  hfEnabled = !hfEnabled;
  $('hf-pill').classList.toggle('on', hfEnabled);
  $('hf-lbl').textContent = hfEnabled ? 'ON' : 'OFF';
  sendHF();
  toast(hfEnabled ? '🎯 Hip Fire ON' : 'Hip Fire OFF', hfEnabled ? 'var(--vi)' : 'var(--mu)');
};
// BUG FIX: send on every change (enabled or not)
$('hf-pd').oninput = sendHF;
$('hf-hz').oninput = sendHF;

// BUG FIX: stop getStatus() overwriting fields the user is editing
// and sync pull_down/horizontal into UI on first init only
let _statusInitDone = false;
let _lastFocusedInput = null;
let _lastConfigLoad = 0;  // timestamp of last config load from browse
document.querySelectorAll('input').forEach(el=>{
  el.addEventListener('focus', ()=>{ _lastFocusedInput = el.id; });
  el.addEventListener('blur',  ()=>{ setTimeout(()=>{ if(_lastFocusedInput===el.id) _lastFocusedInput=null; }, 300); });
});

function getStatus() {
  fetch('/status').then(r=>r.json()).then(d=>{
    setBtn(d.is_enabled);
    if(d.toggle_button) $('tbs').value=d.toggle_button;
    if(d.trigger_mode)  $('trig').value=d.trigger_mode;
    if(d.controller_type){ $('ctrl').value=d.controller_type; ctrlUI(d.controller_type,d.ctrl_connected); }
    if(d.current_config_file) $('cfg-badge').textContent=d.current_config_file.replace('.json','');
    if(d.jitter_strength!==undefined){ $('js').value=d.jitter_strength; $('jv').textContent=(+d.jitter_strength).toFixed(2); }
    if(d.smooth_factor  !==undefined){ $('ss').value=d.smooth_factor;   $('sv2').textContent=(+d.smooth_factor).toFixed(2); }
    $('cpv').style.display = d.has_pull_curve  ? 'inline-flex':'none';
    $('cph').style.display = d.has_horiz_curve ? 'inline-flex':'none';
    if(d.kmbox_ip && !$('km-ip').value){ $('km-ip').value=d.kmbox_ip; $('km-port').value=d.kmbox_port; }
    setConnDot(!!d.ctrl_connected);

    // BUG FIX: sync pull_down/horizontal on first init only
    // do not overwrite every poll or loaded configs get reset
    if(!_statusInitDone) {
      if(d.pull_down  !==undefined){ $('sv').value=d.pull_down;   $('sl').value=Math.round(d.pull_down); }
      if(d.horizontal !==undefined){ $('hv').value=d.horizontal;  $('hs').value=Math.round(d.horizontal); }
      if(d.horizontal_delay_ms   !==undefined){ $('dv').value=d.horizontal_delay_ms;   $('ds').value=d.horizontal_delay_ms; }
      if(d.horizontal_duration_ms!==undefined){ $('uv').value=d.horizontal_duration_ms;$('us').value=d.horizontal_duration_ms; }
      if(d.vertical_delay_ms     !==undefined){ $('vdv').value=d.vertical_delay_ms;    $('vds').value=d.vertical_delay_ms; }
      if(d.vertical_duration_ms  !==undefined){ $('vduv').value=d.vertical_duration_ms;$('vdus').value=d.vertical_duration_ms; }
      _statusInitDone = true;
    }

    // Rapid fire sync — only when state differs (not while user is editing)
    if(d.rapid_fire_enabled!==undefined && d.rapid_fire_enabled!==rfEnabled){
      rfEnabled=d.rapid_fire_enabled;
      $('rf-pill').classList.toggle('on',rfEnabled); $('rf-lbl').textContent=rfEnabled?'ON':'OFF';
    }
    // BUG FIX: sync interval on init or when rf-ms is not focused
    if(d.rapid_fire_interval_ms && _lastFocusedInput!=='rf-ms') {
      rfMs.value=d.rapid_fire_interval_ms;
      rfSl.value=Math.min(d.rapid_fire_interval_ms,500);
    }

    // Hip fire pill state sync
    if(d.hip_fire_enabled!==undefined && d.hip_fire_enabled!==hfEnabled){
      hfEnabled=d.hip_fire_enabled;
      $('hf-pill').classList.toggle('on',hfEnabled); $('hf-lbl').textContent=hfEnabled?'ON':'OFF';
    }
    // FIX BUG 2: do not overwrite hf-pd/hf-hz within 3s after loading a config
    // getStatus may return stale server state before WS/REST updates land
    const now2 = Date.now();
    if(now2 - _lastConfigLoad > 3000) {
      if(d.hip_pull_down  !==undefined && _lastFocusedInput!=='hf-pd') $('hf-pd').value=d.hip_pull_down;
      if(d.hip_horizontal !==undefined && _lastFocusedInput!=='hf-hz') $('hf-hz').value=d.hip_horizontal;
    }
    // beep sync
    if(d.beep_enabled!==undefined && d.beep_enabled!==beepEnabled){
      beepEnabled=d.beep_enabled; updateBeepBtn();
    }
    // Weapon slot state sync
    if(d.weapon_slot_enabled!==undefined && d.weapon_slot_enabled!==wsEnabled){
      wsEnabled=d.weapon_slot_enabled;
      $('ws-pill').classList.toggle('on',wsEnabled);
      $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
    }
    if(d.active_slot!==undefined){
      const prev=$('ws-active-num').textContent;
      const next=d.active_slot>0?String(d.active_slot):'—';
      $('ws-active-num').textContent=next;
      // If slot changed, sync recoil UI values from app_state (server already applied them)
      if(prev!==next && d.active_slot>0){
        if(d.pull_down  !==undefined){ $('sv').value=d.pull_down;   $('sl').value=Math.round(d.pull_down); }
        if(d.horizontal !==undefined){ $('hv').value=d.horizontal;  $('hs').value=Math.round(d.horizontal); }
        if(d.horizontal_delay_ms   !==undefined){ $('dv').value=d.horizontal_delay_ms;   $('ds').value=d.horizontal_delay_ms; }
        if(d.horizontal_duration_ms!==undefined){ $('uv').value=d.horizontal_duration_ms;$('us').value=d.horizontal_duration_ms; }
        if(d.vertical_delay_ms     !==undefined){ $('vdv').value=d.vertical_delay_ms;    $('vds').value=d.vertical_delay_ms; }
        if(d.vertical_duration_ms  !==undefined){ $('vduv').value=d.vertical_duration_ms;$('vdus').value=d.vertical_duration_ms; }
        $('cpv').style.display=d.has_pull_curve ?'inline-flex':'none';
        $('cph').style.display=d.has_horiz_curve?'inline-flex':'none';
        // Highlight active slot config in Browse list
        if(d.active_slot_config){ $('cfgdd').value=d.active_slot_config; updBrowseLbl(); }
        drawCurve();
      }
    }
  }).catch(()=>{});
}
getStatus();
// OPT v8.3: adaptive polling — 800ms when visible, paused when tab hidden
let _statusTimer = setInterval(getStatus, 800);
document.addEventListener('visibilitychange', ()=>{
  clearInterval(_statusTimer);
  if (!document.hidden) {
    getStatus();  // immediate refresh on tab focus
    _statusTimer = setInterval(getStatus, 800);
  }
});

// ── Weapon Slots ──────────────────────────────────────────────────────────────
let wsEnabled = false;
let wsSlots   = {1:null,2:null,3:null,4:null,5:null};
let wsSlotRf  = {1:null,2:null,3:null,4:null,5:null};

function buildWsGrid() {
  const grid = $('ws-slots-grid');
  grid.innerHTML = '';

  // Remove old datalist if any
  let dl = document.getElementById('ws-datalist');
  if(dl) dl.remove();

  // Inject custom dropdown styles once
  if(!document.getElementById('ws-dd-style')){
    const st = document.createElement('style');
    st.id = 'ws-dd-style';
    st.textContent = `
      .ws-dd-wrap { position:relative; margin-bottom:7px; }
      .ws-dd-input {
        width:100%; box-sizing:border-box;
        font-size:.72rem; padding:5px 26px 5px 8px;
        background:var(--bg); border:1px solid var(--bd2);
        border-radius:6px; color:var(--tx);
        font-family:var(--sa); outline:none;
        cursor:pointer; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis;
        transition:border-color .15s;
      }
      .ws-dd-input:focus { border-color:var(--ac); }
      .ws-dd-arrow {
        position:absolute; right:7px; top:50%;
        transform:translateY(-50%);
        pointer-events:none; color:var(--mu);
        font-size:.6rem; line-height:1;
      }
      .ws-dd-clr {
        position:absolute; right:20px; top:50%;
        transform:translateY(-50%);
        background:transparent; border:none;
        color:var(--mu); font-size:.85rem;
        cursor:pointer; padding:0 2px; line-height:1;
        display:none;
      }
      .ws-dd-clr.visible { display:block; }
      .ws-dd-list {
        position:absolute; top:calc(100% + 3px); left:0; right:0;
        background:#1a1a1f; border:1px solid var(--ac);
        border-radius:7px; z-index:9999;
        max-height:220px; overflow-y:auto;
        box-shadow:0 6px 24px rgba(0,0,0,.6);
        display:none; flex-direction:column;
        scrollbar-width:thin; scrollbar-color:var(--bd2) transparent;
      }
      .ws-dd-list.open { display:flex; }
      .ws-dd-search {
        padding:6px 8px; font-size:.7rem;
        background:transparent; border:none;
        border-bottom:1px solid var(--bd2);
        color:var(--tx); font-family:var(--sa);
        outline:none; position:sticky; top:0;
        background:#1a1a1f;
      }
      .ws-dd-items { overflow-y:auto; flex:1; }
      .ws-dd-item {
        padding:5px 10px; font-size:.72rem;
        font-family:var(--sa); color:var(--tx);
        cursor:pointer; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis;
        transition:background .1s;
      }
      .ws-dd-item:hover, .ws-dd-item.active { background:rgba(255,255,255,.07); color:var(--ac); }
      .ws-dd-item.selected { color:var(--ac); font-weight:600; }
      .ws-dd-empty { padding:8px 10px; font-size:.68rem; color:var(--mu); font-family:var(--sa); }
    `;
    document.head.appendChild(st);
  }

  // Helper: resolve display name → key
  function nameToKey(name){
    if(!name) return null;
    const lower = name.toLowerCase();
    if(typeof allKeys!=='undefined'){
      for(const k of allKeys){
        const cfg=cache[k];
        const n=typeof cfg==='object'&&cfg.name ? cfg.name : k;
        if(n.toLowerCase()===lower) return k;
      }
      if(allKeys.includes(name)) return name;
    }
    return null;
  }

  // Helper: build weapon name list
  function getWeaponList(filter=''){
    if(typeof allKeys==='undefined') return [];
    const q = filter.toLowerCase();
    return allKeys
      .map(k=>{ const cfg=cache[k]; return {key:k, name:typeof cfg==='object'&&cfg.name?cfg.name:k}; })
      .filter(w=>!q||w.name.toLowerCase().includes(q));
  }

  // Close all open dropdowns
  function closeAllDd(){
    document.querySelectorAll('.ws-dd-list.open').forEach(el=>el.classList.remove('open'));
  }
  document.removeEventListener('click', window._wsDdClose||null);
  window._wsDdClose = (e)=>{ if(!e.target.closest('.ws-dd-wrap')) closeAllDd(); };
  document.addEventListener('click', window._wsDdClose);

  for(let s=1;s<=5;s++){
    const wrap = document.createElement('div');
    wrap.style.cssText='background:var(--bg);border:1px solid var(--bd2);border-radius:7px;padding:8px 10px;';

    // Slot label
    const lbl = document.createElement('div');
    lbl.style.cssText='font-family:var(--mo);font-size:.58rem;color:var(--mu);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;';
    lbl.textContent='Slot '+s+' [key '+s+']';

    // ── Custom dropdown ───────────────────────────────────────────────────────
    const ddWrap = document.createElement('div');
    ddWrap.className = 'ws-dd-wrap';

    const inp = document.createElement('div');
    inp.id = 'ws-inp-'+s;
    inp.className = 'ws-dd-input';
    inp.tabIndex = 0;

    const arrow = document.createElement('span');
    arrow.className = 'ws-dd-arrow';
    arrow.textContent = '▾';

    const clrBtn = document.createElement('button');
    clrBtn.className = 'ws-dd-clr';
    clrBtn.textContent = '×';
    clrBtn.title = 'Clear slot';

    // Dropdown panel
    const ddList = document.createElement('div');
    ddList.className = 'ws-dd-list';

    const ddSearch = document.createElement('input');
    ddSearch.type = 'text';
    ddSearch.className = 'ws-dd-search';
    ddSearch.placeholder = '🔍  search…';
    ddSearch.autocomplete = 'off';

    const ddItems = document.createElement('div');
    ddItems.className = 'ws-dd-items';

    ddList.appendChild(ddSearch);
    ddList.appendChild(ddItems);

    // Pre-fill
    const assignedKey = wsSlots[s];
    let currentKey = assignedKey || null;
    if(assignedKey){
      const cfg=cache[assignedKey];
      inp.textContent = typeof cfg==='object'&&cfg.name ? cfg.name : assignedKey;
      inp.style.color = 'var(--tx)';
      clrBtn.classList.add('visible');
    } else {
      inp.textContent = 'Search weapon name…';
      inp.style.color = 'var(--mu)';
    }

    function renderItems(filter=''){
      ddItems.innerHTML='';
      const list = getWeaponList(filter);
      if(!list.length){
        const empty = document.createElement('div');
        empty.className='ws-dd-empty';
        empty.textContent='No weapons found';
        ddItems.appendChild(empty);
        return;
      }
      list.forEach(w=>{
        const item = document.createElement('div');
        item.className='ws-dd-item'+(w.key===currentKey?' selected':'');
        item.textContent = w.name;
        item.title = w.name;
        item.onmousedown=(e)=>{
          e.preventDefault();
          currentKey = w.key;
          inp.textContent = w.name;
          inp.style.color = 'var(--tx)';
          clrBtn.classList.add('visible');
          ddList.classList.remove('open');
          assignSlot(s, w.key);
        };
        ddItems.appendChild(item);
      });
    }

    inp.onclick=(e)=>{
      e.stopPropagation();
      const isOpen = ddList.classList.contains('open');
      closeAllDd();
      if(!isOpen){
        ddSearch.value='';
        renderItems('');
        ddList.classList.add('open');
        setTimeout(()=>ddSearch.focus(),30);
      }
    };
    inp.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ inp.onclick(e); } };

    ddSearch.oninput=()=>renderItems(ddSearch.value);
    ddSearch.onkeydown=(e)=>{
      if(e.key==='Escape'){ ddList.classList.remove('open'); inp.focus(); }
    };

    clrBtn.onclick=(e)=>{
      e.stopPropagation();
      currentKey=null;
      inp.textContent='Search weapon name…';
      inp.style.color='var(--mu)';
      clrBtn.classList.remove('visible');
      closeAllDd();
      assignSlot(s,null);
    };

    ddWrap.appendChild(inp);
    ddWrap.appendChild(clrBtn);
    ddWrap.appendChild(arrow);
    ddWrap.appendChild(ddList);

    // Rapid Fire row
    const rfRow = document.createElement('div');
    rfRow.style.cssText='display:flex;align-items:center;gap:6px;margin-top:2px;';

    const rfChk = document.createElement('input');
    rfChk.type='checkbox';
    rfChk.id='ws-rf-en-'+s;
    const slotRf = wsSlotRf[s];
    rfChk.checked = slotRf ? slotRf.enabled : false;
    rfChk.title = 'Enable Rapid Fire for this slot';

    const rfLbl = document.createElement('label');
    rfLbl.htmlFor='ws-rf-en-'+s;
    rfLbl.style.cssText='font-family:var(--mo);font-size:.6rem;color:var(--or);cursor:pointer;user-select:none;';
    rfLbl.textContent='⚡ RF';

    const rfMs = document.createElement('input');
    rfMs.type='number';
    rfMs.id='ws-rf-ms-'+s;
    rfMs.min=30; rfMs.max=2000;
    rfMs.value = slotRf ? slotRf.interval_ms : 100;
    rfMs.style.cssText='width:58px;font-size:.68rem;padding:2px 5px;font-family:var(--mo);background:var(--sf);border:1px solid var(--bd2);border-radius:4px;color:var(--tx);';
    rfMs.title='Rapid Fire interval (ms)';

    const rfUnit = document.createElement('span');
    rfUnit.style.cssText='font-family:var(--mo);font-size:.6rem;color:var(--mu);';
    rfUnit.textContent='ms';

    const rfClear = document.createElement('button');
    rfClear.style.cssText='margin-left:auto;font-family:var(--mo);font-size:.58rem;color:var(--mu);background:transparent;border:1px solid var(--bd2);border-radius:4px;padding:2px 6px;cursor:pointer;';
    rfClear.textContent='inherit';
    rfClear.title='Inherit global Rapid Fire setting';
    rfClear.onclick=()=>{ rfChk.checked=false; assignSlotRf(s,null,null); };

    const saveRf=()=>assignSlotRf(s, rfChk.checked, parseInt(rfMs.value)||100);
    rfChk.onchange=saveRf;
    rfMs.onchange=saveRf;

    rfRow.appendChild(rfChk);
    rfRow.appendChild(rfLbl);
    rfRow.appendChild(rfMs);
    rfRow.appendChild(rfUnit);
    rfRow.appendChild(rfClear);

    wrap.appendChild(lbl);
    wrap.appendChild(ddWrap);
    wrap.appendChild(rfRow);
    grid.appendChild(wrap);
  }
}

function assignSlot(slot, configName){
  fetch('/weapon-slots/assign',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slot,config_name:configName||null})})
  .then(r=>r.json()).then(d=>{
    wsSlots=d.slots;
    toast('Slot '+slot+': '+(configName||'cleared'),'var(--ac)');
  }).catch(()=>{});
}

function assignSlotRf(slot, enabled, interval_ms){
  fetch('/weapon-slots/assign-rf',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slot, enabled, interval_ms})})
  .then(r=>r.json()).then(d=>{
    wsSlotRf=d.slot_rf;
    if(enabled===null) toast('Slot '+slot+' RF: inherit global','var(--mu)');
    else toast('Slot '+slot+' RF: '+(enabled?'ON '+interval_ms+'ms':'OFF'),'var(--or)');
  }).catch(()=>{});
}

function fetchWsSlots(){
  fetch('/weapon-slots').then(r=>r.json()).then(d=>{
    wsEnabled=d.enabled;
    wsSlots=d.slots;
    wsSlotRf=d.slot_rf||{1:null,2:null,3:null,4:null,5:null};
    $('ws-pill').classList.toggle('on',wsEnabled);
    $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
    $('ws-active-num').textContent=d.active_slot>0?d.active_slot:'—';
    buildWsGrid();
  }).catch(()=>{});
}

$('ws-pill').onclick=()=>{
  wsEnabled=!wsEnabled;
  $('ws-pill').classList.toggle('on',wsEnabled);
  $('ws-lbl').textContent=wsEnabled?'ON':'OFF';
  fetch('/weapon-slots/enabled',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:wsEnabled})})
  .then(()=>toast(wsEnabled?'🔫 Weapon Slots ON':'Weapon Slots OFF',wsEnabled?'var(--ac)':'var(--mu)'));
};

// ── Controller ────────────────────────────────────────────────────────────────
function ctrlUI(ct, connected) {
  $('kmbox-card').style.display = ct==='kmbox'    ? 'block':'none';
  $('sw-card').style.display    = ct==='software' ? 'block':'none';
  const M={makcu:['mk','MAKCU 2-PC'],kmbox:['km','KMBox / kmNet'],software:['sw','Software 1-PC']};
  const [cls,txt]=M[ct]||['mk','—'];
  $('cbw').innerHTML=`<span class="cbadge ${cls}">${txt}</span>`;
  if(ct==='software'){
    $('sw-status').textContent=connected?'✓ Ready — SendInput active':'✗ Not Ready (Windows only)';
    $('sw-status').style.color=connected?'var(--ac)':'var(--rd)';
  }
}
$('ctrl').onchange=()=>{
  const ct=$('ctrl').value;
  fetch('/controller-type',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({controller:ct})})
    .then(()=>ctrlUI(ct,false));
};
$('trig').onchange=()=>
  fetch('/trigger-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('trig').value})});

const kmsg=(t,c)=>{$('km-msg').textContent=t;$('km-msg').style.color=c||'var(--mu)';};
$('km-save').onclick=()=>{
  const ip=$('km-ip').value.trim(),port=+$('km-port').value||57856,uuid=$('km-uuid').value.trim().replace(/-/g,'').replace(/ /g,'');
  if(!ip){kmsg('Enter IP','var(--rd)');return;}
  if(uuid.length<8){kmsg('Enter UUID (e.g. 4BD95C53)','var(--rd)');return;}
  fetch('/kmbox-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip,port,uuid})})
    .then(()=>kmsg('✓ Saved','var(--ac)')).catch(()=>kmsg('Failed','var(--rd)'));
};
$('km-conn').onclick=()=>{
  kmsg('Connecting...');
  fetch('/kmbox-connect',{method:'POST'}).then(r=>r.json())
    .then(d=>kmsg(d.connected?'✓ Connected':'✗ '+d.message,d.connected?'var(--ac)':'var(--rd)'))
    .catch(()=>kmsg('Connection failed','var(--rd)'));
};

// ── Curve editor ──────────────────────────────────────────────────────────────
const cv=$('curve-canvas'),ctx=cv.getContext('2d');
let pts=[],drawing=false;
function resize(){
  const r=cv.getBoundingClientRect();
  const w=Math.floor(r.width)||cv.offsetWidth||320;
  if(cv.width!==w){cv.width=w;cv.height=120;drawCurve();}
}
new ResizeObserver(resize).observe(cv);
requestAnimationFrame(resize);

function drawCurve(){
  const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#080b0f';ctx.fillRect(0,0,W,H);
  ctx.strokeStyle='#191e28';ctx.lineWidth=1;
  [1,2,3].forEach(i=>{
    ctx.beginPath();ctx.moveTo(0,H*i/4);ctx.lineTo(W,H*i/4);ctx.stroke();
    ctx.beginPath();ctx.moveTo(W*i/4,0);ctx.lineTo(W*i/4,H);ctx.stroke();
  });
  const refY=1-Math.min((safeNum($('sv').value)/300),1);
  ctx.strokeStyle='rgba(50,160,80,.5)';ctx.lineWidth=1.5;ctx.setLineDash([5,5]);
  ctx.beginPath();ctx.moveTo(0,refY*H);ctx.lineTo(W,refY*H);ctx.stroke();
  ctx.setLineDash([]);
  if(pts.length<2){$('curve-pts').textContent='0 pts';return;}
  const sorted=[...pts].sort((a,b)=>a.x-b.x);
  ctx.beginPath();ctx.moveTo(sorted[0].x*W,H);
  sorted.forEach(p=>ctx.lineTo(p.x*W,p.y*H));
  ctx.lineTo(sorted[sorted.length-1].x*W,H);ctx.closePath();
  ctx.fillStyle='rgba(91,240,160,.05)';ctx.fill();
  ctx.beginPath();ctx.strokeStyle='#5bf0a0';ctx.lineWidth=2;
  sorted.forEach((p,i)=>{const x=p.x*W,y=p.y*H;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
  $('curve-pts').textContent=pts.length+' pts';
}

function normM(e){const r=cv.getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};}
function normT(e){const r=cv.getBoundingClientRect(),t=e.touches[0];return{x:Math.max(0,Math.min(1,(t.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(t.clientY-r.top)/r.height))};}
cv.addEventListener('mousedown',e=>{e.preventDefault();drawing=true;pts=[normM(e)];drawCurve();});
cv.addEventListener('mousemove',e=>{if(!drawing)return;const p=normM(e),l=pts[pts.length-1];if(Math.abs(p.x-l.x)>.007||Math.abs(p.y-l.y)>.007){pts.push(p);drawCurve();}});
cv.addEventListener('mouseup',()=>{drawing=false;});
cv.addEventListener('mouseleave',()=>{drawing=false;});
cv.addEventListener('touchstart',e=>{e.preventDefault();drawing=true;pts=[normT(e)];drawCurve();},{passive:false});
cv.addEventListener('touchmove',e=>{e.preventDefault();if(!drawing)return;const p=normT(e),l=pts[pts.length-1];if(Math.abs(p.x-l.x)>.007||Math.abs(p.y-l.y)>.007){pts.push(p);drawCurve();}},{passive:false});
cv.addEventListener('touchend',()=>{drawing=false;});

$('curve-load-btn').onclick=()=>{
  const v=Math.max(0,Math.min(300,safeNum($('sv').value))),N=40;
  pts=Array.from({length:N},(_,i)=>{const t=i/(N-1);const decay=Math.exp(-t*1.6)*0.45+0.55;return{x:t,y:1-Math.min(v*decay/300,1)};});
  drawCurve();
};
// Flat — horizontal curve at same level as constant (dashed ref), apply immediately
$('curve-flat-btn').onclick=()=>{
  const v=Math.max(0,Math.min(300,safeNum($('sv').value)));
  const flatY=1-Math.min(v/300,1);
  pts=[{x:0,y:flatY},{x:0.25,y:flatY},{x:0.5,y:flatY},{x:0.75,y:flatY},{x:1,y:flatY}];
  drawCurve();
  const c=getCurve();
  if(c&&ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:c}));
  $('cpv').style.display='inline-flex';
  toast('✓ Flat curve applied');
};
$('curve-apply-btn').onclick=()=>{
  const c=getCurve();if(!c){showWarn('Draw a curve first');return;}
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:c}));
  $('cpv').style.display='inline-flex';toast('✓ Curve applied');
  const b=$('curve-apply-btn');b.textContent='Applied ✓';setTimeout(()=>{b.textContent='Apply';},1600);
};
$('curve-clear-btn').onclick=()=>{
  pts=[];drawCurve();$('cpv').style.display='none';
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({pull_down_curve:[]}));
  toast('Curve cleared','var(--mu)');
};
function restoreCurve(arr){
  if(!arr||arr.length<2){pts=[];drawCurve();return;}
  const N=arr.length;pts=arr.map((v,i)=>({x:i/(N-1),y:1-Math.min(v/300,1)}));drawCurve();
}
function getCurve(){
  if(pts.length<2)return null;
  return[...pts].sort((a,b)=>a.x-b.x).map(p=>parseFloat(((1-p.y)*300).toFixed(2)));
}

// ── Preset tag buttons ────────────────────────────────────────────────────────
let currentTags = {};

document.querySelectorAll('.pbtn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const k=btn.dataset.k, v=btn.dataset.v;
    currentTags[k]=v;
    btn.closest('.preset-row').querySelectorAll('.pbtn').forEach(b=>{
      b.style.opacity = (b.dataset.v===v) ? '1' : '0.45';
      b.style.borderWidth = (b.dataset.v===v) ? '2px' : '1px';
    });
    renderTagChips(); updSavePreview();
  });
});

function renderTagChips(){
  const c=$('tag-chips');c.innerHTML='';
  Object.entries(currentTags).forEach(([k,v])=>{
    const chip=document.createElement('span');
    chip.className='tag-chip';
    chip.innerHTML=`<span style="color:var(--bl)">${k}:</span><span>${v}</span><span class="rm" data-k="${k}">×</span>`;
    chip.querySelector('.rm').onclick=e=>{
      delete currentTags[e.target.dataset.k];
      document.querySelectorAll(`.pbtn[data-k="${e.target.dataset.k}"]`).forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='1px'; });
      renderTagChips(); updSavePreview();
    };
    c.appendChild(chip);
  });
}

$('add-tag-btn').onclick=()=>{
  const k=$('tag-key').value.trim(),v=$('tag-val').value.trim();
  if(!k||!v){showWarn('Enter both key and value');return;}
  currentTags[k]=v;renderTagChips();updSavePreview();
  $('tag-key').value='';$('tag-val').value='';
};
$('tag-key').addEventListener('keydown',e=>{if(e.key==='Tab'&&$('tag-key').value.trim()){e.preventDefault();$('tag-val').focus();}});
$('tag-val').addEventListener('keydown',e=>{if(e.key==='Enter')$('add-tag-btn').click();});

function updSavePreview(){
  const name=$('cfg-name').value.trim(),pre=$('sp-preview');
  if(!name){pre.textContent='Enter a name first…';pre.className='sp-preview';return;}
  let txt='"'+name+'"';
  if(Object.keys(currentTags).length>0)txt+='  '+Object.entries(currentTags).map(([k,v])=>`[${k}:${v}]`).join(' ');
  const vv=safeNum($('sv').value),hh=safeNum($('hv').value);
  txt+=`  ↓${vv}`;if(hh!==0)txt+=`  ←→${hh}`;
  pre.textContent=txt;pre.className='sp-preview ready';
}
$('cfg-name').oninput=updSavePreview;

// ── Config system ─────────────────────────────────────────────────────────────
let cache={},allKeys=[],activeTagFilters=new Set();

function fetchConfigs(){
  return fetch('/configs').then(r=>r.json()).then(d=>{
    cache=d;allKeys=Object.keys(d);buildTagFilters();filterBrowse();
    buildWsGrid();  // rebuild weapon slot dropdowns with fresh config list
  }).catch(()=>{});
}

function buildTagFilters(){
  const tagSet=new Set();
  Object.values(cache).forEach(cfg=>{
    if(typeof cfg==='object'&&cfg.tags)
      Object.entries(cfg.tags).forEach(([k,v])=>tagSet.add(`${k}:${v}`));
  });
  const c=$('tag-filters');c.innerHTML='';
  [...tagSet].sort().forEach(tag=>{
    const el=document.createElement('span');
    el.className='tfchip'+(activeTagFilters.has(tag)?' active':'');
    el.textContent=tag;
    el.onclick=()=>{ activeTagFilters.has(tag)?activeTagFilters.delete(tag):activeTagFilters.add(tag); el.classList.toggle('active',activeTagFilters.has(tag)); filterBrowse(); };
    c.appendChild(el);
  });
  if(tagSet.size===0)c.innerHTML='<span style="font-size:.65rem;color:var(--mu);">No tags yet</span>';
}

function filterBrowse(){
  const q=$('search').value.toLowerCase(),prev=$('cfgdd').value;
  $('cfgdd').innerHTML='<option value="">-- Select config --</option>';
  for(const key of allKeys){
    const cfg=cache[key];
    const name=typeof cfg==='object'?(cfg.name||key):key;
    const tags=typeof cfg==='object'?(cfg.tags||{}):{};
    const pd=typeof cfg==='object'?(cfg.pull_down??0):0;
    const haystack=(name+' '+Object.entries(tags).map(([k,v])=>k+':'+v).join(' ')).toLowerCase();
    if(q&&!haystack.includes(q))continue;
    if(activeTagFilters.size>0){
      const ts=new Set(Object.entries(tags).map(([k,v])=>`${k}:${v}`));
      if(![...activeTagFilters].every(t=>ts.has(t)))continue;
    }
    const tagStr=Object.entries(tags).map(([k,v])=>`[${k}:${v}]`).join(' ');
    const o=document.createElement('option');
    o.value=key;
    o.textContent=name+`  ↓${pd}`;
    if(tagStr) o.title=tagStr;
    $('cfgdd').appendChild(o);
  }
  $('cfgdd').value=prev;
  updBrowseLbl();
}

function updBrowseLbl(){
  const key=$('cfgdd').value,lbl=$('browse-lbl');
  if(!key){lbl.textContent='Browse Configs';return;}
  const cfg=cache[key];
  const name=typeof cfg==='object'?(cfg.name||key):key;
  const tags=typeof cfg==='object'?(cfg.tags||{}):{}
  const tagStr=Object.entries(tags).map(([k,v])=>`<span style="color:var(--mu);font-size:.6rem">[${k}:${v}]</span>`).join(' ');
  lbl.innerHTML='Selected — <span style="color:var(--ac);font-family:var(--mo)">'+name+'</span>'+(tagStr?' '+tagStr:'');
}

$('cfgdd').onchange=()=>{
  const key=$('cfgdd').value;updBrowseLbl();if(!key)return;
  const cfg=cache[key];if(cfg==null)return;
  const pd=typeof cfg==='object'?(cfg.pull_down??0):(cfg??0);
  const hz=typeof cfg==='object'?(cfg.horizontal??0):0;
  const dl=typeof cfg==='object'?(cfg.horizontal_delay_ms??500):500;
  const du=typeof cfg==='object'?(cfg.horizontal_duration_ms??2000):2000;
  const vdl=typeof cfg==='object'?(cfg.vertical_delay_ms??0):0;
  const vdu=typeof cfg==='object'?(cfg.vertical_duration_ms??0):0;
  $('sv').value=pd;$('sl').value=Math.round(pd);
  $('hv').value=hz;$('hs').value=Math.round(hz);
  $('dv').value=dl;$('ds').value=dl;
  $('uv').value=du;$('us').value=du;
  $('vdv').value=vdl;$('vds').value=vdl;
  $('vduv').value=vdu;$('vdus').value=vdu;
  // FIX BUG 2a: set hip fire inputs then immediately push to server via REST
  // (WS alone is too slow — getStatus() poll at 1s can overwrite before WS arrives)
  if(cfg.hip_pull_down !==undefined) $('hf-pd').value=cfg.hip_pull_down;
  if(cfg.hip_horizontal!==undefined) $('hf-hz').value=cfg.hip_horizontal;
  // FIX BUG 2b: always push hip fire values to server when loading config
  _lastConfigLoad = Date.now();  // suppress getStatus overwrite for 3s
  sendHF();
  const hasCurve=typeof cfg==='object'&&cfg.pull_down_curve;
  $('cpv').style.display=hasCurve?'inline-flex':'none';
  restoreCurve(hasCurve?cfg.pull_down_curve:null);
  // FIX BUG 2c: send all recoil values immediately via WS
  // Use setTimeout(0) to ensure DOM values are committed before sendAll reads them
  setTimeout(()=>{
    sendAll();
    if(ws&&ws.readyState===1) ws.send(JSON.stringify({pull_down_curve:hasCurve?cfg.pull_down_curve:[]}));
  }, 0);
  const name=typeof cfg==='object'?(cfg.name||key):key;
  $('cfg-name').value=name;
  currentTags=typeof cfg==='object'&&cfg.tags?{...cfg.tags}:{};
  document.querySelectorAll('.pbtn').forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='1px'; });
  Object.entries(currentTags).forEach(([k,v])=>{
    document.querySelectorAll(`.pbtn[data-k="${k}"][data-v="${v}"]`).forEach(b=>{ b.style.opacity='1'; b.style.borderWidth='2px'; });
    document.querySelectorAll(`.pbtn[data-k="${k}"]:not([data-v="${v}"])`).forEach(b=>{ b.style.opacity='0.45'; });
  });
  renderTagChips();updSavePreview();
  toast('✓ Loaded: '+name);
};

$('search').oninput=filterBrowse;

function showWarn(msg){ $('warn-txt').textContent=msg; $('warn').classList.add('show'); setTimeout(()=>$('warn').classList.remove('show'),3000); }

function buildPayload(name){
  const c=getCurve();
  return{
    name,
    tags:Object.keys(currentTags).length>0?currentTags:null,
    pull_down_value:        safeNum($('sv').value),
    vertical_delay_ms:      safeNum($('vdv').value),
    vertical_duration_ms:   safeNum($('vduv').value),
    horizontal_value:       safeNum($('hv').value),
    horizontal_delay_ms:    safeNum($('dv').value),
    horizontal_duration_ms: safeNum($('uv').value),
    hip_pull_down:          safeNum($('hf-pd').value),
    hip_horizontal:         safeNum($('hf-hz').value),
    ...(c?{pull_down_curve:c}:{})
  };
}

$('save-btn').onclick=()=>{
  const name=$('cfg-name').value.trim();if(!name){showWarn('Enter a name');return;}
  fetch('/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildPayload(name))})
    .then(r=>r.json()).then(d=>{ if(d.detail)showWarn(d.detail); else{fetchConfigs();toast('✓ Saved: '+name);} }).catch(()=>showWarn('Save failed'));
};
$('overwrite-btn').onclick=()=>{
  const key=$('cfgdd').value,name=$('cfg-name').value.trim()||key;
  if(!key){showWarn('Select a config to overwrite');return;}
  if(!confirm('Overwrite "'+name+'" with current values?'))return;
  fetch('/configs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildPayload(name))})
    .then(r=>r.json()).then(d=>{
      if(d.detail)showWarn(d.detail);
      else{
        if(key!==name) fetch('/configs/'+encodeURIComponent(key),{method:'DELETE'}).finally(fetchConfigs);
        else fetchConfigs();
        toast('✓ Overwritten: '+name);
      }
    }).catch(()=>showWarn('Overwrite failed'));
};
$('delete-btn').onclick=()=>{
  const key=$('cfgdd').value;if(!key){showWarn('Select a config to delete');return;}
  if(!confirm('Delete "'+key+'"?'))return;
  fetch('/configs/'+encodeURIComponent(key),{method:'DELETE'})
    .then(()=>{fetchConfigs();toast('Deleted: '+key,'var(--rd)');}).catch(()=>{});
};

// ── Profile management ────────────────────────────────────────────────────────
function fetchCfgFiles(){
  fetch('/config-files').then(r=>r.json()).then(d=>{
    const dd=$('cfgfd');dd.innerHTML='';
    d.files.forEach(f=>{ const o=document.createElement('option');o.value=f;o.textContent=f.replace('.json','');dd.appendChild(o); });
    dd.value=d.current;
    $('cfg-badge').textContent=d.current.replace('.json','');
  }).catch(()=>{});
}
$('cfgfd').onchange=()=>{
  fetch('/config-files/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:$('cfgfd').value})})
    .then(r=>r.json()).then(d=>{
      $('cfg-badge').textContent=d.current_config_file.replace('.json','');
      cache=d.guns;allKeys=Object.keys(d.guns);
      buildTagFilters();filterBrowse();
      buildWsGrid();  // weapon slots follow the active profile
      toast('Profile: '+d.current_config_file.replace('.json',''));
    }).catch(()=>{});
};
$('create-cfg').onclick=()=>{
  const n=$('new-cfg-name').value.trim();if(!n)return;
  fetch('/config-files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:n})})
    .then(r=>r.json()).then(()=>{fetchCfgFiles();$('new-cfg-name').value='';toast('✓ Profile created: '+n);}).catch(()=>{});
};
$('delete-cfg').onclick=()=>{
  const f=$('cfgfd').value;if(!f||f==='default.json')return;
  if(confirm('Delete profile "'+f+'"?'))
    fetch('/config-files/'+encodeURIComponent(f),{method:'DELETE'})
      .then(()=>{fetchCfgFiles();toast('Profile deleted','var(--rd)');}).catch(()=>{});
};

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Curve Visualizer  (lightweight, event-driven)
//
//  Rules:
//  1. Static draw (drawVizStatic) — called ONCE whenever values change.
//     No animation loop. Costs one canvas repaint.
//  2. Tick animation — starts ONLY when server reports is_enabled=true.
//     Driven by setInterval at 50ms (20fps) — matches TICK_S granularity,
//     NOT requestAnimationFrame (no need for 60fps on a status indicator).
//     Stops immediately when firing stops OR tab goes hidden.
//  3. No duplicate /status poll — vizFiring is updated inside the existing
//     getStatus() cycle already polling every 1 s.
// ══════════════════════════════════════════════════════════════════════════
const vizCanvas = $('curve-viz');
const vizCtx    = vizCanvas ? vizCanvas.getContext('2d') : null;
let _vizIntervalId = null;   // setInterval handle — null when idle
let _vizTick       = 0;
let vizFiring      = false;

function _drawViz(tick, firing) {
  if (!vizCtx) return;
  const W = vizCanvas.width, H = vizCanvas.height;
  vizCtx.clearRect(0, 0, W, H);

  // grid — draw once using a single path per axis
  vizCtx.strokeStyle = '#1a1a2a'; vizCtx.lineWidth = 1;
  vizCtx.beginPath();
  for (let x = 0; x <= W; x += W/6) { vizCtx.moveTo(x,0); vizCtx.lineTo(x,H); }
  for (let y = 0; y <= H; y += H/4) { vizCtx.moveTo(0,y); vizCtx.lineTo(W,y); }
  vizCtx.stroke();

  const curve = getCurve();
  const pd    = parseFloat($('sv').value) || 0;
  const pts   = (curve && curve.length > 1) ? curve : Array.from({length:30}, ()=>pd);
  const maxV  = Math.max(...pts, 1);
  const N     = pts.length;
  const px    = i => (i / (N - 1)) * W;
  const py    = v => H - (v / maxV) * (H * 0.85) - H * 0.075;

  // filled area
  vizCtx.beginPath();
  pts.forEach((v,i) => i===0 ? vizCtx.moveTo(px(i),py(v)) : vizCtx.lineTo(px(i),py(v)));
  vizCtx.lineTo(W,H); vizCtx.lineTo(0,H); vizCtx.closePath();
  vizCtx.fillStyle = 'rgba(30,100,60,0.10)'; vizCtx.fill();

  // curve line
  vizCtx.beginPath();
  pts.forEach((v,i) => i===0 ? vizCtx.moveTo(px(i),py(v)) : vizCtx.lineTo(px(i),py(v)));
  vizCtx.strokeStyle = firing ? '#1a7050' : '#252535';
  vizCtx.lineWidth = 1.5; vizCtx.stroke();

  // position indicator — only when firing
  if (firing) {
    const t  = tick % N;
    const cx = px(t), cy = py(pts[t]);
    vizCtx.beginPath();
    vizCtx.strokeStyle = 'rgba(91,240,160,0.5)';
    vizCtx.lineWidth = 1; vizCtx.setLineDash([3,4]);
    vizCtx.moveTo(cx,0); vizCtx.lineTo(cx,H); vizCtx.stroke();
    vizCtx.setLineDash([]);
    vizCtx.beginPath();
    vizCtx.arc(cx, cy, 4, 0, Math.PI*2);
    vizCtx.fillStyle = '#5bf0a0'; vizCtx.fill();
    if ($('viz-tick-lbl')) $('viz-tick-lbl').textContent = `t=${tick % N}/${N}`;
  } else {
    if ($('viz-tick-lbl')) $('viz-tick-lbl').textContent = '';
  }
}

// Static snapshot — called on value changes, no loop
function drawVizStatic() { _drawViz(0, false); }

// Start 20fps interval only while actively firing
function _vizStart() {
  if (_vizIntervalId) return;
  _vizTick = 0;
  _vizIntervalId = setInterval(()=>{
    // Stop if tab hidden (saves CPU when alt-tabbed mid-game)
    if (document.hidden || !vizFiring) { _vizStop(); return; }
    _drawViz(_vizTick++, true);
  }, 50);  // 20fps — enough for a tick indicator
}

function _vizStop() {
  if (_vizIntervalId) { clearInterval(_vizIntervalId); _vizIntervalId = null; }
  _vizTick = 0;
  drawVizStatic();  // restore clean static view
}

// Pause/resume on tab visibility change
document.addEventListener('visibilitychange', ()=>{ if (document.hidden) _vizStop(); });

// Redraw static preview whenever curve/slider values change (input events only)
['sv','sl'].forEach(id=>{
  const el=$(id);
  if (el) el.addEventListener('input', ()=>{ if (!vizFiring) drawVizStatic(); });
});

// Called from getStatus handler below — zero overhead when state hasn't changed
function vizUpdate(isFiring) {
  if (isFiring === vizFiring) return;  // no state change → do nothing
  vizFiring = isFiring;
  isFiring ? _vizStart() : _vizStop();
}

// Draw initial static preview after DOM settles
setTimeout(drawVizStatic, 300);

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Sensitivity Scaling
// ══════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════
//  v8.2 — Macros UI
// ══════════════════════════════════════════════════════════════════════════
let macRecording    = false;
let macRecPollTimer = null;

function fetchMacros() {
  fetch('/macros').then(r=>r.json()).then(d=>{
    renderMacros(d.macros, d.playing, d.recording);
    macRecording = d.recording;
    updateRecBtn();
  }).catch(()=>{});
}

function stepSummary(steps) {
  if (!steps || !steps.length) return '0 steps';
  const moves   = steps.filter(s=>s.type==='move').length;
  const clicks  = steps.filter(s=>s.type==='click'&&s.state==='down').length;
  const keys    = steps.filter(s=>s.type==='kdown').length;
  const delays  = steps.filter(s=>s.type==='delay').length;
  const parts   = [];
  if (moves)  parts.push(`${moves} moves`);
  if (clicks) parts.push(`${clicks} clicks`);
  if (keys)   parts.push(`${keys} keys`);
  if (delays) parts.push(`${delays} delays`);
  return parts.join(', ') || `${steps.length} steps`;
}

function renderMacros(macros, playing, recording) {
  const el = $('mac-list');
  if (!el) return;
  const keys = Object.keys(macros||{});
  if (!keys.length) {
    el.innerHTML='<div style="font-size:.68rem;color:var(--mu);padding:6px 0;">No macros saved yet.</div>';
    return;
  }
  el.innerHTML = keys.map(name=>{
    const m        = macros[name];
    const keyBadge = m.key ? `<span class="macro-key">${m.key}</span>` : '';
    const loopBadge= m.loop ? '<span style="color:var(--vi);font-size:.6rem;margin-left:4px;">LOOP</span>' : '';
    const isPlaying= (playing||[]).includes(name);
    const playLbl  = isPlaying ? '⏹ Stop' : '▶ Play';
    const playCls  = isPlaying ? 'btn-d'  : 'btn-g';
    const enc      = encodeURIComponent(name);
    return `<div class="macro-item" style="flex-wrap:wrap;gap:5px;">
      <span class="macro-name ${isPlaying?'macro-playing':''}" style="flex:1;min-width:80px;">${name}</span>
      ${keyBadge}${loopBadge}
      <span class="macro-steps">${stepSummary(m.steps)}</span>
      <button class="btn ${playCls} mac-play-btn" style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}" data-loop="${!!m.loop}" data-playing="${isPlaying}">${playLbl}</button>
      <button class="btn btn-s mac-edit-btn"  style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}">⏱ Delays</button>
      <button class="btn btn-d mac-del-btn"   style="font-size:.6rem;padding:4px 8px;"
              data-name="${enc}">✕</button>
    </div>`;
  }).join('');

  el.querySelectorAll('.mac-play-btn').forEach(btn=>{
    btn.onclick = ()=>{
      const name    = decodeURIComponent(btn.dataset.name);
      const loop    = btn.dataset.loop === 'true';
      const playing = btn.dataset.playing === 'true';
      toggleMacro(name, loop, playing);
    };
  });
  el.querySelectorAll('.mac-edit-btn').forEach(btn=>{
    btn.onclick = ()=> openDelayEditor(decodeURIComponent(btn.dataset.name));
  });
  el.querySelectorAll('.mac-del-btn').forEach(btn=>{
    btn.onclick = ()=> deleteMacro(decodeURIComponent(btn.dataset.name));
  });
}

function toggleMacro(name, loop, playing) {
  const ep   = playing ? '/macros/stop' : '/macros/play';
  const body = playing ? {name} : {name, loop};
  fetch(ep, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(r=>{ if(!r.ok) r.json().then(e=>showWarn(e.detail||'Failed')); else fetchMacros(); })
    .catch(()=>showWarn('Request failed'));
}

function deleteMacro(name) {
  if (!confirm(`Delete macro "${name}"?`)) return;
  fetch('/macros/'+encodeURIComponent(name), {method:'DELETE'})
    .then(r=>{ if(!r.ok) showWarn('Delete failed'); else fetchMacros(); })
    .catch(()=>showWarn('Delete failed'));
}

function updateRecBtn() {
  const btn     = $('mac-rec-btn');
  const st      = $('mac-rec-status');
  const discard = $('mac-discard-btn');
  if (!btn) return;
  if (macRecording) {
    btn.textContent = '⏹ Stop & Save';
    btn.style.cssText = 'flex:1;background:#200404;border-color:#7a0808;color:#ff9090;';
    if (discard) discard.style.display = '';
  } else {
    btn.textContent = '⏺ Record';
    btn.style.cssText = 'flex:1;background:#0d0404;border-color:#3a0808;color:#ff6060;';
    if (st) st.textContent = '';
    if (discard) discard.style.display = 'none';
    stopRecPoll();
  }
}

function startRecPoll() {
  if (macRecPollTimer) return;
  macRecPollTimer = setInterval(()=>{
    fetch('/macros/record/status').then(r=>r.json()).then(d=>{
      if (!d.recording) { stopRecPoll(); return; }
      const st = $('mac-rec-status');
      if (st)
        st.innerHTML = `● Recording&hellip; &nbsp;<span style="color:var(--ac);">${d.steps} events captured</span>`;
    }).catch(()=>{});
  }, 200);   // 200ms — slightly snappier counter, still cheap
}

function stopRecPoll() {
  if (macRecPollTimer) { clearInterval(macRecPollTimer); macRecPollTimer = null; }
}

$('mac-rec-btn').onclick = ()=>{
  if (!macRecording) {
    fetch('/macros/record/start', {method:'POST'})
      .then(()=>{
        macRecording = true;
        updateRecBtn();
        startRecPoll();
        const st = $('mac-rec-status');
        if (st) { st.textContent='● Recording…'; st.style.color='#ff6060'; }
      }).catch(()=>showWarn('Could not start recording'));
  } else {
    const name = ($('mac-name').value||'').trim();
    if (!name) { showWarn('Enter a macro name first'); return; }
    const key  = ($('mac-rec-trigger-key').value) || null;
    const loop = $('mac-rec-loop').checked;
    fetch('/macros/record/stop', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, key, loop, steps:[]})
    }).then(r=>r.json()).then(d=>{
      macRecording = false;
      updateRecBtn();
      fetchMacros();
      toast(`✓ Saved "${d.saved}" — ${d.steps} events`);
      $('mac-name').value='';
    }).catch(()=>showWarn('Save failed'));
  }
};

$('mac-discard-btn').onclick = ()=>{
  if (!confirm('Discard current recording?')) return;
  fetch('/macros/record/discard', {method:'POST'})
    .then(()=>{ macRecording=false; updateRecBtn(); fetchMacros(); })
    .catch(()=>{ macRecording=false; updateRecBtn(); });
};

fetchMacros();
// Reduced from 2000ms → 1200ms for snappier macro list refresh
// but paused while macro tab is hidden (saves CPU when not looking at macros)
let _macPollTimer = setInterval(fetchMacros, 1200);
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click', ()=>{
    // Slow down poll when not on macros tab
    clearInterval(_macPollTimer);
    _macPollTimer = setInterval(fetchMacros, t.dataset.tab === 'macros' ? 1200 : 4000);
  });
});

// ── Macro Record Key ──────────────────────────────────────────────────────────
function fetchRecordKey() {
  fetch('/macros/record-key').then(r=>r.json()).then(d=>{
    if ($('mac-rec-key'))         $('mac-rec-key').value         = d.record_key  || '';
    if ($('mac-rec-trigger-key')) $('mac-rec-trigger-key').value = d.trigger_key || '';
    if ($('mac-rec-loop'))        $('mac-rec-loop').checked      = !!d.loop;
    const st = $('mac-rec-key-status');
    if (st) _updateRecKeyStatus(d.record_key, d.trigger_key, d.loop);
  }).catch(()=>{});
}
fetchRecordKey();

function _updateRecKeyStatus(recKey, triggerKey, loop) {
  const st = $('mac-rec-key-status');
  if (!st) return;
  if (!recKey) { st.textContent = ''; return; }
  let msg = `✓ ${recKey} → toggles recording`;
  if (triggerKey) msg += ` · saves with hotkey ${triggerKey}`;
  if (loop)       msg += ' · LOOP';
  st.textContent = msg;
  st.style.color = 'var(--vi)';
}

$('mac-rec-key-save').onclick = ()=>{
  const key        = $('mac-rec-key').value        || '';
  const triggerKey = $('mac-rec-trigger-key').value || '';
  const loop       = $('mac-rec-loop').checked;
  fetch('/macros/record-key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, trigger_key: triggerKey || null, loop})
  }).then(r=>r.json()).then(d=>{
    _updateRecKeyStatus(d.record_key, d.trigger_key, d.loop);
    if (d.record_key) {
      let msg = `⏺ Record key: ${d.record_key}`;
      if (d.trigger_key) msg += `  →  trigger: ${d.trigger_key}`;
      toast(msg, 'var(--vi)');
    } else {
      toast('Record key disabled', 'var(--mu)');
    }
  }).catch(()=>showWarn('Failed to set record key'));
};

// ══════════════════════════════════════════════════════════════════════════
//  Delay Editor
// ══════════════════════════════════════════════════════════════════════════
let _delayMacroName  = '';
let _delaySteps      = [];

function openDelayEditor(name) {
  _delayMacroName = name;
  fetch('/macros').then(r=>r.json()).then(d=>{
    const m = (d.macros||{})[name];
    if (!m) { showWarn('Macro not found'); return; }
    _delaySteps = JSON.parse(JSON.stringify(m.steps||[]));
    $('delay-macro-name').textContent = name;
    renderDelaySteps();
    $('delay-editor').style.display = 'flex';
  });
}

function stepLabel(s) {
  if (!s) return '?';
  switch(s.type) {
    case 'move':  return `Move (${s.dx>=0?'+':''}${s.dx}, ${s.dy>=0?'+':''}${s.dy})`;
    case 'click': return `Click ${s.btn} ${s.state}`;
    case 'kdown': return `Key ${s.key} down`;
    case 'kup':   return `Key ${s.key} up`;
    case 'delay': return `Delay`;
    default:      return s.type||'step';
  }
}

function renderDelaySteps() {
  const el = $('delay-step-list');
  if (!el) return;
  if (!_delaySteps.length) { el.innerHTML='<div style="color:var(--mu);padding:6px 0;">No steps.</div>'; return; }

  el.innerHTML = _delaySteps.map((s,i)=>{
    const lbl   = stepLabel(s);
    const dtVal = s.dt_ms != null ? s.dt_ms : 0;
    const isDelay = s.type === 'delay';
    // Color-code delay steps differently so they're easy to spot
    const lblColor = isDelay ? 'var(--yl)' : 'var(--tx)';
    return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--bd);">
      <span style="min-width:18px;font-family:var(--mo);font-size:.58rem;color:var(--mu);">${i+1}</span>
      <span style="flex:1;color:${lblColor};font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${lbl}</span>
      <span style="color:var(--mu);font-size:.58rem;white-space:nowrap;">wait:</span>
      <input type="number" value="${dtVal}" min="0" max="60000" step="10"
             data-idx="${i}"
             style="width:65px;font-size:.68rem;padding:2px 4px;text-align:right;"
             class="delay-ms-inp">
      <span style="color:var(--mu);font-size:.58rem;">ms</span>
      <button class="btn btn-d delay-del-btn" data-idx="${i}"
              style="font-size:.55rem;padding:2px 6px;flex-shrink:0;" title="Remove this step">✕</button>
    </div>`;
  }).join('');

  // Bind events via delegation — avoids inline onclick index bugs
  el.querySelectorAll('.delay-ms-inp').forEach(inp=>{
    inp.addEventListener('change', ()=>{
      const idx = parseInt(inp.dataset.idx);
      if (_delaySteps[idx] != null)
        _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
    });
    // Also update on blur so typing then clicking ✕ immediately captures the value
    inp.addEventListener('blur', ()=>{
      const idx = parseInt(inp.dataset.idx);
      if (_delaySteps[idx] != null)
        _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
    });
  });

  el.querySelectorAll('.delay-del-btn').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const idx = parseInt(btn.dataset.idx);
      _delaySteps.splice(idx, 1);
      renderDelaySteps();
    });
  });
}

$('delay-add-btn').onclick = ()=>{
  const ms = Math.max(1, parseInt($('delay-add-ms').value)||500);
  _delaySteps.push({type:'delay', dt_ms: ms});
  renderDelaySteps();
};

$('delay-save-btn').onclick = ()=>{
  // Flush any input values still in-focus before saving — use data-idx, not forEach index
  $('delay-step-list').querySelectorAll('input.delay-ms-inp').forEach(inp=>{
    const idx = parseInt(inp.dataset.idx);
    if (_delaySteps[idx] != null)
      _delaySteps[idx].dt_ms = Math.max(0, parseInt(inp.value)||0);
  });
  fetch(`/macros/${encodeURIComponent(_delayMacroName)}/steps`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({steps: _delaySteps})
  }).then(r=>r.json()).then(d=>{
    $('delay-editor').style.display = 'none';
    fetchMacros();
    toast(`✓ Delays saved — ${d.steps} steps`);
  }).catch(()=>showWarn('Save failed'));
};

$('delay-close-btn').onclick = ()=>{ $('delay-editor').style.display='none'; };

// Close on backdrop click
$('delay-editor').onclick = e=>{ if(e.target===$('delay-editor')) $('delay-editor').style.display='none'; };

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Export / Import
// ══════════════════════════════════════════════════════════════════════════
$('import-file').onchange=function(){
  const file=this.files[0]; if(!file)return;
  const merge=$('import-merge').checked;
  const st=$('import-status');
  st.textContent='Uploading…'; st.style.color='var(--mu)';
  const reader=new FileReader();
  reader.onload=e=>{
    const b64=btoa(String.fromCharCode(...new Uint8Array(e.target.result)));
    fetch('/import',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data:b64,merge})})
      .then(r=>r.json()).then(d=>{
        const imp=d.imported;
        st.textContent=`✓ Imported: ${imp.profiles.join(', ')||'—'}${imp.macros?' + macros':''}`;
        st.style.color='var(--ac)';
        fetchConfigs();fetchCfgFiles();fetchMacros();
        toast('✓ Import complete');
      }).catch(()=>{st.textContent='Import failed';st.style.color='var(--rd)';});
  };
  reader.readAsArrayBuffer(file);
  this.value='';
};

// ══════════════════════════════════════════════════════════════════════════
//  v8.0 — Hook visualizer into the EXISTING getStatus poll
//  (no duplicate fetch — just piggybacks on the 1s poll already running)
// ══════════════════════════════════════════════════════════════════════════
function onStatusUpdate(s) {
  // Drive visualizer — only triggers canvas work on state change
  vizUpdate(!!s.is_enabled && !!s.ctrl_connected);
}

// Monkey-patch into the existing getStatus interval.
(()=>{
  const _origGS = typeof getStatus === 'function' ? getStatus : null;
  if (_origGS) {
    window.getStatus = function() {
      return fetch('/status').then(r=>r.json()).then(s=>{
        onStatusUpdate(s);
        return s;
      }).catch(()=>{});
    };
  } else {
    let _vizPollBusy = false;
    setInterval(()=>{
      if (_vizPollBusy || document.hidden) return;
      _vizPollBusy = true;
      fetch('/status').then(r=>r.json()).then(s=>{
        onStatusUpdate(s); _vizPollBusy=false;
      }).catch(()=>{ _vizPollBusy=false; });
    }, 1000);
  }
})();

// Init
fetchConfigs().then(()=>fetchWsSlots());fetchCfgFiles();updSavePreview();
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTML


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

        print("\n══ RVN v8.2 Unit Tests ══")
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