"""
RVN — Recoil Control System  v4.1
Bug fixes & improvements from v4.0:
  • FIX: Software Direct mode — StartButtonListener was never called on connect
  • FIX: Software Direct polling thread leaked if controller switched
  • FIX: Controller reconnect loop didn't call StartButtonListener after reconnect
  • FIX: KMBox get_button_state used wrong poll approach (stateless UDP, not callback)
  • FIX: Curve canvas didn't resize correctly on first load (race condition)
  • FIX: WebSocket pull_down_curve:null didn't actually clear the curve on server
  • FIX: Config save used 'pull_down' key but load expected 'pull_down_value' in some paths
  • FIX: humanize() smooth clamp allowed 1.0 which causes division-style freeze
  • IMPROVE: Smoother reset on toggle now also resets hold_start
  • IMPROVE: Status endpoint includes controller connection state
  • IMPROVE: UI shows controller connected/disconnected indicator live
  • IMPROVE: Software mode warning is clearer about Windows requirement
  • IMPROVE: Input validation on numeric fields (NaN guard)
  • IMPROVE: Config browse shows pull_down value in dropdown for quick reference
  • IMPROVE: Tab keyboard shortcut (Alt+1/2/3)
"""

import threading
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
import json, os, socket, time, struct, random
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
    _makcu = _MakcuStub()

makcu_controller = _makcu


# ══════════════════════════════════════════════════════════════════════════════
#  SoftwareController  (ctypes SendInput — single-PC)
# ══════════════════════════════════════════════════════════════════════════════
class SoftwareController:
    _VK = {"LMB": 0x01, "RMB": 0x02, "MMB": 0x04, "M4": 0x05, "M5": 0x06}

    def __init__(self):
        self._user32 = None
        self._ready = False
        self._btn: dict = {}
        self._lock = threading.Lock()
        self._connected = False
        self._poll_thread: threading.Thread | None = None  # FIX: track thread

        if _HAS_CTYPES:
            try:
                self._user32 = ctypes.windll.user32
                self._ready = True
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
            self._INPUT = INPUT
            self._INPUT_sz = ctypes.sizeof(INPUT)

    def is_connected(self):
        return self._connected

    def connect(self):
        self._connected = True
        mode = 'ctypes SendInput' if self._ready else 'no-op (non-Windows)'
        print(f"[Software] Ready ({mode})")
        # FIX: always start listener on connect
        self.StartButtonListener()
        return True

    def disconnect(self):
        self._connected = False  # FIX: poll thread checks this and exits

    def StartButtonListener(self):
        # FIX: don't spawn duplicate threads
        if not self._ready:
            return
        if self._poll_thread and self._poll_thread.is_alive():
            return

        def _poll():
            while self._connected:
                with self._lock:
                    for n, vk in self._VK.items():
                        self._btn[n] = bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
                time.sleep(0.005)

        self._poll_thread = threading.Thread(target=_poll, daemon=True)
        self._poll_thread.start()

    def get_button_state(self, btn):
        with self._lock:
            return self._btn.get(btn, False)

    def simple_move_mouse(self, x, y):
        if not self._ready or (x == 0 and y == 0):
            return
        try:
            inp = self._INPUT(type=0)
            inp.mi = self._MOUSEINPUT(
                dx=x, dy=y, mouseData=0, dwFlags=0x0001, time=0, dwExtraInfo=None
            )
            self._user32.SendInput(1, ctypes.byref(inp), self._INPUT_sz)
        except Exception as e:
            print(f"[Software] move error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  KMBoxController  (UDP 2-PC)
# ══════════════════════════════════════════════════════════════════════════════
class KMBoxController:
    CMD_MOVE = 0x0001
    CMD_MONITOR = 0x0100
    CMD_CONNECT = 0x000E
    _BTN = {"LMB": 1, "RMB": 2, "MMB": 4, "M4": 8, "M5": 16}

    def __init__(self):
        self._sock = None
        self._connected = False
        self._mac = b'\x00\x00\x00\x00'
        self._rand = 0
        self._seq = 0
        self._lock = threading.Lock()
        # FIX: cache last known button states to avoid hammering UDP per tick
        self._btn_cache: dict = {}
        self._monitor_thread: threading.Thread | None = None

    def is_connected(self):
        return self._connected

    def connect(self):
        cfg = app_state.get_kmbox_config()
        ip, port = cfg["ip"], int(cfg["port"]) if cfg["port"] else 1408
        uuid = cfg["uuid"].replace("-", "").replace(" ", "")
        self._mac = bytes.fromhex(uuid[:8]) if len(uuid) >= 8 else b'\x00\x00\x00\x00'
        try:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._rand = random.randint(1, 0xFFFFFFFF)
            self._seq = 0
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            self._sock = sock
            sock.sendto(self._build(self.CMD_CONNECT, b'\x00' * 4), (ip, port))
            resp, _ = sock.recvfrom(1024)
            if len(resp) >= 16:
                self._connected = True
                sock.settimeout(0.05)
                print(f"[KMBox] Connected {ip}:{port}")
                self.StartButtonListener()
                return True
            raise Exception(f"Bad handshake ({len(resp)} bytes)")
        except Exception as e:
            print(f"[KMBox] Failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        self._connected = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    # FIX: poll button states in background thread, not per move tick
    def StartButtonListener(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        def _poll():
            while self._connected:
                for btn, mask in self._BTN.items():
                    with self._lock:
                        try:
                            if self._sock is None:
                                break
                            self._sock.sendto(
                                self._build(self.CMD_MONITOR, struct.pack('<I', mask)),
                                self._addr()
                            )
                            resp, _ = self._sock.recvfrom(1024)
                            state = bool(struct.unpack_from('<I', resp, 16)[0] & mask) if len(resp) >= 20 else False
                            self._btn_cache[btn] = state
                        except socket.timeout:
                            self._btn_cache[btn] = False
                        except Exception:
                            self._connected = False
                            break
                time.sleep(0.01)

        self._monitor_thread = threading.Thread(target=_poll, daemon=True)
        self._monitor_thread.start()

    def get_button_state(self, btn):
        return self._btn_cache.get(btn, False)

    def simple_move_mouse(self, x, y):
        if not self._connected:
            return
        with self._lock:
            try:
                self._sock.sendto(
                    self._build(self.CMD_MOVE, struct.pack('<hhhB', x, y, 0, 0)),
                    self._addr()
                )
            except Exception:
                self._connected = False

    def _build(self, cmd, payload):
        self._seq += 1
        return (
            self._mac
            + struct.pack('<III', self._rand, self._seq, cmd)
            + payload
            + b'\x00' * max(0, 48 - len(payload))
        )

    def _addr(self):
        c = app_state.get_kmbox_config()
        return (c["ip"], int(c["port"]) if c["port"] else 1408)


software_controller = SoftwareController()
kmbox_controller = KMBoxController()


def get_active_controller():
    t = app_state.get_controller_type()
    if t == "kmbox":
        return kmbox_controller
    if t == "software":
        return software_controller
    return makcu_controller


# ══════════════════════════════════════════════════════════════════════════════
#  Config file helpers
# ══════════════════════════════════════════════════════════════════════════════
CONFIG_DIR = os.path.join(os.path.dirname(__file__), 'configs')
os.makedirs(CONFIG_DIR, exist_ok=True)
DEFAULT_CONFIG_FILE = "default.json"


def get_config_path(f):
    return os.path.join(CONFIG_DIR, f)


def read_configs(config_file=None):
    f = config_file or DEFAULT_CONFIG_FILE
    p = get_config_path(f)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_configs(configs, config_file=None):
    f = config_file or DEFAULT_CONFIG_FILE
    p = get_config_path(f)
    tmp = p + ".tmp"
    try:
        with open(tmp, 'w') as fh:
            json.dump(configs, fh, indent=4)
        os.replace(tmp, p)
    except OSError as e:
        print(f"[CONFIG] Write error: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass


def list_config_files():
    try:
        return sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith('.json'))
    except Exception:
        return []


def create_config_file(filename):
    if not filename.endswith('.json'):
        filename += '.json'
    fp = get_config_path(filename)
    if os.path.exists(fp):
        raise HTTPException(400, "Config file already exists.")
    try:
        with open(fp, 'w') as fh:
            json.dump({}, fh)
        return filename
    except OSError as e:
        raise HTTPException(500, str(e))


def delete_config_file(filename):
    if filename == DEFAULT_CONFIG_FILE:
        raise HTTPException(400, "Cannot delete default config.")
    fp = get_config_path(filename)
    if not os.path.exists(fp):
        raise HTTPException(404, "Not found.")
    try:
        os.remove(fp)
    except OSError as e:
        raise HTTPException(500, str(e))


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


class ToggleButtonConfig(BaseModel):    button: str
class TriggerModeConfig(BaseModel):     mode: str
class ControllerTypeConfig(BaseModel):  controller: str
class ConfigFileRequest(BaseModel):     filename: str


class KMBoxConfigRequest(BaseModel):
    ip:   str = Field(..., min_length=7, max_length=64)
    port: int = Field(default=1408, ge=1, le=65535)
    uuid: str = Field(default="")


class HumanizeConfig(BaseModel):
    jitter_strength: float = Field(..., ge=0.0, le=1.0)
    smooth_factor:   float = Field(..., ge=0.0, le=0.95)


# ══════════════════════════════════════════════════════════════════════════════
#  AppState
# ══════════════════════════════════════════════════════════════════════════════
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
        self.toggle_button           = "M5"
        self.current_config_file     = DEFAULT_CONFIG_FILE
        self.trigger_mode            = "LMB"
        self.controller_type         = "makcu"
        self.kmbox_ip                = "192.168.2.188"
        self.kmbox_port              = 1408
        self.kmbox_uuid              = ""
        self.lock                    = threading.Lock()

    def _g(self, a):
        with self.lock:
            return getattr(self, a)

    def _s(self, a, v):
        with self.lock:
            setattr(self, a, v)

    def set_active_value(self, v):
        # FIX: NaN guard
        if v != v:
            return
        self._s('active_pull_down_value', max(0, min(300, v)))

    def get_active_value(self):          return self._g('active_pull_down_value')
    def set_horizontal_value(self, v):
        if v != v:
            return
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
        with self.lock:
            return self.pull_down_curve, self.horizontal_curve

    def set_jitter(self, v):   self._s('jitter_strength', max(0.0, min(1.0, v)))
    def get_jitter(self):      return self._g('jitter_strength')
    # FIX: clamp smooth to 0.94 max — 0.95 causes near-zero alpha which freezes smoother
    def set_smooth(self, v):   self._s('smooth_factor', max(0.0, min(0.94, v)))
    def get_smooth(self):      return self._g('smooth_factor')
    def get_enabled(self):     return self._g('is_enabled')

    def toggle_enabled(self):
        with self.lock:
            self.is_enabled = not self.is_enabled
            return self.is_enabled

    def set_toggle_button(self, b):
        with self.lock:
            if b in VALID_TOGGLE_BTNS:
                self.toggle_button = b
                return b
            return None

    def get_toggle_button(self):         return self._g('toggle_button')

    def set_current_config_file(self, f):
        if not f.endswith('.json'):
            f += '.json'
        self._s('current_config_file', f)
        return f

    def get_current_config_file(self):   return self._g('current_config_file')

    def set_trigger_mode(self, m):
        with self.lock:
            if m in VALID_TRIGGER_MODES:
                self.trigger_mode = m
                return m
            return None

    def get_trigger_mode(self):          return self._g('trigger_mode')

    def set_controller_type(self, c):
        with self.lock:
            if c in VALID_CONTROLLERS:
                self.controller_type = c
                return c
            return None

    def get_controller_type(self):       return self._g('controller_type')

    def set_kmbox_config(self, ip, port, uuid):
        with self.lock:
            self.kmbox_ip   = ip
            self.kmbox_port = port
            self.kmbox_uuid = uuid

    def get_kmbox_config(self):
        with self.lock:
            return {"ip": self.kmbox_ip, "port": self.kmbox_port, "uuid": self.kmbox_uuid}

    def get_status(self):
        ctrl = get_active_controller()
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
                # FIX: expose controller connection state to UI
                "ctrl_connected":         ctrl.is_connected(),
            }


app_state = AppState()


# ══════════════════════════════════════════════════════════════════════════════
#  Humanization
# ══════════════════════════════════════════════════════════════════════════════
class _Smoother:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0

    def update(self, tx, ty, alpha):
        # FIX: clamp alpha so we never multiply by exactly 0
        alpha = max(0.06, min(1.0, alpha))
        self.x = alpha * tx + (1 - alpha) * self.x
        self.y = alpha * ty + (1 - alpha) * self.y
        return int(round(self.x)), int(round(self.y))

    def reset(self):
        self.x = 0.0
        self.y = 0.0


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


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=mouse_control_loop, daemon=True).start()
    yield


def mouse_control_loop():
    toggle_was = False
    hold_start  = None
    curve_tick  = 0
    smoother    = _Smoother()
    _listener_started = {"makcu": False, "kmbox": False, "software": False}

    while True:
        t0 = time.perf_counter()
        try:
            ctrl     = get_active_controller()
            ctrl_key = app_state.get_controller_type()

            if not ctrl.is_connected():
                time.sleep(0.5)
                ok = ctrl.connect()
                if ok and not _listener_started[ctrl_key]:
                    # FIX: ensure StartButtonListener is called after successful connect
                    ctrl.StartButtonListener()
                    _listener_started[ctrl_key] = True
                continue

            # FIX: call StartButtonListener once after first successful connection
            if not _listener_started[ctrl_key]:
                ctrl.StartButtonListener()
                _listener_started[ctrl_key] = True

            btn     = app_state.get_toggle_button()
            pressed = ctrl.get_button_state(btn)
            if pressed and not toggle_was:
                enabled = app_state.toggle_enabled()
                smoother.reset()
                hold_start = None  # FIX: reset hold_start on toggle too
            toggle_was = pressed

            lmb  = ctrl.get_button_state("LMB")
            rmb  = ctrl.get_button_state("RMB")
            fire = (lmb and rmb) if app_state.get_trigger_mode() == "LMB+RMB" else lmb

            if app_state.get_enabled() and fire:
                now = time.perf_counter()
                if hold_start is None:
                    hold_start = now
                    curve_tick = 0
                hold_ms = (now - hold_start) * 1000.0

                pd_curve, hz_curve = app_state.get_curves()

                # Vertical
                v_delay  = app_state.get_vertical_delay()
                v_dur    = app_state.get_vertical_duration()
                v_active = (hold_ms >= v_delay) and (v_dur == 0 or hold_ms <= v_delay + v_dur)
                if v_active:
                    raw_y = (
                        pd_curve[min(curve_tick, len(pd_curve) - 1)] / 5.0
                        if pd_curve
                        else app_state.get_active_value() / 5.0
                    )
                else:
                    raw_y = 0.0

                # Horizontal
                h_delay = app_state.get_horizontal_delay()
                h_dur   = app_state.get_horizontal_duration()
                raw_x   = 0.0
                if hold_ms >= h_delay and (h_dur == 0 or hold_ms <= h_delay + h_dur):
                    raw_x = (
                        hz_curve[min(curve_tick, len(hz_curve) - 1)] / 5.0
                        if hz_curve
                        else app_state.get_horizontal_value() / 5.0
                    )

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
        if coarse > 0:
            time.sleep(coarse)
        while (time.perf_counter() - t0) < TICK_S:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  FastAPI endpoints
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
                        try:
                            getattr(app_state, fn)(conv(msg[k]))
                        except Exception:
                            pass

                # FIX: handle pull_down_curve:null explicitly to clear curve
                if "pull_down_curve" in msg:
                    v = msg["pull_down_curve"]
                    _, hz = app_state.get_curves()
                    app_state.set_curves(v if isinstance(v, list) and len(v) > 0 else None, hz)

            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass


@app.get("/status")
async def status():
    return app_state.get_status()


@app.post("/toggle")
async def toggle():
    return {"is_enabled": app_state.toggle_enabled()}


@app.post("/toggle-button")
async def set_toggle_button(c: ToggleButtonConfig):
    r = app_state.set_toggle_button(c.button)
    if r is None:
        raise HTTPException(400, f"Must be one of {VALID_TOGGLE_BTNS}")
    return {"toggle_button": r}


@app.post("/trigger-mode")
async def set_trigger_mode(c: TriggerModeConfig):
    r = app_state.set_trigger_mode(c.mode)
    if r is None:
        raise HTTPException(400, f"Must be one of {VALID_TRIGGER_MODES}")
    return {"trigger_mode": r}


@app.post("/controller-type")
async def set_controller_type(c: ControllerTypeConfig):
    r = app_state.set_controller_type(c.controller)
    if r is None:
        raise HTTPException(400, f"Must be one of {VALID_CONTROLLERS}")
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
    ok = kmbox_controller.connect()
    return {"connected": ok, "message": "Connected" if ok else "Failed — check IP/Port/UUID"}


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
    if not os.path.exists(get_config_path(f)):
        raise HTTPException(404, "Not found.")
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
    if not key:
        raise HTTPException(400, "Name cannot be empty.")
    entry: dict = {
        "name":                   config.name,
        "tags":                   config.tags or {},
        # FIX: use consistent key 'pull_down' for storage (matches legacy configs)
        "pull_down":              config.pull_down_value,
        "vertical_delay_ms":      config.vertical_delay_ms,
        "vertical_duration_ms":   config.vertical_duration_ms,
        "horizontal":             config.horizontal_value,
        "horizontal_delay_ms":    config.horizontal_delay_ms,
        "horizontal_duration_ms": config.horizontal_duration_ms,
    }
    if config.pull_down_curve  is not None:
        entry["pull_down_curve"]  = config.pull_down_curve
    if config.horizontal_curve is not None:
        entry["horizontal_curve"] = config.horizontal_curve
    cfgs[key] = entry
    write_configs(cfgs, cf)
    return {"message": f"Saved '{key}'", "key": key}


@app.delete("/configs/{key:path}")
async def delete_config(key: str):
    cf   = app_state.get_current_config_file()
    cfgs = read_configs(cf)
    if key not in cfgs:
        raise HTTPException(404, "Config not found.")
    del cfgs[key]
    write_configs(cfgs, cf)
    return {"message": "Deleted."}


@app.post("/humanize")
async def set_humanize(cfg: HumanizeConfig):
    app_state.set_jitter(cfg.jitter_strength)
    app_state.set_smooth(cfg.smooth_factor)
    return {
        "jitter_strength": app_state.get_jitter(),
        "smooth_factor":   app_state.get_smooth(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  UI  v4.1
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>RVN v4.1</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Noto+Sans+Thai:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#09090c;--sf:#111116;--bd:#1c1c24;--bd2:#252530;
  --tx:#c0c0d0;--mu:#3a3a4c;
  --ac:#5bf0a0;--ac2:#3cd880;--bl:#4ea8ff;--rd:#ff4d6a;--yl:#ffc84a;--vi:#c084fc;
  --mo:'JetBrains Mono',monospace;--sa:'Noto Sans Thai',sans-serif;
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

/* CONNECTION DOT — new in v4.1 */
.conn-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--mu);margin-left:auto;transition:background .4s,box-shadow .4s;}
.conn-dot.ok{background:var(--ac);box-shadow:0 0 7px rgba(91,240,160,.5);}
.conn-dot.bad{background:var(--rd);box-shadow:0 0 7px rgba(255,77,106,.35);}

.card{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);padding:15px 17px;margin-bottom:8px;transition:border-color .2s;}
.card:hover{border-color:var(--bd2);}
.clabel{font-family:var(--mo);font-size:.57rem;letter-spacing:2px;text-transform:uppercase;color:var(--mu);margin-bottom:11px;display:flex;align-items:center;flex-wrap:wrap;gap:6px;}

/* STATUS */
#toggle-btn{width:100%;padding:13px;border-radius:8px;font-family:var(--mo);font-size:.9rem;font-weight:700;letter-spacing:3px;cursor:pointer;border:1.5px solid var(--bd2);background:var(--sf);color:var(--mu);transition:all .3s;}
#toggle-btn.enabled{background:#03120a;border-color:#195230;color:var(--ac);box-shadow:0 0 28px rgba(91,240,160,.1);}
#toggle-btn.disabled{background:#130408;border-color:#521428;color:var(--rd);}
#toggle-btn:active{transform:scale(.98);}
.trow{display:flex;align-items:center;gap:8px;margin-top:9px;font-size:.75rem;color:var(--mu);}

/* TABS */
.tabs{display:flex;border:1px solid var(--bd);border-radius:8px;overflow:hidden;margin-bottom:8px;}
.tab{flex:1;padding:10px 4px;background:var(--sf);border:none;color:var(--mu);font-family:var(--mo);font-size:.57rem;letter-spacing:1.5px;text-transform:uppercase;cursor:pointer;transition:all .2s;}
.tab:not(:first-child){border-left:1px solid var(--bd);}
.tab.active{background:var(--bd2);color:#fff;}
.tc{display:none;}.tc.active{display:block;}

select,input[type=text]{background:var(--bg);border:1px solid var(--bd2);border-radius:7px;color:var(--tx);padding:8px 11px;font-size:.84rem;font-family:var(--sa);outline:none;transition:border-color .2s;width:100%;}
select:focus,input[type=text]:focus{border-color:#2a4060;}
select{cursor:pointer;}select:disabled{opacity:.25;cursor:not-allowed;}
#tbs{width:auto;padding:4px 8px;font-size:.8rem;background:var(--bg);border:1px solid var(--bd2);border-radius:5px;color:var(--mu);}

.num{background:transparent;border:1px solid transparent;border-radius:6px;color:#fff;font-family:var(--mo);font-size:1.7rem;font-weight:700;width:100%;padding:0 4px 2px;outline:none;-moz-appearance:textfield;transition:border-color .2s,background .2s;}
.num::-webkit-outer-spin-button,.num::-webkit-inner-spin-button{-webkit-appearance:none;margin:0;}
.num:hover{border-color:var(--bd2);}.num:focus{border-color:#2a4060;background:#0a1018;}
.num-row{display:flex;align-items:baseline;gap:4px;margin-bottom:6px;}
.unit{font-family:var(--mo);font-size:.62rem;color:var(--mu);}
.num-sm{font-size:1.15rem !important;}

input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;border-radius:2px;background:var(--bd2);outline:none;}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 0 4px rgba(255,255,255,.12);transition:transform .15s,box-shadow .15s;}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.2);box-shadow:0 0 10px rgba(255,255,255,.28);}
input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;background:#fff;cursor:pointer;border:none;}
.hint{font-size:.68rem;color:var(--mu);margin-top:6px;line-height:1.6;}

.btn{padding:8px 13px;border:1px solid var(--bd2);border-radius:7px;font-family:var(--mo);font-size:.68rem;font-weight:700;letter-spacing:.7px;cursor:pointer;transition:all .2s;background:var(--sf);color:#bbb;}
.btn:hover{background:var(--bd2);color:#fff;transform:translateY(-1px);}.btn:active{transform:scale(.98);}
.btn:disabled{opacity:.25;cursor:not-allowed;transform:none;pointer-events:none;}
.btn-s{background:#06091a;border-color:#181a4a;color:#88aaff;}.btn-s:hover{background:#0c1030;}
.btn-d{background:#150508;border-color:#4a1018;color:#ff7070;}.btn-d:hover{background:#200810;}
.btn-g{background:#031008;border-color:#0d3020;color:var(--ac);}.btn-g:hover{background:#071a10;}
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
.cacts{display:flex;gap:6px;margin-top:7px;align-items:center;flex-wrap:wrap;}

.tag-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;min-height:22px;}
.tag-chip{display:inline-flex;align-items:center;gap:3px;background:#0a0a14;border:1px solid var(--bd2);border-radius:20px;padding:3px 10px;font-size:.7rem;color:#aaa;font-family:var(--mo);}
.tag-chip .rm{cursor:pointer;color:var(--rd);font-size:.78em;margin-left:2px;line-height:1;}
.tag-chip .rm:hover{color:#ff9090;}
.add-tag-row{display:flex;gap:6px;margin-bottom:10px;}
.add-tag-row input{flex:1;}

.sp-preview{font-family:var(--mo);font-size:.64rem;color:var(--mu);min-height:1.3em;margin-bottom:8px;word-break:break-all;padding:6px 9px;background:var(--bg);border-radius:5px;border:1px solid var(--bd);}
.sp-preview.ready{color:var(--ac);border-color:#0c2e20;}
.sp-actions{display:flex;gap:8px;margin-top:10px;}
.sp-actions .btn{flex:1;}

.warn{background:#120802;border:1px solid #481a08;border-radius:7px;color:#cc7744;font-size:.73rem;
  padding:8px 12px;margin-bottom:10px;display:none;align-items:center;gap:8px;font-family:var(--mo);}
.warn.show{display:flex;}

/* FIX: toast notification for feedback */
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

/* FIX: software mode notice */
.sw-notice{background:#03080f;border:1px solid #0a2035;border-radius:7px;padding:9px 12px;font-size:.7rem;color:var(--bl);line-height:1.7;font-family:var(--mo);}
.sw-notice .w-line{color:#5a3a10;display:block;margin-top:4px;}
</style>
</head>
<body>
<div class="w">
  <div class="hdr">
    <div class="logo">R<em>V</em>N</div>
    <span class="vtag">v4.1 — RCS</span>
    <!-- FIX: live connection indicator -->
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
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="recoil">Recoil</button>
    <button class="tab" data-tab="humanize">Humanize</button>
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
      <div class="hint">Delay = หน่วงก่อนเริ่ม · Duration = นานแค่ไหน · 0 Duration = ตลอด</div>
    </div>

    <div class="card">
      <div class="clabel">Horizontal <span id="cph" class="cpill" style="display:none">CURVE</span></div>
      <input type="number" class="num" id="hv" value="0" min="-300" max="300" step="0.001">
      <input type="range" min="-300" max="300" value="0" id="hs">
      <div class="hint" style="margin-bottom:10px">Negative = ซ้าย · Positive = ขวา · 0 = ปิด</div>
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
      <div class="hint">0 Duration = ตลอดเวลา</div>
    </div>

    <div class="card">
      <div class="clabel">Recoil Curve <span style="font-size:.85em;color:var(--mu);letter-spacing:0;font-family:var(--sa);">— override ค่าคงที่</span></div>
      <div class="hint" style="margin-bottom:9px">
        ลากเพื่อวาด · แกน X = เวลา · แกน Y = แรงดีด (บน = มาก)<br>
        <span style="color:#223a28;">— — —</span> เส้นประเขียว = ค่าคงที่ &nbsp; <span style="color:#3ab070;">——</span> เส้นเขียว = curve ที่วาด
      </div>
      <div class="ceditor">
        <canvas id="curve-canvas" height="120"></canvas>
        <div class="cacts">
          <button class="btn btn-s" id="curve-load-btn" style="font-size:.6rem;padding:5px 9px">From Value</button>
          <button class="btn btn-g" id="curve-apply-btn" style="font-size:.6rem;padding:5px 9px">Apply</button>
          <button class="btn btn-d" id="curve-clear-btn" style="font-size:.6rem;padding:5px 9px">Clear</button>
          <span id="curve-pts" style="font-size:.6rem;color:var(--mu);margin-left:auto;font-family:var(--mo)">0 pts</span>
        </div>
      </div>
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
      <div class="hint" style="margin-bottom:13px">Gaussian noise ต่อ tick · 0 = ปิด · 1 = สูงสุด</div>
      <div class="hrow">
        <span class="hlbl">Smooth</span>
        <!-- FIX: max 0.94 matches server clamp -->
        <input type="range" id="ss" min="0" max="0.94" step="0.01" value="0.60" style="flex:1">
        <span class="hval" id="sv2">0.60</span>
      </div>
      <div class="hint">Exponential smoothing · 0 = ดิบ · 0.94 = นุ่มมาก</div>
    </div>
  </div>

  <!-- ═══ SETTINGS ═══════════════════════════════════════════════════════════ -->
  <div id="tab-settings" class="tc">

    <div class="card">
      <div class="clabel">Trigger Mode</div>
      <select id="trig">
        <option value="LMB">LMB เท่านั้น</option>
        <option value="LMB+RMB">LMB + RMB พร้อมกัน (ADS + ยิง)</option>
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
      <div class="clabel">KMBox Connection</div>
      <label style="font-size:.64rem;color:var(--mu);display:block;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">IP Address</label>
      <input type="text" id="km-ip" placeholder="192.168.2.188" style="margin-bottom:8px;">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:9px;">
        <div>
          <label style="font-size:.64rem;color:var(--mu);display:block;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Port</label>
          <input type="text" id="km-port" placeholder="1408">
        </div>
        <div>
          <label style="font-size:.64rem;color:var(--mu);display:block;margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">UUID (opt)</label>
          <input type="text" id="km-uuid" placeholder="xxxxxxxx">
        </div>
      </div>
      <div class="row">
        <button class="btn btn-s" id="km-save" style="flex:1">บันทึก</button>
        <button class="btn btn-g" id="km-conn" style="flex:1">เชื่อมต่อ</button>
      </div>
      <div id="km-msg" style="margin-top:7px;font-size:.7rem;color:var(--mu);min-height:1.2em;font-family:var(--mo)"></div>
    </div>

    <!-- FIX: clearer software mode panel -->
    <div class="card" id="sw-card" style="display:none">
      <div class="clabel">Software Direct Mode</div>
      <div class="sw-notice">
        SendInput Windows API<br>
        ทำงานบน <strong>1-PC</strong> โดยไม่ต้องมีฮาร์ดแวร์ภายนอก<br>
        <span class="w-line">⚠ ต้องรัน Python บน Windows เท่านั้น<br>
        ⚠ อาจตรวจจับได้โดย- Risk (BattleEye / EAC)</span>
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
      <!-- FIX: wider select for readability -->
      <select id="cfgdd" size="6" style="font-family:var(--mo);font-size:.74rem;"></select>
    </div>

    <div class="card">
      <div class="clabel">Save Config</div>
      <div class="warn" id="warn"><span>!</span><span id="warn-txt">error</span></div>

      <div style="margin-bottom:10px;">
        <label style="font-size:.62rem;color:var(--mu);display:block;margin-bottom:5px;letter-spacing:.5px;text-transform:uppercase;font-family:var(--mo);">Name</label>
        <input type="text" id="cfg-name" placeholder="e.g. AK47 · MP5 Comp · M249 Iron">
      </div>

      <div class="sdiv">Tags <span style="letter-spacing:0;font-family:var(--sa);font-size:.88em;color:#333;text-transform:none;">— game, attach, barrel, ...</span></div>

      <div id="tag-chips" class="tag-row"></div>
      <div class="add-tag-row">
        <input type="text" id="tag-key" placeholder="key" style="flex:0 0 88px;">
        <input type="text" id="tag-val" placeholder="value">
        <button class="btn" id="add-tag-btn" style="flex:0 0 auto;padding:7px 10px;font-size:.67rem;">+ Tag</button>
      </div>

      <div class="sp-preview" id="sp-preview">กรอกชื่อก่อน…</div>
      <div class="sp-actions">
        <button class="btn btn-s" id="save-btn">Save</button>
        <button class="btn" id="overwrite-btn" style="background:#050c14;border-color:#10283a;color:#5599cc;">Overwrite</button>
        <button class="btn btn-d" id="delete-btn">Delete</button>
      </div>
    </div>

  </div>
</div>

<!-- FIX: toast for non-blocking feedback -->
<div id="toast"></div>

<script>
document.addEventListener('DOMContentLoaded', async () => {
const $ = id => document.getElementById(id);

// ── Refs ──────────────────────────────────────────────────────────────────────
const sl=$('sl'),sv=$('sv'), hs=$('hs'),hv=$('hv'),
      ds=$('ds'),dv=$('dv'), us=$('us'),uv=$('uv'),
      vds=$('vds'),vdv=$('vdv'), vdus=$('vdus'),vduv=$('vduv'),
      jss=$('js'),jvd=$('jv'), sss=$('ss'),smoothDisp=$('sv2');

// ── Toast ─────────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, color='var(--ac)') {
  const t = $('toast');
  t.textContent = msg;
  t.style.color = color;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws;
function connectWs() {
  const p = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(p + '//' + location.host + '/ws');
  ws.onopen = sendAll;
  ws.onclose = () => setTimeout(connectWs, 2000);
}
connectWs();

function sendAll() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({
    pull_down:              +sv.value  || 0,
    horizontal:             +hv.value  || 0,
    horizontal_delay_ms:    +dv.value  || 0,
    horizontal_duration_ms: +uv.value  || 0,
    vertical_delay_ms:      +vdv.value || 0,
    vertical_duration_ms:   +vduv.value|| 0,
    jitter_strength:        +jss.value || 0,
    smooth_factor:          +sss.value || 0,
  }));
}

// FIX: NaN guard on sync
function safeNum(v, fallback=0) {
  const n = parseFloat(v);
  return isNaN(n) ? fallback : n;
}

function sync(r, i, cb) {
  r.oninput = () => { i.value = r.value; if (cb) cb(); sendAll(); };
  i.oninput = () => {
    const v = safeNum(i.value);
    r.value = v; i.value = v;
    if (cb) cb(); sendAll();
  };
}
sync(sl, sv, drawCurve); sync(hs, hv);
sync(ds, dv); sync(us, uv);
sync(vds, vdv); sync(vdus, vduv);
jss.oninput = () => { jvd.textContent = parseFloat(jss.value).toFixed(2); sendAll(); };
sss.oninput = () => { smoothDisp.textContent = parseFloat(sss.value).toFixed(2); sendAll(); };

// ── Tabs + keyboard shortcut ──────────────────────────────────────────────────
const TABS = ['recoil','humanize','settings'];
function switchTab(name) {
  document.querySelectorAll('.tab,.tc').forEach(e => e.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
  $('tab-' + name).classList.add('active');
}
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => switchTab(t.dataset.tab);
});
// FIX: Alt+1/2/3 keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.altKey && e.key >= '1' && e.key <= '3') {
    e.preventDefault();
    switchTab(TABS[+e.key - 1]);
  }
});

// ── Connection dot update ─────────────────────────────────────────────────────
function setConnDot(ok) {
  [$('conn-dot'), $('conn-dot2')].forEach(d => {
    d.classList.toggle('ok', ok);
    d.classList.toggle('bad', !ok);
    d.title = ok ? 'Controller: Connected' : 'Controller: Disconnected';
  });
}

// ── Status ────────────────────────────────────────────────────────────────────
function setBtn(on) {
  $('toggle-btn').textContent = on ? '■ ON' : '○ OFF';
  $('toggle-btn').className = on ? 'enabled' : 'disabled';
}

$('toggle-btn').onclick = () =>
  fetch('/toggle', { method: 'POST' }).then(r => r.json()).then(d => setBtn(d.is_enabled));

$('tbs').onchange = () =>
  fetch('/toggle-button', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ button: $('tbs').value })
  });

function getStatus() {
  fetch('/status').then(r => r.json()).then(d => {
    setBtn(d.is_enabled);
    if (d.toggle_button) $('tbs').value = d.toggle_button;
    if (d.trigger_mode)  $('trig').value = d.trigger_mode;
    if (d.controller_type) { $('ctrl').value = d.controller_type; ctrlUI(d.controller_type, d.ctrl_connected); }
    if (d.current_config_file) $('cfg-badge').textContent = d.current_config_file.replace('.json','');
    if (d.jitter_strength !== undefined) { jss.value = d.jitter_strength; jvd.textContent = (+d.jitter_strength).toFixed(2); }
    if (d.smooth_factor   !== undefined) { sss.value = d.smooth_factor;   smoothDisp.textContent = (+d.smooth_factor).toFixed(2); }
    $('cpv').style.display = d.has_pull_curve  ? 'inline-flex' : 'none';
    $('cph').style.display = d.has_horiz_curve ? 'inline-flex' : 'none';
    if (d.kmbox_ip && !$('km-ip').value) { $('km-ip').value = d.kmbox_ip; $('km-port').value = d.kmbox_port; }
    // FIX: update connection dot from status
    setConnDot(!!d.ctrl_connected);
  }).catch(() => {});
}
getStatus();
setInterval(getStatus, 1000);

// ── Controller ────────────────────────────────────────────────────────────────
function ctrlUI(ct, connected) {
  $('kmbox-card').style.display = ct === 'kmbox'    ? 'block' : 'none';
  $('sw-card').style.display    = ct === 'software' ? 'block' : 'none';
  const M = { makcu: ['mk','MAKCU 2-PC'], kmbox: ['km','KMBox 2-PC'], software: ['sw','Software 1-PC'] };
  const [cls, txt] = M[ct] || ['mk','—'];
  $('cbw').innerHTML = `<span class="cbadge ${cls}">${txt}</span>`;
  // FIX: show software ready state
  if (ct === 'software') {
    $('sw-status').textContent = connected ? '✓ ready — SendInput active' : '✗ Not ready (Windows only)';
    $('sw-status').style.color = connected ? 'var(--ac)' : 'var(--rd)';
  }
}

$('ctrl').onchange = () => {
  const ct = $('ctrl').value;
  fetch('/controller-type', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ controller: ct })
  }).then(() => ctrlUI(ct, false));
};

$('trig').onchange = () =>
  fetch('/trigger-mode', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: $('trig').value })
  });

const kmsg = (t, c) => { $('km-msg').textContent = t; $('km-msg').style.color = c || 'var(--mu)'; };

$('km-save').onclick = () => {
  const ip = $('km-ip').value.trim(), port = +$('km-port').value || 1408, uuid = $('km-uuid').value.trim();
  if (!ip) { kmsg('กรุณากรอก IP', 'var(--rd)'); return; }
  fetch('/kmbox-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ip, port, uuid })
  }).then(() => kmsg('✓ บันทึกแล้ว', 'var(--ac)')).catch(() => kmsg('ไม่สำเร็จ', 'var(--rd)'));
};

$('km-conn').onclick = () => {
  kmsg('กำลังเชื่อมต่อ...');
  fetch('/kmbox-connect', { method: 'POST' }).then(r => r.json())
    .then(d => kmsg(d.connected ? '✓ เชื่อมต่อสำเร็จ' : '✗ ' + d.message, d.connected ? 'var(--ac)' : 'var(--rd)'))
    .catch(() => kmsg('เชื่อมต่อไม่ได้', 'var(--rd)'));
};

// ── Curve editor ──────────────────────────────────────────────────────────────
const cv = $('curve-canvas'), ctx = cv.getContext('2d');
let pts = [], drawing = false;

// FIX: use ResizeObserver instead of window resize + setTimeout race
function resize() {
  const r = cv.getBoundingClientRect();
  const w = Math.floor(r.width) || cv.offsetWidth || 320;
  if (cv.width !== w) { cv.width = w; cv.height = 120; drawCurve(); }
}
new ResizeObserver(resize).observe(cv);
requestAnimationFrame(resize);

function drawCurve() {
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#080b0f'; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = '#191e28'; ctx.lineWidth = 1;
  [1,2,3].forEach(i => {
    ctx.beginPath(); ctx.moveTo(0, H*i/4); ctx.lineTo(W, H*i/4); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(W*i/4, 0); ctx.lineTo(W*i/4, H); ctx.stroke();
  });
  const refY = 1 - Math.min((safeNum(sv.value) / 300), 1);
  ctx.strokeStyle = 'rgba(50,160,80,.5)'; ctx.lineWidth = 1.5; ctx.setLineDash([5, 5]);
  ctx.beginPath(); ctx.moveTo(0, refY*H); ctx.lineTo(W, refY*H); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#2a3a4a'; ctx.font = '8px monospace';
  ctx.fillText('300', 3, 10); ctx.fillText('0', 3, H-3);
  if (pts.length < 2) { $('curve-pts').textContent = '0 pts'; return; }
  const sorted = [...pts].sort((a,b) => a.x - b.x);
  ctx.beginPath();
  ctx.moveTo(sorted[0].x*W, H);
  sorted.forEach(p => ctx.lineTo(p.x*W, p.y*H));
  ctx.lineTo(sorted[sorted.length-1].x*W, H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(91,240,160,.05)'; ctx.fill();
  ctx.beginPath(); ctx.strokeStyle = '#5bf0a0'; ctx.lineWidth = 2;
  sorted.forEach((p,i) => { const x=p.x*W, y=p.y*H; i ? ctx.lineTo(x,y) : ctx.moveTo(x,y); });
  ctx.stroke();
  ctx.fillStyle = 'rgba(91,240,160,.8)';
  const step = Math.max(1, Math.floor(sorted.length / 16));
  sorted.filter((_,i) => i % step === 0 || i === sorted.length-1).forEach(p => {
    ctx.beginPath(); ctx.arc(p.x*W, p.y*H, 2.3, 0, Math.PI*2); ctx.fill();
  });
  $('curve-pts').textContent = pts.length + ' pts';
}

function normM(e) {
  const r = cv.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (e.clientX-r.left)/r.width)), y: Math.max(0, Math.min(1, (e.clientY-r.top)/r.height)) };
}
function normT(e) {
  const r = cv.getBoundingClientRect(), t = e.touches[0];
  return { x: Math.max(0, Math.min(1, (t.clientX-r.left)/r.width)), y: Math.max(0, Math.min(1, (t.clientY-r.top)/r.height)) };
}

cv.addEventListener('mousedown', e => { e.preventDefault(); drawing=true; pts=[normM(e)]; drawCurve(); });
cv.addEventListener('mousemove', e => {
  if (!drawing) return;
  const p=normM(e), l=pts[pts.length-1];
  if (Math.abs(p.x-l.x) > .007 || Math.abs(p.y-l.y) > .007) { pts.push(p); drawCurve(); }
});
cv.addEventListener('mouseup',    () => { drawing = false; });
cv.addEventListener('mouseleave', () => { drawing = false; });
cv.addEventListener('touchstart', e => { e.preventDefault(); drawing=true; pts=[normT(e)]; drawCurve(); }, { passive:false });
cv.addEventListener('touchmove',  e => {
  e.preventDefault(); if (!drawing) return;
  const p=normT(e), l=pts[pts.length-1];
  if (Math.abs(p.x-l.x) > .007 || Math.abs(p.y-l.y) > .007) { pts.push(p); drawCurve(); }
}, { passive:false });
cv.addEventListener('touchend', () => { drawing = false; });

$('curve-load-btn').onclick = () => {
  const v = Math.max(0, Math.min(300, safeNum(sv.value)));
  const N = 40;
  pts = Array.from({ length: N }, (_, i) => {
    const t = i / (N-1);
    const decay = Math.exp(-t * 1.6) * 0.45 + 0.55;
    return { x: t, y: 1 - Math.min(v * decay / 300, 1) };
  });
  drawCurve();
};

$('curve-apply-btn').onclick = () => {
  const c = getCurve();
  if (!c) { showWarn('วาด curve ก่อน'); return; }
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ pull_down_curve: c }));
  $('cpv').style.display = 'inline-flex';
  toast('✓ Curve applied');
  const b = $('curve-apply-btn'); b.textContent = 'Applied ✓';
  setTimeout(() => { b.textContent = 'Apply'; }, 1600);
};

$('curve-clear-btn').onclick = () => {
  pts = []; drawCurve(); $('cpv').style.display = 'none';
  // FIX: send empty list which server now correctly treats as null
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ pull_down_curve: [] }));
  toast('Curve cleared', 'var(--mu)');
};

function restoreCurve(arr) {
  if (!arr || arr.length < 2) { pts = []; drawCurve(); return; }
  const N = arr.length;
  pts = arr.map((v, i) => ({ x: i/(N-1), y: 1 - Math.min(v/300, 1) }));
  drawCurve();
}

function getCurve() {
  if (pts.length < 2) return null;
  return [...pts].sort((a,b) => a.x - b.x).map(p => parseFloat(((1-p.y)*300).toFixed(2)));
}

// ═══════════════════════════════════════════════════════════════════════════
//  TAGS
// ═══════════════════════════════════════════════════════════════════════════
let currentTags = {};

function renderTagChips() {
  const c = $('tag-chips'); c.innerHTML = '';
  Object.entries(currentTags).forEach(([k, v]) => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.innerHTML = `<span style="color:var(--bl)">${k}:</span><span>${v}</span><span class="rm" data-k="${k}">×</span>`;
    chip.querySelector('.rm').onclick = e => {
      delete currentTags[e.target.dataset.k]; renderTagChips(); updSavePreview();
    };
    c.appendChild(chip);
  });
}

$('add-tag-btn').onclick = () => {
  const k = $('tag-key').value.trim(), v = $('tag-val').value.trim();
  if (!k || !v) { showWarn('กรอกทั้ง key และ value'); return; }
  currentTags[k] = v; renderTagChips(); updSavePreview();
  $('tag-key').value = ''; $('tag-val').value = '';
};
$('tag-key').addEventListener('keydown', e => {
  if (e.key === 'Tab' && $('tag-key').value.trim()) { e.preventDefault(); $('tag-val').focus(); }
});
$('tag-val').addEventListener('keydown', e => { if (e.key === 'Enter') $('add-tag-btn').click(); });

function updSavePreview() {
  const name = $('cfg-name').value.trim();
  const pre  = $('sp-preview');
  if (!name) { pre.textContent = 'กรอกชื่อก่อน…'; pre.className = 'sp-preview'; return; }
  let txt = '"' + name + '"';
  if (Object.keys(currentTags).length > 0)
    txt += '  ' + Object.entries(currentTags).map(([k,v]) => `[${k}:${v}]`).join(' ');
  const vv = safeNum(sv.value), hh = safeNum(hv.value);
  txt += `  ↓${vv}`;
  if (hh !== 0) txt += `  ←→${hh}`;
  pre.textContent = txt; pre.className = 'sp-preview ready';
}
$('cfg-name').oninput = updSavePreview;

// ═══════════════════════════════════════════════════════════════════════════
//  CONFIG SYSTEM
// ═══════════════════════════════════════════════════════════════════════════
let cache = {}, allKeys = [], activeTagFilters = new Set();

function fetchConfigs() {
  fetch('/configs').then(r => r.json()).then(d => {
    cache = d; allKeys = Object.keys(d);
    buildTagFilters(); filterBrowse();
  }).catch(() => {});
}

function buildTagFilters() {
  const tagSet = new Set();
  Object.values(cache).forEach(cfg => {
    if (typeof cfg === 'object' && cfg.tags)
      Object.entries(cfg.tags).forEach(([k,v]) => tagSet.add(`${k}:${v}`));
  });
  const c = $('tag-filters'); c.innerHTML = '';
  [...tagSet].sort().forEach(tag => {
    const el = document.createElement('span');
    el.className = 'tfchip' + (activeTagFilters.has(tag) ? ' active' : '');
    el.textContent = tag;
    el.onclick = () => {
      activeTagFilters.has(tag) ? activeTagFilters.delete(tag) : activeTagFilters.add(tag);
      el.classList.toggle('active', activeTagFilters.has(tag));
      filterBrowse();
    };
    c.appendChild(el);
  });
  if (tagSet.size === 0) c.innerHTML = '<span style="font-size:.65rem;color:var(--mu);">ยังไม่มี tags</span>';
}

function filterBrowse() {
  const q    = $('search').value.toLowerCase();
  const prev = $('cfgdd').value;
  $('cfgdd').innerHTML = '<option value="">-- เลือก Config --</option>';
  for (const key of allKeys) {
    const cfg  = cache[key];
    const name = typeof cfg === 'object' ? (cfg.name || key) : key;
    const tags = typeof cfg === 'object' ? (cfg.tags || {}) : {};
    const pd   = typeof cfg === 'object' ? (cfg.pull_down ?? 0) : 0;
    const haystack = (name + ' ' + Object.entries(tags).map(([k,v]) => k+':'+v).join(' ')).toLowerCase();
    if (q && !haystack.includes(q)) continue;
    if (activeTagFilters.size > 0) {
      const ts = new Set(Object.entries(tags).map(([k,v]) => `${k}:${v}`));
      if (![...activeTagFilters].every(t => ts.has(t))) continue;
    }
    const tagStr = Object.entries(tags).map(([k,v]) => `[${k}:${v}]`).join(' ');
    const o = document.createElement('option');
    o.value = key;
    // FIX: show pull_down value in list for quick reference
    o.textContent = name + (tagStr ? ' ' + tagStr : '') + `  ↓${pd}`;
    $('cfgdd').appendChild(o);
  }
  $('cfgdd').value = prev;
  updBrowseLbl();
}

function updBrowseLbl() {
  const key = $('cfgdd').value, lbl = $('browse-lbl');
  if (!key) { lbl.textContent = 'Browse Configs'; return; }
  const cfg  = cache[key];
  const name = typeof cfg === 'object' ? (cfg.name || key) : key;
  lbl.innerHTML = 'Selected — <span style="color:var(--ac);font-family:var(--mo)">' + name + '</span>';
}

$('cfgdd').onchange = () => {
  const key = $('cfgdd').value; updBrowseLbl(); if (!key) return;
  const cfg = cache[key]; if (cfg == null) return;
  const pd  = typeof cfg === 'object' ? (cfg.pull_down ?? 0) : (cfg ?? 0);
  const hz  = typeof cfg === 'object' ? (cfg.horizontal ?? 0) : 0;
  const dl  = typeof cfg === 'object' ? (cfg.horizontal_delay_ms  ?? 500)  : 500;
  const du  = typeof cfg === 'object' ? (cfg.horizontal_duration_ms ?? 2000) : 2000;
  const vdl = typeof cfg === 'object' ? (cfg.vertical_delay_ms    ?? 0)    : 0;
  const vdu = typeof cfg === 'object' ? (cfg.vertical_duration_ms ?? 0)    : 0;
  sv.value = pd; sl.value = Math.round(pd);
  hv.value = hz; hs.value = Math.round(hz);
  dv.value = dl; ds.value = dl;
  uv.value = du; us.value = du;
  vdv.value = vdl; vds.value = vdl;
  vduv.value = vdu; vdus.value = vdu;
  const hasCurve = typeof cfg === 'object' && cfg.pull_down_curve;
  $('cpv').style.display = hasCurve ? 'inline-flex' : 'none';
  restoreCurve(hasCurve ? cfg.pull_down_curve : null);
  sendAll();
  if (ws && ws.readyState === 1)
    ws.send(JSON.stringify({ pull_down_curve: hasCurve ? cfg.pull_down_curve : [] }));
  const name = typeof cfg === 'object' ? (cfg.name || key) : key;
  $('cfg-name').value = name;
  currentTags = typeof cfg === 'object' && cfg.tags ? { ...cfg.tags } : {};
  renderTagChips(); updSavePreview();
  toast('✓ Loaded: ' + name);
};

$('search').oninput = filterBrowse;

function showWarn(msg) {
  $('warn-txt').textContent = msg;
  $('warn').classList.add('show');
  setTimeout(() => $('warn').classList.remove('show'), 3000);
}

function buildPayload(name) {
  const c = getCurve();
  return {
    name,
    tags: Object.keys(currentTags).length > 0 ? currentTags : null,
    pull_down_value:        safeNum(sv.value),
    vertical_delay_ms:      safeNum(vdv.value),
    vertical_duration_ms:   safeNum(vduv.value),
    horizontal_value:       safeNum(hv.value),
    horizontal_delay_ms:    safeNum(dv.value),
    horizontal_duration_ms: safeNum(uv.value),
    ...(c ? { pull_down_curve: c } : {})
  };
}

$('save-btn').onclick = () => {
  const name = $('cfg-name').value.trim();
  if (!name) { showWarn('กรอกชื่อก่อน'); return; }
  fetch('/configs', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(buildPayload(name)) })
    .then(r => r.json()).then(d => {
      if (d.detail) showWarn(d.detail);
      else { fetchConfigs(); toast('✓ Saved: ' + name); }
    }).catch(() => showWarn('บันทึกไม่สำเร็จ'));
};

$('overwrite-btn').onclick = () => {
  const key  = $('cfgdd').value;
  const name = $('cfg-name').value.trim() || key;
  if (!key) { showWarn('เลือก config ที่ต้องการ overwrite ก่อน'); return; }
  if (!confirm('Overwrite "' + name + '" ด้วยค่าปัจจุบัน?')) return;
  fetch('/configs', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(buildPayload(name)) })
    .then(r => r.json()).then(d => {
      if (d.detail) showWarn(d.detail);
      else {
        if (key !== name)
          fetch('/configs/' + encodeURIComponent(key), { method:'DELETE' }).finally(fetchConfigs);
        else
          fetchConfigs();
        toast('✓ Overwritten: ' + name);
      }
    }).catch(() => showWarn('Overwrite ไม่สำเร็จ'));
};

$('delete-btn').onclick = () => {
  const key = $('cfgdd').value;
  if (!key) { showWarn('เลือก config ที่ต้องการลบก่อน'); return; }
  if (!confirm('ลบ "' + key + '"?')) return;
  fetch('/configs/' + encodeURIComponent(key), { method:'DELETE' })
    .then(() => { fetchConfigs(); toast('Deleted: ' + key, 'var(--rd)'); }).catch(() => {});
};

// ── Profile management ────────────────────────────────────────────────────────
function fetchCfgFiles() {
  fetch('/config-files').then(r => r.json()).then(d => {
    const dd = $('cfgfd'); dd.innerHTML = '';
    d.files.forEach(f => {
      const o = document.createElement('option');
      o.value = f; o.textContent = f.replace('.json','');
      dd.appendChild(o);
    });
    dd.value = d.current;
    $('cfg-badge').textContent = d.current.replace('.json','');
  }).catch(() => {});
}

$('cfgfd').onchange = () => {
  fetch('/config-files/switch', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ filename: $('cfgfd').value })
  }).then(r => r.json()).then(d => {
    $('cfg-badge').textContent = d.current_config_file.replace('.json','');
    cache = d.guns; allKeys = Object.keys(d.guns);
    buildTagFilters(); filterBrowse();
    toast('Profile: ' + d.current_config_file.replace('.json',''));
  }).catch(() => {});
};

$('create-cfg').onclick = () => {
  const n = $('new-cfg-name').value.trim(); if (!n) return;
  fetch('/config-files', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ filename: n })
  }).then(r => r.json()).then(() => { fetchCfgFiles(); $('new-cfg-name').value = ''; toast('✓ Profile created: ' + n); })
  .catch(() => {});
};

$('delete-cfg').onclick = () => {
  const f = $('cfgfd').value;
  if (!f || f === 'default.json') return;
  if (confirm('ลบ profile "' + f + '"?'))
    fetch('/config-files/' + encodeURIComponent(f), { method:'DELETE' })
      .then(() => { fetchCfgFiles(); toast('Profile deleted', 'var(--rd)'); }).catch(() => {});
};

// Init
fetchConfigs(); fetchCfgFiles(); updSavePreview();
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTML


# ─────────────────────────────────────────────────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = get_local_ip()
    print(f"\n  ┌─ RVN v4.1 ─────────────────────────────────────────┐")
    print(f"  │  Local  : http://localhost:8000                    │")
    print(f"  │  Network: http://{ip}:8000        │")
    print(f"  └────────────────────────────────────────────────────┘\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
