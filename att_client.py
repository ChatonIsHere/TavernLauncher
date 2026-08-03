"""
The Modding Tavern — Client Launcher
"""

# Bump this with every release you publish to
# github.com/ModdingTavern/TavernLauncher/releases (tag it vX.Y.Z to match).
APP_VERSION = "1.8.2"

# The subfolder this app occupies inside the release zip
# (TavernLauncher-vX.Y.Z.zip contains /Client and /Server side by side) —
# used by the self-updater to know which part of the zip is "ours".
UPDATE_APP_FOLDER = "Client"

import sys, os, subprocess, time, json, socket, secrets, csv, threading, io, hashlib, glob, webbrowser, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import base64, hmac as _hmac, tempfile, urllib.request, urllib.error, ctypes, zipfile, shutil, struct, contextlib
import http.client
from urllib.parse import urlparse, urlencode

_updater = None
try:
    import updater as _updater
except ImportError:
    pass

import modmanager as _modmanager

# ══════════════════════════════════════════════════════════════════════════════
#  DARK TITLE BAR  (Windows 10/11 only — safe no-op elsewhere)
# ══════════════════════════════════════════════════════════════════════════════

def _enable_dark_titlebar(window):
    """Tint a Tk window's OS title bar dark so it matches the app's palette.
    Windows 10 (1809+) / 11 only. Silently does nothing anywhere else.

    Setting DWMWA_USE_IMMERSIVE_DARK_MODE only takes visual effect the next
    time DWM fully recomposes the window's caption. A SetWindowPos(...,
    SWP_FRAMECHANGED) call isn't reliably enough to trigger that full
    recompose (icon, title text, AND the min/max/close buttons) on a window's
    very first paint — but a real hide/show cycle is, which is exactly why
    clicking into the window and back out "fixes" it: that round-trip forces
    Windows to fully repaint the non-client area from scratch.

    So instead of trying to nudge DWM with a frame-changed message, we just
    do that hide/show ourselves, using raw Win32 ShowWindow calls on the
    native handle (not Tk's withdraw/deiconify) so we don't disturb Tk's own
    idea of the window's state, focus, or grab. SW_HIDE + SW_SHOWNA is
    imperceptibly quick and SW_SHOWNA specifically does not steal focus or
    reorder the window, so it's safe to run on dialogs too.

    We also run this once immediately (harmless if the window isn't mapped
    yet) and again shortly after via `after()`, since the root Tk window
    isn't actually mapped onto the screen until mainloop() starts pumping
    events — which happens after __init__ (and this call) returns.
    """
    if sys.platform != "win32":
        return

    def _apply(force_repaint):
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1)
            # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+/Win11)
            ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            if ok != 0:
                # 19 = older Win10 1809/1903 builds that used the pre-release attribute id
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
            if force_repaint:
                SW_HIDE, SW_SHOWNA = 0, 8
                ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
                ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNA)
        except Exception:
            pass

    _apply(force_repaint=False)
    try:
        window.after(60, lambda: _apply(force_repaint=True))
    except Exception:
        pass



# ══════════════════════════════════════════════════════════════════════════════
#  PALETTE
# ══════════════════════════════════════════════════════════════════════════════
BG       = "#1a1210"
SURF     = "#241c17"
SURF2    = "#2e2218"
BORDER   = "#4a3828"
AMBER    = "#e8a840"
AMBERDIM = "#8a5e1a"
PARCH    = "#f0e6cc"
MUTED    = "#8a7a62"
GREEN    = "#6aaa72"
RED      = "#c45c5c"
CYAN     = "#6ab0aa"
MONO     = ("Consolas", 9)

AUTH_PORT   = 1762
CONSOLE_PORT    = 1758  # kept for reference — no longer used directly
WS_CONSOLE_PORT = 1760

class WsConsoleClient:
    """WebSocket client for the game console on port 1760."""

    def __init__(self):
        self._ws        = None
        self._lock      = threading.Lock()
        self._connected = False
        self._cmd_id    = 0
        self._pending   = {}
        self._stop      = threading.Event()
        self._on_line   = None
        self._on_disc   = None

    def connect(self, host, token, on_line=None, on_disc=None, timeout=6):
        import websocket as _wslib
        self._on_line = on_line
        self._on_disc = on_disc
        self._stop.clear()
        try:
            ws = _wslib.WebSocket()
            ws.settimeout(timeout)
            ws.connect(f"ws://{host}:{WS_CONSOLE_PORT}")
            ws.send(token)
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "SystemMessage":
                data = str(msg.get("data", ""))
                if "Connection Succeeded" in data:
                    ws.settimeout(None)
                    self._ws = ws
                    self._connected = True
                    threading.Thread(target=self._recv_loop, daemon=True).start()
                    return True, data
                ws.close()
                return False, data
            ws.close()
            return False, f"Unexpected auth response: {msg}"
        except Exception as e:
            return False, str(e)

    def disconnect(self):
        self._stop.set()
        self._connected = False
        ws, self._ws = self._ws, None
        if ws:
            try: ws.close()
            except: pass
        with self._lock:
            for ev, holder in self._pending.values():
                holder["error"] = "Disconnected"
                ev.set()
            self._pending.clear()

    def send(self, cmd):
        if not self._connected or not self._ws:
            return
        with self._lock:
            self._cmd_id += 1
            cid = self._cmd_id
        try:
            self._ws.send(json.dumps({"id": cid, "content": cmd}))
        except Exception:
            pass

    def send_capture(self, cmd, timeout=20.0):
        if not self._connected or not self._ws:
            return "", None, "Not connected"
        with self._lock:
            self._cmd_id += 1
            cid    = self._cmd_id
            ev     = threading.Event()
            holder = {"result_string": "", "result_data": None, "error": None}
            self._pending[cid] = (ev, holder)
        try:
            self._ws.send(json.dumps({"id": cid, "content": cmd}))
        except Exception as e:
            with self._lock:
                self._pending.pop(cid, None)
            return "", None, str(e)
        ev.wait(timeout)
        with self._lock:
            self._pending.pop(cid, None)
        return holder["result_string"], holder["result_data"], holder["error"]

    def _recv_loop(self):
        ws = self._ws
        while not self._stop.is_set():
            try:
                raw = ws.recv()
                if not raw:
                    break
                self._handle(raw)
            except Exception as e:
                if not self._stop.is_set():
                    self._connected = False
                    reason = str(e) or "Connection lost"
                    if self._on_disc:
                        self._on_disc(reason)
                    with self._lock:
                        for ev, holder in self._pending.values():
                            holder["error"] = reason
                            ev.set()
                        self._pending.clear()
                break

    def _handle(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            if self._on_line:
                self._on_line(raw + "\n")
            return
        msg_type = msg.get("type", "")
        data     = msg.get("data")
        cmd_id   = msg.get("commandId")

        if msg_type == "CommandResult":
            rs = ""
            rd = None
            if isinstance(data, dict):
                rs = str(data.get("ResultString") or "")
                rd = data.get("Result")
            elif data is not None:
                rs = str(data)
            if self._on_line:
                if rs and not rs.startswith("System."):
                    self._on_line(rs if rs.endswith("\n") else rs + "\n")
                elif rd is not None and rd != [] and rd != "":
                    # ResultString was a useless type name; show structured data
                    import json as _json
                    try:
                        display = _json.dumps(rd, indent=2)
                    except Exception:
                        display = str(rd)
                    self._on_line(display + "\n")
            if cmd_id is not None:
                with self._lock:
                    entry = self._pending.get(cmd_id)
                if entry:
                    entry[1]["result_string"] = rs
                    entry[1]["result_data"]   = rd
                    entry[0].set()

        elif msg_type == "SystemMessage":
            text = str(data) if data else ""
            if text and self._on_line:
                self._on_line(f"[{text}]\n")
        else:
            if self._on_line:
                self._on_line(raw + "\n")

# The dropdown shows friendly platform names, but the game itself still
# needs the original /vrmode values it always expected — this is a purely
# visual simplification, not a protocol change. PLATFORM_LEGACY_TO_DISPLAY
# handles a config file saved before this change (which would have the old
# "OpenVR"/"Oculus" display string persisted) so existing users' saved
# choice still loads correctly instead of silently resetting.
PLATFORM_DISPLAY_TO_BACKEND = {"SteamVR": "openvr", "Quest": "oculus"}
PLATFORM_LEGACY_TO_DISPLAY  = {"OpenVR": "SteamVR", "Oculus": "Quest"}
USERNAME_MAX_LEN = 16
USERNAME_EXTRA_CHARS = " -_"

def _is_valid_username(username):
    """ASCII letters/digits plus space, hyphen, underscore — keeps usernames
    safe to embed in file names (token cache) and launch args without escaping."""
    return all((c.isalnum() and c.isascii()) or c in USERNAME_EXTRA_CHARS
               for c in username)

GAME_LOG_PATH = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming",
    "A Township Tale", "Client", "logs", "unity-log.csv"
)

# Community server list backend — a small Flask app the server owner runs
# at home (see community_server.py). Plain HTTP on the port they forwarded;
# it's just public server metadata, nothing sensitive.
COMMUNITY_API = "http://themoddingtavern.com:1763/servers"
DISCORD_URL   = "https://discord.gg/jNQUUDAYSj"

# ══════════════════════════════════════════════════════════════════════════════
#  ICON
# ══════════════════════════════════════════════════════════════════════════════
_ICON_B64 = None
try:
    from icon_data import ICON_B64 as _ICON_B64
except ImportError:
    pass

def _set_window_icon(root):
    if not _ICON_B64: return
    try:
        tmp = os.path.join(tempfile.gettempdir(), "tavern_icon.ico")
        with open(tmp, "wb") as f: f.write(base64.b64decode(_ICON_B64))
        root.iconbitmap(tmp)
    except Exception: pass

# ══════════════════════════════════════════════════════════════════════════════
#  HEADER BANNER  (background image behind the title bar, live-resized)
# ══════════════════════════════════════════════════════════════════════════════
# Needs Pillow — tkinter's own PhotoImage can't smoothly rescale on the fly,
# only a plain sample-based zoom/subsample. If Pillow or the embedded asset
# isn't available for any reason, the header just falls back to its old flat
# background color; this never blocks the app from running.
_HEADER_BANNER_IMG = None
try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk, ImageEnhance as _PILImageEnhance
    from banner_data import BANNER_B64 as _BANNER_B64
    _HEADER_BANNER_IMG = _PILImage.open(io.BytesIO(base64.b64decode(_BANNER_B64))).convert("RGB")
except Exception:
    _HEADER_BANNER_IMG = None

def _header_crop_box(src_w, src_h, target_w, target_h,
                      min_reveal=0.35, min_width=540, reveal_at_width=1400):
    """A centered crop box (source-image pixel coordinates) matching the
    target aspect ratio exactly, so scaling it up to (target_w, target_h)
    afterward never distorts anything — unlike stretching the whole image
    to an arbitrary width, which is what made it look "stretched super far"
    on a maximized window. At the smallest window width this shows a
    modestly zoomed-in slice near the center of the artwork; widening the
    window smoothly reveals more of it (rather than stretching the same
    content further) up to showing the whole image by reveal_at_width, and
    simply staying fully revealed (scaled larger) beyond that."""
    target_w = max(int(target_w), 1)
    target_h = max(int(target_h), 1)
    span = max(1, reveal_at_width - min_width)
    reveal = min_reveal + (1.0 - min_reveal) * min(1.0, max(0.0, (target_w - min_width) / span))
    crop_w = src_w * reveal
    crop_h = crop_w * target_h / target_w
    if crop_h > src_h:
        crop_h = src_h
        crop_w = crop_h * target_w / target_h
    crop_w = min(crop_w, src_w)
    cx, cy = src_w / 2.0, src_h / 2.0
    left   = max(0, int(round(cx - crop_w / 2.0)))
    top    = max(0, int(round(cy - crop_h / 2.0)))
    right  = min(src_w, int(round(cx + crop_w / 2.0)))
    bottom = min(src_h, int(round(cy + crop_h / 2.0)))
    return (left, top, right, bottom)

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════

def _app_dir():
    if getattr(sys, "frozen", False): return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _tavern_data_dir():
    """The one shared place this launcher's own persistent data lives —
    config and token files — regardless of which folder the exe itself
    happens to be running from. Means downloading a new build to a
    different folder, or a fresh install replacing the old one, never
    requires manually moving files over; they were never next to the exe
    in the first place. (The Patch/ folder and per-game-install files like
    .tavern_mods_meta.json deliberately stay where they are — see the
    comments at their own definitions for why.)"""
    base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    path = os.path.join(base, "TheModdingTavern")
    try: os.makedirs(path, exist_ok=True)
    except Exception: pass
    return path


def _last_rejection_path():
    """Same path TavernLib's TavernDirectories.LastRejection writes to:
    %AppData%/TheModdingTavern/last_rejection.json."""
    return os.path.join(_tavern_data_dir(), "last_rejection.json")


def _delete_last_rejection_file():
    """Called right before every launch so a file present after THIS
    process exits was written by THIS attempt, not a leftover from an
    earlier one."""
    try: os.remove(_last_rejection_path())
    except OSError: pass

def _migrate_legacy_file(old_path, new_path):
    """One-time move from before file storage was unified into
    _tavern_data_dir(). Safe to call every startup — a no-op once the file
    has already been moved, or if it never existed at the old location."""
    try:
        if os.path.isfile(old_path) and not os.path.isfile(new_path):
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.move(old_path, new_path)
    except Exception:
        pass

def _migrate_legacy_tokens():
    """One-time bulk move of every pre-existing token file (old single-
    file-per-username scheme and the newer per-server-pair scheme alike)
    from next to the exe into the shared tokens/ folder."""
    try:
        old_dir = _app_dir()
        new_dir = os.path.join(_tavern_data_dir(), "tokens")
        for old_path in glob.glob(os.path.join(old_dir, ".token_*.json")):
            new_path = os.path.join(new_dir, os.path.basename(old_path))
            _migrate_legacy_file(old_path, new_path)
    except Exception:
        pass

CONFIG_FILE = os.path.join(_tavern_data_dir(), "tavern_launcher.json")
_migrate_legacy_file(os.path.join(os.path.expanduser("~"), ".tavern_launcher.json"), CONFIG_FILE)
_migrate_legacy_tokens()

def _safe_part(s):
    return "".join(c for c in str(s).lower() if c.isalnum() or c in "-_") or "x"

def _legacy_token_file(username):
    """Old scheme: one token file per username, shared across every server."""
    return os.path.join(_tavern_data_dir(), "tokens", f".token_{_safe_part(username)}.json")

def _token_file(host, username):
    """New scheme: one token file per server+username pair, so the same
    username can hold a different, independent token on each server."""
    return os.path.join(_tavern_data_dir(), "tokens",
        f".token_{_safe_part(host)}__{_safe_part(username)}.json")

def _any_token_files_exist():
    """True if at least one token file (old or new naming scheme) already
    exists, regardless of which server/username it's for."""
    try:
        return bool(glob.glob(os.path.join(_tavern_data_dir(), "tokens", ".token_*.json")))
    except Exception:
        return False

def _get_or_create_token(host, username):
    """Returns (token, is_new). is_new is True only the first time this
    server+username pair gets a token file created on this machine."""
    path = _token_file(host, username)
    try:
        d = json.load(open(path))
        if d.get("username","").lower() == username.lower() and d.get("token"):
            return d["token"], False
    except: pass

    # One-time migration: if this username already had a token under the old
    # shared-across-all-servers scheme, reuse it so existing accounts on
    # servers the player already joined don't suddenly stop matching.
    token = None
    try:
        d = json.load(open(_legacy_token_file(username)))
        if d.get("username","").lower() == username.lower() and d.get("token"):
            token = d["token"]
    except: pass

    is_new = token is None
    if token is None:
        token = secrets.token_urlsafe(18)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump({"username": username, "host": host, "token": token}, open(path,"w"))
    except: pass
    return token, is_new

def authenticate(host, username, token, password=None, timeout=8):
    payload = {"username": username, "token": token}
    if password is not None:
        payload["password"] = hashlib.sha256(password.encode()).hexdigest()
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, AUTH_PORT))
        s.sendall(json.dumps(payload).encode())
        raw = s.recv(4096)
        s.close()
        resp = json.loads(raw.decode())
    except Exception as e:
        # Distinguishable from a real rejection — this means there was no
        # auth service to even talk to, not that one rejected the login.
        # _do_launch uses this specifically to decide whether a headless-
        # server fallback lookup is worth trying.
        return None, f"CANNOT_REACH::{e}"
    status = resp.get("status")
    if status == "ok":          return resp.get("user_id"), None
    if status == "needs_password": return None, "NEEDS_PASSWORD"
    if status == "wrong_password": return None, "Wrong password."
    if status == "not_whitelisted": return None, "You are not on the whitelist for this server."
    return None, resp.get("message", "Authentication failed.")

def ticket_request(host, action, username, token, timeout=10, **kwargs):
    """Sends one ticket_action request to a server's auth port and returns
    the parsed JSON response. Raises on a connection failure — callers
    should catch and show a clear error, same as any other network call
    here. Uses a larger receive buffer than the plain auth exchange, since
    a ticket list with several tickets and comment threads can genuinely
    exceed the smaller buffer used for a simple login response."""
    payload = {"ticket_action": action, "username": username, "token": token}
    payload.update(kwargs)
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((host, AUTH_PORT))
    s.sendall(json.dumps(payload).encode())
    raw = s.recv(65536)
    s.close()
    return json.loads(raw.decode())

def ping_server(host, timeout=5):
    """Returns (info_dict, latency_ms) or raises. info_dict carries mods_hash/
    mods_count alongside the existing fields when the server supports it. An
    older server without them just omits those keys, so callers must use
    .get(), not [] indexing, for either."""
    t0 = time.time()
    s  = socket.socket()
    s.settimeout(timeout)
    s.connect((host, AUTH_PORT))
    s.sendall(json.dumps({"ping": True}).encode())
    raw = s.recv(4096)
    ms  = int((time.time() - t0) * 1000)
    s.close()
    return json.loads(raw.decode()), ms


# Hard ceiling on a framed response body.
_MAX_FRAMED_BYTES = 8 * 1024 * 1024


def _recv_framed(s, timeout):
    """Reads one length-prefixed message: a 4-byte big-endian byte count, then
    exactly that many bytes, looping recv() since a single call can return a
    partial read no matter the buffer size. Raises on a short/closed stream, or
    on a declared length past _MAX_FRAMED_BYTES (rejected from the header alone,
    before a single body byte is read or buffered)."""
    s.settimeout(timeout)
    header = b""
    while len(header) < 4:
        chunk = s.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("Connection closed while reading length header.")
        header += chunk
    length = int.from_bytes(header, "big")
    if length > _MAX_FRAMED_BYTES:
        raise ConnectionError(
            f"Server declared a {length}-byte response, over the "
            f"{_MAX_FRAMED_BYTES}-byte limit; refusing to read it.")
    body = bytearray()
    while len(body) < length:
        chunk = s.recv(min(65536, length - len(body)))
        if not chunk:
            raise ConnectionError("Connection closed while reading message body.")
        body += chunk
    return bytes(body)


def fetch_server_mods(host, timeout=10):
    """Fetches the server's full installed-mods list: every currently-enabled
    mod as {"id","version","client_side","server_side"}.
    Only called on a mods_hash cache miss (or right before a join); the
    ordinary ping/pong stays a single small recv, this is the one request that
    needs proper length-prefixed framing since a large mod list can genuinely
    exceed one recv's buffer. Raises on any connection/parse failure or a
    server that doesn't understand the request (older TavernLib/launcher)."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, AUTH_PORT))
        s.sendall(json.dumps({"mods_list": True}).encode())
        body = _recv_framed(s, timeout)
    finally:
        # Closed on every path: this runs against an unreachable or misbehaving
        # host often enough that leaking the socket on the error path matters.
        try: s.close()
        except OSError: pass
    resp = json.loads(body.decode())
    if resp.get("status") != "ok":
        raise Exception(resp.get("message", "Server rejected the mods list request."))
    return resp.get("mods", [])

# ══════════════════════════════════════════════════════════════════════════════
#  JWT
# ══════════════════════════════════════════════════════════════════════════════

def _b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _jwt(payload):
    h = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    b = _b64url(json.dumps(payload, separators=(",",":")).encode())
    s = _b64url(_hmac.new(b"offline", f"{h}.{b}".encode(), hashlib.sha256).digest())
    return f"{h}.{b}.{s}"

def _resolve_ip_for_game(host):
    """The game's own /dev_server_ip argument appears to require a literal
    IP address, not a hostname — passing a DNS name through connects fine
    at the auth-handshake level (that's plain Python socket code, which
    resolves hostnames automatically) but then silently fails to actually
    join: black screen, server sees no incoming connection. This resolves
    the hostname once, specifically for that one argument, so the game
    always receives a real IP regardless of whether the player joined via
    a hostname from the community list or typed one in directly."""
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        return host


def _valid_port(value, default=1757):
    """Coerces whatever's in the port field to a sane integer, falling back
    to the game's own default if it's empty, non-numeric, or out of range."""
    try:
        p = int(str(value).strip())
        return p if 1 <= p <= 65535 else default
    except (TypeError, ValueError):
        return default


def build_tokens(user_id, username, tavern_token="", mods_claim=""):
    """tavern_token is our OWN internal secret (the same one _get_or_create_token
    already tracks per server+username) — embedded here as an extra custom
    claim purely for a server-side mod to verify independently. UserId and
    Username alone aren't enough for that: since the "offline" HMAC key is
    necessarily public (the game itself has to know it to run in
    /force_offline mode at all), anyone can hand-craft a validly-signed JWT
    claiming any UserId/Username they like. This extra claim is the one
    thing in here a forger can't guess — a cryptographically random value
    that's only ever handed out after actually passing the auth handshake
    (password, whitelist, blacklist all included), so a mod checking it
    against the server's own records closes that gap regardless of what
    else the presented JWT claims to be.

    mods_claim is a JSON object string of {mod_id: version} for every
    community mod currently enabled on this machine, the client's own
    side of TavernLib's exact-version mod-parity check (PlayerJoinFilter /
    ModParity.ValidateClient), read off the same "TavernMods" claim. Empty
    string when there's nothing to report (no mods enabled, or the caller
    didn't compute one); a server with no client_side-required mods ignores
    it either way.

    Both TavernToken and TavernMods go on the identity token as well as the
    access token, because the identity one is what the game actually sends as
    RequestJoinMessage.UserCredentials - the only token a server ever reads
    these off. On the access token alone the claim never arrives, the server
    sees a client with no mods, and every mod it requires of clients comes back
    as a mismatch no matter what's installed."""
    exp, uid = 9999999999, str(user_id)
    a = _jwt({"UserId":uid,"Username":username,"role":"Access","is_verified":"True",
              "is_member":"True","Policy":["offline","play_offline","server_access_pre_alpha",
              "server_access_tutorial","game_access_public","game_access_development",
              "server_access_development","server_access_testing","game_access_testing",
              "server_owner","debug_features","admin_vr_modes","database_admin",
              "server_create_development","reuse_refresh_tokens"],
              "TavernToken":tavern_token,"TavernMods":mods_claim,
              "exp":exp,"iss":"AltaWebAPI","aud":"AltaClient"})
    r = _jwt({"UserId":uid,"role":"Refresh","exp":exp,"iss":"AltaWebAPI","aud":"AltaClient"})
    i = _jwt({"UserId":uid,"Username":username,"role":"Identity","is_member":"True",
              "is_dev":"True","TavernToken":tavern_token,"TavernMods":mods_claim,
              "exp":exp,"iss":"AltaWebAPI","aud":"AltaClient"})
    return a, r, i

# Headless/direct-connect servers have no port-1762 gate to hand out a
# user_id, so one is derived locally instead — stable per-username (so
# reconnecting as the same name keeps the same id).
#
# Range choice matters here: the game parses UserId back out as a signed
# Int32 (max 2,147,483,647). att_server.py's official ids start at
# BASE_USER_ID = 2,000,000,000 and only grow by 1 per player, so they stay
# safely under that limit — but it leaves very little headroom above it to
# put a second, non-colliding range without also blowing past Int32's max.
# So instead this range sits entirely *below* the official one: comfortably
# inside Int32, and never reachable by the official counter in practice.
HEADLESS_USER_ID_BASE  = 1_000_000_000
HEADLESS_USER_ID_RANGE = 999_999_999

def _headless_user_id(username):
    h = int(hashlib.sha256(username.strip().lower().encode()).hexdigest(), 16)
    return HEADLESS_USER_ID_BASE + (h % HEADLESS_USER_ID_RANGE)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def load_cfg():
    try: return json.load(open(CONFIG_FILE))
    except: return {}

def save_cfg(d):
    try: json.dump(d, open(CONFIG_FILE,"w"), indent=2)
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
#  LOG TAILER
# ══════════════════════════════════════════════════════════════════════════════

class GameLogTailer:
    INITIAL_TAIL_LINES = 50
    INITIAL_TAIL_BYTES = 200_000  # generous window to guarantee >= 50 lines of CSV

    def __init__(self, path, on_line, on_status=None):
        self.path, self.on_line, self.on_status = path, on_line, on_status
        self._stop = threading.Event()
    def start(self): threading.Thread(target=self._run, daemon=True).start()
    def stop(self):  self._stop.set()
    def _run(self):
        last, f, buf = -1, None, ""
        while not self._stop.is_set():
            try:
                if not os.path.exists(self.path): time.sleep(1); continue
                sz = os.path.getsize(self.path)
                if f is None or sz < last:
                    if f:
                        try: f.close()
                        except: pass
                    # Show only the tail of existing history instead of reading
                    # the whole file — on a big log that read could take a while.
                    self._emit_initial_tail(sz)
                    f = open(self.path,"r",encoding="utf-8-sig",errors="replace",newline="")
                    f.seek(0, os.SEEK_END)  # we've already shown the history above
                    buf = ""
                    if self.on_status: self.on_status("watching")
                if sz > last:
                    chunk = f.read()
                    if chunk:
                        buf += chunk
                        rows, buf = self._split(buf)
                        if rows: self._emit(rows)
                last = sz
            except: pass
            time.sleep(0.4)
        if f:
            try: f.close()
            except: pass
    def _emit_initial_tail(self, sz):
        """Read just the last chunk of the file (in binary, so an arbitrary
        byte offset is always safe to seek to) and emit only its last
        INITIAL_TAIL_LINES complete rows."""
        try:
            read_from = max(0, sz - self.INITIAL_TAIL_BYTES)
            with open(self.path, "rb") as bf:
                bf.seek(read_from)
                raw = bf.read()
            text = raw.decode("utf-8-sig", errors="replace")
            if read_from > 0:
                # We likely started mid-line — drop the truncated first line.
                nl = text.find("\n")
                text = text[nl+1:] if nl != -1 else ""
            rows, _ = self._split(text)
            tail_rows = rows[-self.INITIAL_TAIL_LINES:]
            if tail_rows: self._emit(tail_rows)
        except Exception:
            pass
    @staticmethod
    def _split(buf):
        recs, i, n, s, q = [], 0, len(buf), 0, False
        while i < n:
            c = buf[i]
            if c == '"': q = not q
            elif c == '\n' and not q: recs.append(buf[s:i+1]); s = i+1
            i += 1
        return recs, buf[s:]
    def _emit(self, rows):
        try:
            for row in csv.reader(io.StringIO("".join(rows))):
                if len(row) >= 4: t,lv,lg,msg = row[0],row[1],row[2],row[3]
                elif len(row)==3: t,lv,lg,msg = row[0],row[1],"",row[2]
                else: continue
                ts = t[11:19] if len(t)>=19 else t
                self.on_line(ts,lv,lg,msg.split("\n",1)[0])
        except: pass

# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

def _divider(parent):
    f = tk.Frame(parent, bg=BG)
    f.pack(fill="x", padx=20, pady=5)
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, pady=4)
    tk.Label(f, text=" ✦ ", bg=BG, fg=AMBERDIM, font=("Georgia",9)).pack(side="left")
    tk.Frame(f, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, pady=4)

def _section_label(parent, text):
    tk.Label(parent, text=text, bg=BG, fg=MUTED,
             font=("Georgia",8,"bold")).pack(anchor="w", padx=22, pady=(7,3))

def _field(parent):
    f = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    f.pack(fill="x", padx=20, pady=(0,3))
    return f

def _hint(parent, text):
    tk.Label(parent, text=text, bg=BG, fg=MUTED, justify="left",
             font=("Segoe UI",8)).pack(anchor="w", padx=22, pady=(0,2))

def _btn(parent, text, cmd, style="normal", **kw):
    colors = {
        "normal":  (SURF2, PARCH, AMBERDIM, AMBER),
        "primary": ("#3d2a0a", AMBER, "#5a3d0e", "#ffd080"),
        "danger":  ("#3d1010","#e88080","#5a1818","#ffaaaa"),
        "success": ("#1a3d1e","#a8d8a0","#2a5e2e","#c8f0c0"),
        "dim":     (SURF,     MUTED,  SURF2,   PARCH),
    }[style]
    return tk.Button(parent, text=text, bg=colors[0], fg=colors[1],
                     activebackground=colors[2], activeforeground=colors[3],
                     disabledforeground=MUTED,
                     relief="flat", bd=0, cursor="hand2", command=cmd, **kw)

def _mk_combobox(parent, var, values):
    style = ttk.Style()
    style.configure("Tav.TCombobox",
                    fieldbackground=SURF, background=SURF2,
                    foreground=PARCH, selectbackground=SURF,
                    selectforeground=PARCH, arrowcolor=AMBERDIM, borderwidth=0)
    style.map("Tav.TCombobox",
              fieldbackground=[("readonly",SURF)],
              foreground=[("readonly",PARCH)],
              selectbackground=[("readonly",SURF)],
              selectforeground=[("readonly",PARCH)])
    parent.option_add("*TCombobox*Listbox.background",       SURF)
    parent.option_add("*TCombobox*Listbox.foreground",       PARCH)
    parent.option_add("*TCombobox*Listbox.selectBackground", AMBERDIM)
    parent.option_add("*TCombobox*Listbox.selectForeground", "#ffd080")
    cb = ttk.Combobox(parent, textvariable=var, values=values,
                      state="readonly", font=("Consolas",10), style="Tav.TCombobox")
    cb.pack(fill="x", ipady=4, padx=6, pady=6)
    return cb

def _mk_scrollbar(parent, command, orient="vertical"):
    """A ttk scrollbar styled to match the dark theme.
    Plain tk.Scrollbar renders using native Windows visual styles and ignores
    bg/troughcolor there, which is why scrollbars stayed white — ttk under the
    'clam' theme draws its own elements instead, so our colors actually apply."""
    style = ttk.Style()
    name = "Tav.Vertical.TScrollbar" if orient == "vertical" else "Tav.Horizontal.TScrollbar"
    style.configure(name, background=SURF2, troughcolor=BG, bordercolor=BORDER,
                    arrowcolor=AMBERDIM, darkcolor=SURF2, lightcolor=SURF2, relief="flat")
    style.map(name, background=[("active", AMBERDIM), ("pressed", AMBERDIM)],
              arrowcolor=[("pressed", "#ffd080")])
    sb = ttk.Scrollbar(parent, orient=orient, command=command, style=name)
    return sb

def _mk_tree(parent, cols, widths, height=8, hscroll=False):
    style = ttk.Style()
    style.configure("Tav.Treeview", background=SURF, fieldbackground=SURF,
                    foreground=PARCH, rowheight=26, borderwidth=0)
    style.configure("Tav.Treeview.Heading", background=SURF2, foreground=AMBER,
                    font=("Georgia",9,"bold"))
    style.map("Tav.Treeview",
              background=[("selected",AMBERDIM)],
              foreground=[("selected","#ffd080")])
    f = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
    f.pack(fill="both", expand=True)

    hsb = None
    if hscroll:
        # Pack the horizontal scrollbar at the bottom first so it claims its
        # space before the tree body below fills the rest — reversing this
        # order would let the tree body crowd the scrollbar out entirely.
        hsb = _mk_scrollbar(f, None, "horizontal")
        hsb.pack(side="bottom", fill="x")

    body = tk.Frame(f, bg=SURF)
    body.pack(fill="both", expand=True, padx=2, pady=2)

    tree = ttk.Treeview(body, columns=cols, show="headings",
                        selectmode="browse", height=height, style="Tav.Treeview")
    for col, w in zip(cols, widths):
        tree.heading(col, text=col.replace("_"," ").title())
        # With a horizontal scrollbar, columns should keep their exact width
        # and overflow into scroll range rather than being squeezed to fit —
        # that squeezing is exactly what made columns unreadable before.
        tree.column(col, width=w, minwidth=w, stretch=not hscroll, anchor="w")

    vsb = _mk_scrollbar(body, tree.yview, "vertical")
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    tree.config(yscrollcommand=vsb.set)
    if hsb is not None:
        hsb.config(command=tree.xview)
        tree.config(xscrollcommand=hsb.set)
    return tree

# ══════════════════════════════════════════════════════════════════════════════
#  COMMUNITY BROWSER
# ══════════════════════════════════════════════════════════════════════════════

class CommunityBrowser(tk.Toplevel):
    _COLUMNS = ("name","address","players","locked","type","version")
    _HEADINGS = {"name":"Name","address":"Address","players":"Players",
                 "locked":"","type":"Type","version":"Version"}
    _SORT_KEYS = {
        "name":    lambda s: s.get("name","").lower(),
        "address": lambda s: s.get("address","").lower(),
        "players": lambda s: s.get("player_count",0),
        "locked":  lambda s: bool(s.get("has_password")),
        "type":    lambda s: s.get("kind","official"),
        "version": lambda s: s.get("version","unknown").lower(),
    }

    def __init__(self, parent, on_select):
        super().__init__(parent)
        self.title("Community Servers")
        self.configure(bg=BG)
        self.geometry("780x460")
        self.resizable(False, False)
        self._on_select = on_select
        self._servers   = []   # full list, straight from the API
        self._visible    = []  # filtered + sorted subset actually shown
        self._sort_col   = None
        self._sort_reverse = False
        self._build()
        self._refresh()
        _enable_dark_titlebar(self)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🌍  Community Servers", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        sf = tk.Frame(self, bg=BG)
        sf.pack(fill="x", padx=20, pady=(10,4))
        tk.Label(sf, text="🔍", bg=BG, fg=MUTED, font=("Segoe UI",10)).pack(side="left")
        self.v_search = tk.StringVar(value="")
        self.v_search.trace_add("write", lambda *_: self._populate())
        tk.Entry(sf, textvariable=self.v_search, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True, padx=(6,0))

        self._status = tk.StringVar(value="Fetching server list…")
        tk.Label(self, textvariable=self._status, bg=BG, fg=MUTED,
                 font=("Segoe UI",9)).pack(anchor="w", padx=20, pady=(4,4))

        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=20, pady=(0,8))
        self.tree = _mk_tree(lf, self._COLUMNS,
                             [190,130,65,30,85,95], height=8, hscroll=True)
        for col in self._COLUMNS:
            self.tree.heading(col, text=self._HEADINGS[col],
                              command=lambda c=col: self._sort_by(c))

        br = tk.Frame(self, bg=BG)
        br.pack(fill="x", padx=20, pady=(0,12))
        _btn(br, "⟳ Refresh", self._refresh, font=("Segoe UI",9),
             pady=6, padx=12).pack(side="left")
        _btn(br, "★ Save as Favorite", self._save_favorite,
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=6)
        _btn(br, "Connect",   self._connect, "primary",
             font=("Georgia",10,"bold"), pady=6, padx=14).pack(side="right")

    def _refresh(self):
        self._status.set("Fetching…")
        for r in self.tree.get_children(): self.tree.delete(r)
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            req = urllib.request.Request(COMMUNITY_API,
                headers={"User-Agent":"TavernLauncher/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
            self._servers = data if isinstance(data, list) else []
            self.after(0, self._populate)
        except Exception as e:
            self.after(0, lambda: self._status.set(
                f"Could not reach community list — {e}"))

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._populate()

    def _populate(self):
        query = self.v_search.get().strip().lower()
        if query:
            visible = [s for s in self._servers if
                       query in s.get("name","").lower() or
                       query in s.get("address","").lower()]
        else:
            visible = list(self._servers)

        if self._sort_col:
            key = self._SORT_KEYS[self._sort_col]
            visible.sort(key=key, reverse=self._sort_reverse)

        self._visible = visible

        for col in self._COLUMNS:
            label = self._HEADINGS[col]
            if col == self._sort_col:
                label += " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(col, text=label)

        for r in self.tree.get_children(): self.tree.delete(r)
        for s in self._visible:
            players = f"{s.get('player_count',0)}/{s.get('player_limit',50)}"
            locked  = "🔒" if s.get("has_password") else ""
            kind    = s.get("kind", "official")
            type_label = "🏛 Official" if kind == "official" else "🌐 Headless"
            version    = s.get("version", "unknown") or "unknown"
            self.tree.insert("","end",
                values=(s.get("name","?"), s.get("address","?"), players, locked, type_label, version))

        if not self._servers:
            self._status.set("No servers listed yet.")
        elif query and not visible:
            self._status.set(f"No servers match '{query}'.")
        else:
            self._status.set(f"{len(visible)} of {len(self._servers)} servers shown."
                             if query else f"{len(self._servers)} servers listed.")

    def _selected_server(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a server first.", parent=self)
            return None
        return self._visible[self.tree.index(sel[0])]

    def _connect(self):
        srv = self._selected_server()
        if not srv: return
        kind = srv.get("kind", "official")
        address = srv.get("address","")
        host = address.split(":")[0]
        port = address.split(":")[1] if ":" in address else "1757"
        # "address" is "ip:port" for display — only the host is meaningful to
        # the auth handshake / headless join today, but the port still gets
        # passed through so Join Server can hand it to the game itself.
        self._on_select(host, srv.get("name",""), kind, port)
        self.destroy()

    def _save_favorite(self):
        srv = self._selected_server()
        if not srv: return
        address = srv.get("address","")
        host = address.split(":")[0]
        port = address.split(":")[1] if ":" in address else "1757"
        name = srv.get("name") or host
        cfg = load_cfg()
        saved = cfg.get("saved_servers", [])
        if any(s.get("ip") == host for s in saved):
            messagebox.showinfo("Already saved", f"'{name}' is already in your favorites.", parent=self)
            return
        saved.append({"name": name, "ip": host, "port": port})
        cfg["saved_servers"] = saved
        save_cfg(cfg)
        messagebox.showinfo("Saved", f"'{name}' added to favorites.", parent=self)

# ══════════════════════════════════════════════════════════════════════════════
#  SERVER LIST PANEL  (Saved / Recent)
# ══════════════════════════════════════════════════════════════════════════════

class ServerListPanel(tk.Toplevel):
    def __init__(self, parent, on_select):
        super().__init__(parent)
        self.title("Saved & Recent Servers")
        self.configure(bg=BG)
        self.geometry("520x460")
        self.resizable(False, False)
        self._on_select = on_select
        self._build()
        _enable_dark_titlebar(self)

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="⚑  Your Servers", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        nb = ttk.Notebook(self)
        style = ttk.Style()
        style.configure("TavNB.TNotebook", background=BG, borderwidth=0)
        style.configure("TavNB.TNotebook.Tab", background=SURF2, foreground=PARCH,
                        padding=(12,5), font=("Georgia",9))
        style.map("TavNB.TNotebook.Tab",
                  background=[("selected",AMBERDIM)],
                  foreground=[("selected","#ffd080")])
        nb.configure(style="TavNB.TNotebook")
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        fav_tab    = tk.Frame(nb, bg=BG)
        recent_tab = tk.Frame(nb, bg=BG)
        nb.add(fav_tab,    text="  Favourites  ")
        nb.add(recent_tab, text="  Recent  ")

        self._build_fav_tab(fav_tab)
        self._build_recent_tab(recent_tab)

    def _build_fav_tab(self, parent):
        lf = tk.Frame(parent, bg=BG)
        lf.pack(fill="both", expand=True, padx=8, pady=(8,4))
        self.fav_tree = _mk_tree(lf, ("label","ip"), [240,200], height=9)
        cfg = load_cfg()
        for s in cfg.get("saved_servers", []):
            self.fav_tree.insert("","end", values=(s.get("name",s.get("ip","")), s.get("ip","")))

        br = tk.Frame(parent, bg=BG)
        br.pack(fill="x", padx=8, pady=(0,8))

        def connect():
            sel = self.fav_tree.selection()
            if not sel: return
            vals = self.fav_tree.item(sel[0],"values")
            ip = vals[1]
            entry = next((s for s in load_cfg().get("saved_servers",[])
                          if s.get("ip") == ip), {})
            self._on_select(ip, vals[0], entry.get("port","1757")); self.destroy()

        def remove():
            sel = self.fav_tree.selection()
            if not sel: return
            vals = self.fav_tree.item(sel[0],"values")
            cfg = load_cfg()
            cfg["saved_servers"] = [s for s in cfg.get("saved_servers",[])
                                    if s.get("ip") != vals[1]]
            save_cfg(cfg); self.fav_tree.delete(sel[0])

        def add_manual():
            ip = simpledialog.askstring("Add Server",
                "Server IP:", parent=self)
            if not ip: return
            ip = ip.strip()
            label = simpledialog.askstring("Add Server",
                "Label for this server:", parent=self) or ip
            port = simpledialog.askstring("Add Server",
                "Port (leave blank for 1757):", parent=self) or "1757"
            cfg = load_cfg()
            saved = cfg.get("saved_servers", [])
            saved.append({"name": label, "ip": ip, "port": port})
            cfg["saved_servers"] = saved
            save_cfg(cfg)
            self.fav_tree.insert("","end", values=(label, ip))

        _btn(br, "Connect",   connect,    "primary", font=("Georgia",10,"bold"),
             pady=6, padx=14).pack(side="left")
        _btn(br, "+ Add IP",  add_manual, style="normal",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=6)
        _btn(br, "✕ Remove",  remove,     "danger",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left")

    def _build_recent_tab(self, parent):
        lf = tk.Frame(parent, bg=BG)
        lf.pack(fill="both", expand=True, padx=8, pady=(8,4))
        self.rec_tree = _mk_tree(lf, ("name","ip"), [240,200], height=9)
        cfg = load_cfg()
        for s in cfg.get("recent_servers", []):
            self.rec_tree.insert("","end", values=(s.get("name",s.get("ip","")), s.get("ip","")))

        br = tk.Frame(parent, bg=BG)
        br.pack(fill="x", padx=8, pady=(0,8))

        def connect():
            sel = self.rec_tree.selection()
            if not sel: return
            vals = self.rec_tree.item(sel[0],"values")
            ip = vals[1]
            entry = next((s for s in load_cfg().get("recent_servers",[])
                          if s.get("ip") == ip), {})
            self._on_select(ip, vals[0], entry.get("port","1757")); self.destroy()

        def save_fav():
            sel = self.rec_tree.selection()
            if not sel: return
            vals = self.rec_tree.item(sel[0],"values")
            ip, name = vals[1], vals[0]
            # If name is just the IP, ask for a proper label
            if name == ip or not name:
                name = simpledialog.askstring("Save Favourite",
                    f"Label for {ip}:", parent=self) or ip
            port = next((s.get("port","1757") for s in load_cfg().get("recent_servers",[])
                         if s.get("ip") == ip), "1757")
            cfg = load_cfg()
            saved = cfg.get("saved_servers", [])
            if not any(s["ip"] == ip for s in saved):
                saved.append({"name": name, "ip": ip, "port": port})
                cfg["saved_servers"] = saved
                save_cfg(cfg)
                messagebox.showinfo("Saved", f"'{name}' added to favourites.", parent=self)

        _btn(br, "Connect",        connect,  "primary",
             font=("Georgia",10,"bold"), pady=6, padx=14).pack(side="left")
        _btn(br, "★ Save as Fav",  save_fav, style="normal",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=6)

# ══════════════════════════════════════════════════════════════════════════════
#  MOD INSTALLATION  (MelonLoader + TavernLib)
# ══════════════════════════════════════════════════════════════════════════════

# The official MelonLoader project (Apache-2.0, github.com/LavaGang/MelonLoader)
# publishes these exact "always the latest release" download links itself —
# it's the same URL their own install guide points people to, just automated
# here instead of asking the player to click it. Note the org name: LavaGang,
# no hyphen — there are copy-cat repos with similar names floating around
# that should NOT be used as a source for this.
MELONLOADER_ZIP_URLS = {
    "x64": "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.x64.zip",
    "x86": "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.x86.zip",
}

# Fill this in with wherever you host TavernLib releases — a GitHub Releases
# asset URL or a raw.githubusercontent.com link both work fine, since this is
# just downloaded as a plain file.
TAVERNLIB_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernLib/releases/latest/download/TavernLib.dll"
TAVERNLIB_FILENAME = "TavernLib.dll"

# A small marker file dropped next to the game exe recording what we last
# installed, so later we can tell "outdated" apart from "never checked".
MODS_META_FILENAME = ".tavern_mods_meta.json"


def _mods_meta_path(game_dir):
    return os.path.join(game_dir, MODS_META_FILENAME)


def _load_mod_meta(game_dir):
    try:
        with open(_mods_meta_path(game_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_mod_meta(game_dir, meta):
    try:
        with open(_mods_meta_path(game_dir), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception:
        pass


def _get_redirect_location(url, timeout=10):
    """HEAD-requests a URL and returns the Location header of the *first*
    redirect hop, without following it. Used to read a GitHub 'latest
    release' download alias's resolved tag (e.g. 'v0.7.3') straight out of
    the redirect target, without downloading anything."""
    parsed = urlparse(url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    with _force_ipv4():
        conn = conn_cls(parsed.netloc, timeout=timeout)
        try:
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
            conn.request("HEAD", path, headers={"User-Agent": "TavernLauncher/1.0",
                                                 "Host": parsed.netloc})
            resp = conn.getresponse()
            resp.read()
            if 300 <= resp.status < 400:
                return resp.getheader("Location")
            return None
        finally:
            conn.close()


def _get_melonloader_latest_tag():
    """Reads the current MelonLoader release tag (e.g. 'v0.7.3') from the
    redirect target of its 'latest' download alias — no GitHub API call,
    no rate limit, and no need to download the (large) release zip."""
    loc = _get_redirect_location(
        "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.x64.zip")
    if not loc:
        return None
    # .../releases/download/v0.7.3/MelonLoader.x64.zip -> "v0.7.3"
    parts = loc.rstrip("/").split("/")
    try:
        return parts[parts.index("download") + 1]
    except (ValueError, IndexError):
        return None


def _fetch_remote_fingerprint(url, timeout=10):
    """A lightweight 'has this file changed' check — HEAD for ETag (falls
    back to Last-Modified, then Content-Length), without downloading the
    file. Needed for TavernLib specifically because its releases stay on a
    single tag name that never changes, so tag comparison can't detect
    updates the way it can for MelonLoader."""
    def _read(resp):
        h = resp.headers
        return h.get("ETag") or h.get("Last-Modified") or h.get("Content-Length") or ""
    with _force_ipv4():
        req = urllib.request.Request(url, method="HEAD",
            headers={"User-Agent": "TavernLauncher/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                fp = _read(resp)
                if fp: return fp
        except Exception:
            pass
        # Fallback for hosts that don't support HEAD on the (often presigned)
        # redirect target: a 1-byte ranged GET still reveals the same headers.
        req = urllib.request.Request(url, headers={
            "User-Agent": "TavernLauncher/1.0", "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read(resp)


def _detect_exe_arch(exe_path):
    """Reads the PE header to tell whether the game exe is 32- or 64-bit,
    so we grab the matching MelonLoader build. Returns 'x64', 'x86', or
    None if it can't be determined (unusual/corrupt file, unknown arch)."""
    try:
        with open(exe_path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\0\0":
                return None
            machine = struct.unpack("<H", f.read(2))[0]
            return {0x8664: "x64", 0x14c: "x86"}.get(machine)
    except Exception:
        return None


def _melonloader_installed(game_dir):
    return (os.path.isdir(os.path.join(game_dir, "MelonLoader")) and
            os.path.isfile(os.path.join(game_dir, "version.dll")))


def _tavernlib_installed(game_dir):
    return os.path.isfile(os.path.join(game_dir, "Plugins", TAVERNLIB_FILENAME))


# CircuitsVoiceChat ships as two DLLs (the mod itself plus the Concentus
# codec it depends on) in one release zip on its own repo — real GitHub
# releases, same "latest" alias trick as MelonLoader. Per the mod's own
# install instructions: the mod itself goes in Mods/, Concentus (a shared
# codec library) goes in UserLibs/.
CIRCUITSVOICECHAT_REPO = "CircuitLord/CircuitsVoiceChat"
CIRCUITSVOICECHAT_DESTINATIONS = {
    "CircuitsVoiceChat.dll": "Mods",
    "Concentus.dll": "UserLibs",
}

def _get_circuitsvoicechat_latest_tag():
    """Same redirect-peek trick as MelonLoader's tag check — no GitHub API
    call, no rate limit."""
    loc = _get_redirect_location(f"https://github.com/{CIRCUITSVOICECHAT_REPO}/releases/latest")
    if not loc:
        return None
    return loc.rstrip("/").split("/")[-1]

def _circuitsvoicechat_manual_paths():
    """Where a copy of both DLLs shipped with this launcher release is
    checked for, as an automatic fallback if the GitHub download fails or
    is taking too long — same reasoning as MelonLoader's bundled fallback."""
    return {name: os.path.join(_app_dir(), "Patch", name)
            for name in CIRCUITSVOICECHAT_DESTINATIONS}

def _circuitsvoicechat_installed(game_dir):
    return all(os.path.isfile(os.path.join(game_dir, subdir, name))
               for name, subdir in CIRCUITSVOICECHAT_DESTINATIONS.items())

def _circuitsvoicechat_status(game_dir):
    """Returns 'missing', 'outdated', 'unknown', or 'current' — same state
    machine as _melonloader_status, now that this has a real tag to check
    against instead of just a local file."""
    if not _circuitsvoicechat_installed(game_dir):
        return "missing"
    installed_tag = _load_mod_meta(game_dir).get("circuitsvoicechat_tag")
    if not installed_tag or installed_tag.startswith("bundled:"):
        return "unknown"
    try:
        latest = _get_circuitsvoicechat_latest_tag()
    except Exception:
        return "unknown"
    if not latest:
        return "unknown"
    return "current" if latest == installed_tag else "outdated"

def _install_circuitsvoicechat(game_dir, on_progress):
    """Tries downloading the latest CircuitsVoiceChat release first; if
    that fails, or a bundled copy exists in Patch/ and the download hasn't
    finished quickly, falls back to the bundled DLLs — the exact same
    network-first, fast-fallback pattern as _install_melonloader. Checks
    both destination files exist in whichever source is actually used
    before writing anything, so a partial zip or a missing bundled file
    can't leave the mod half-installed."""
    manual_paths = _circuitsvoicechat_manual_paths()
    have_bundled = all(os.path.isfile(p) for p in manual_paths.values())

    tag = None
    try: tag = _get_circuitsvoicechat_latest_tag()
    except Exception: pass

    downloaded_files = None  # filename -> bytes, populated only on a real successful download
    if tag:
        zip_filename = f"CircuitsVoiceChat-{tag}.zip"
        url = (f"https://github.com/{CIRCUITSVOICECHAT_REPO}/releases/latest/"
               f"download/{urllib.parse.quote(zip_filename)}")
        tmp_zip = os.path.join(tempfile.gettempdir(), "tavern_circuitsvoicechat_dl.zip")
        try:
            if have_bundled:
                # A good fallback is right there — don't make the user
                # wait long before using it.
                _download_with_progress(url, tmp_zip, on_progress,
                                         connect_timeout=8, max_total_seconds=15)
            else:
                _download_with_progress(url, tmp_zip, on_progress)
            on_progress("Extracting CircuitsVoiceChat…")
            found = {}
            with _open_zip_with_retry(tmp_zip) as zf:
                for wanted in CIRCUITSVOICECHAT_DESTINATIONS:
                    match = _find_zip_entry(zf, wanted)
                    if not match:
                        raise RuntimeError(
                            f"The downloaded release zip didn't contain {wanted}.")
                    found[wanted] = zf.read(match)
            downloaded_files = found
        except Exception:
            downloaded_files = None
            if not have_bundled:
                raise
            on_progress("Couldn't reach GitHub — using the version bundled with this launcher…")
        finally:
            try: os.remove(tmp_zip)
            except Exception: pass
    elif not have_bundled:
        raise RuntimeError(
            "Couldn't reach GitHub to check for CircuitsVoiceChat, and no bundled "
            "copy was found in Patch/ either.")

    for name, subdir in CIRCUITSVOICECHAT_DESTINATIONS.items():
        dest_dir = os.path.join(game_dir, subdir)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, name)
        if downloaded_files is not None:
            expected_hash = hashlib.sha256(downloaded_files[name]).hexdigest()
            with open(dest_path, "wb") as f:
                f.write(downloaded_files[name])
        else:
            expected_hash = _sha256_file(manual_paths[name])
            shutil.copy2(manual_paths[name], dest_path)
        # A silently-blocked write (Controlled Folder Access is a
        # documented example) can leave this looking like it succeeded —
        # no exception, no error — while the file on disk never actually
        # changed. Reading it back and comparing is the only reliable way
        # to tell a real success apart from that.
        if not os.path.isfile(dest_path) or _sha256_file(dest_path) != expected_hash:
            raise RuntimeError(
                f"{name} was written without any error, but checking it afterward shows "
                "it doesn't match what was just downloaded/copied. This usually means "
                "something on this PC silently blocked the write — most commonly Windows' "
                "Controlled Folder Access, or antivirus real-time protection. Try adding an "
                "exclusion for the game's install folder in Windows Security (or your "
                "antivirus), or temporarily disabling Controlled Folder Access, then try again.")

    meta = _load_mod_meta(game_dir)
    if downloaded_files is not None and tag:
        meta["circuitsvoicechat_tag"] = tag
    else:
        meta["circuitsvoicechat_tag"] = "bundled:local"
    _save_mod_meta(game_dir, meta)


@contextlib.contextmanager
def _force_ipv4():
    """Temporarily makes socket.getaddrinfo only return IPv4 results.
    Fixes a common real-world failure: a network where IPv6 is technically
    configured but the actual route is dead/blackholed, so anything that
    tries the (often-preferred) IPv6 address first just hangs instead of
    failing over. Browsers and curl dodge this automatically by racing both
    address families ("happy eyeballs"); plain urllib doesn't, so this
    nudges it into only ever trying IPv4."""
    _orig = socket.getaddrinfo
    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = _orig


def _urlopen_hard_timeout(req, connect_timeout=20, socket_timeout=20):
    """Runs urlopen() in a helper thread so a hung DNS lookup can't block
    forever — urlopen's own timeout= only bounds the socket connect/read
    once a connection attempt actually starts; DNS resolution happens
    before that and isn't covered by it at all. This is very likely what
    "stuck on Downloading MelonLoader, even as admin" actually was for at
    least some users: a permissions fix wouldn't touch a hung DNS lookup.
    If nothing happens within connect_timeout seconds, this gives up and
    raises rather than waiting on it — the abandoned attempt is a daemon
    thread, so it can't keep the app running even if it eventually returns."""
    result = {}
    def _do():
        try:
            result["resp"] = urllib.request.urlopen(req, timeout=socket_timeout)
        except Exception as e:
            result["error"] = e
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(connect_timeout)
    if t.is_alive():
        raise RuntimeError(
            f"Connecting to {urlparse(req.full_url).netloc} took too long and was "
            "abandoned. This usually means DNS resolution or the connection itself "
            "is hanging on this machine — often a VPN, a misconfigured router, or "
            "security software silently intercepting it rather than refusing it "
            "outright. Worth trying: disable any active VPN, try a different "
            "network (e.g. a phone hotspot) to confirm, or temporarily disable "
            "antivirus/firewall and retry.")
    if "error" in result:
        raise result["error"]
    return result["resp"]


def _download_with_progress(url, dest_path, on_progress,
                             connect_timeout=20, max_total_seconds=1800, chunk_size=1<<16):
    """Downloads url to dest_path, reporting live progress and enforcing a
    real wall-clock cap on the whole operation — a plain urlopen timeout=
    only guards a single socket operation, so a connection that trickles
    data just fast enough to dodge that never trips it and looks like a
    permanent hang rather than a slow download. Returns the response
    headers on success (some callers use these, e.g. for an ETag). Raises
    RuntimeError with a specific, actionable message on failure, and never
    leaves a partially-downloaded file at dest_path."""
    start = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "TavernLauncher/1.0"})
    with _force_ipv4():
        try:
            resp = _urlopen_hard_timeout(req, connect_timeout=connect_timeout)
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Couldn't connect to {urlparse(url).netloc} — {getattr(e,'reason',e)}\n\n"
                "This is usually a network/firewall/antivirus issue on this machine, "
                "not something wrong with the launcher itself. Worth trying:\n"
                "  • Run the launcher as Administrator\n"
                "  • Temporarily disable antivirus/VPN and retry\n"
                "  • Check whether a firewall is blocking outbound HTTPS for this app")

        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        downloaded = 0
        try:
            with resp, open(dest_path, "wb") as out:
                while True:
                    if time.time() - start > max_total_seconds:
                        raise RuntimeError(
                            f"Download stalled for over {max_total_seconds // 60} minutes — giving up. "
                            "The connection may be extremely slow, or something is "
                            "silently throttling it (security software, a captive "
                            "portal, etc.) rather than blocking it outright.")
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / max(1, total))
                        on_progress(f"Downloading… {pct}%  ({downloaded//1024:,} / {total//1024:,} KB)")
                    else:
                        on_progress(f"Downloading… {downloaded//1024:,} KB")
        except Exception:
            try: os.remove(dest_path)
            except Exception: pass
            raise
        return dict(resp.headers)


def _open_zip_with_retry(path, retries=8, delay=1.0):
    """Windows sometimes briefly locks a freshly-downloaded file while
    antivirus real-time protection scans it — and a .zip containing DLLs
    is exactly the kind of file that gets scanned most aggressively. A
    plain zipfile.ZipFile() open can stall or fail unpredictably during
    that window, with no timeout of its own (this is local disk I/O, not
    network, so the download's own timeout doesn't cover it at all). This
    retries a few times with short pauses — up to ~8s total — before
    giving up for real, rather than hanging indefinitely or failing on
    what's usually just a few seconds of transient scanning."""
    last_err = None
    for _ in range(retries):
        try:
            return zipfile.ZipFile(path)
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(
        f"Couldn't open the downloaded file — {last_err}\n\n"
        "This can happen if antivirus is still scanning it. Try clicking "
        "Install again, or temporarily disable real-time scanning and retry.")


def _find_zip_entry(zf, wanted_filename):
    """Finds a zip entry matching wanted_filename, tolerating a version
    suffix baked into the actual filename — e.g. the real CircuitsVoiceChat
    release ships "CircuitsVoiceChat-v1.0.4.dll" for what we track as
    "CircuitsVoiceChat.dll". That suffix changes every release, so an exact
    filename match would break on every version bump; matching by stem
    prefix + same extension instead means a new release just works without
    ever needing a code change here. Returns the zip entry's real name (for
    reading), or None if nothing matches."""
    stem, ext = os.path.splitext(wanted_filename)
    stem, ext = stem.lower(), ext.lower()
    for n in zf.namelist():
        b_stem, b_ext = os.path.splitext(os.path.basename(n))
        if b_ext.lower() == ext and b_stem.lower().startswith(stem):
            return n
    return None


def _melonloader_manual_zip_path(arch):
    """Where a copy of MelonLoader shipped with this launcher release is
    checked for, as an automatic fallback if the network download fails
    or is taking too long. Some networks (school/corporate proxies that
    need PAC/WPAD config Python doesn't evaluate, antivirus intercepting
    the download for scanning, firewalls that only allowlist browser
    traffic) block this app's own outbound request in ways no amount of
    retry/timeout logic can fix from the inside — bundling a known-good
    copy means the install still succeeds either way, with no user action
    needed. The network attempt still goes first, since it's the only way
    to get anything newer than whatever shipped with this build."""
    return os.path.join(_app_dir(), "Patch", f"MelonLoader.{arch}.zip")


def _install_melonloader(game_dir, arch, on_progress):
    """Tries downloading the latest official MelonLoader release first;
    if that fails, or a bundled copy exists and the download hasn't
    finished quickly, falls back to whatever shipped in Patch/ — so this
    succeeds either way without ever needing the user to do anything.
    Raises only if neither a working download nor a bundled copy exists."""
    manual_zip  = _melonloader_manual_zip_path(arch)
    have_bundled = os.path.isfile(manual_zip)
    url = MELONLOADER_ZIP_URLS.get(arch)
    if not url and not have_bundled:
        raise RuntimeError(f"Unsupported or unrecognized game architecture ({arch}).")

    tag = None
    downloaded_ok = False
    tmp_zip = os.path.join(tempfile.gettempdir(), "tavern_melonloader_dl.zip")

    if url:
        try: tag = _get_melonloader_latest_tag()
        except Exception: pass
        try:
            if have_bundled:
                # A good fallback is right there — don't make the user
                # wait long before using it.
                _download_with_progress(url, tmp_zip, on_progress,
                                         connect_timeout=8, max_total_seconds=15)
            else:
                _download_with_progress(url, tmp_zip, on_progress)
            downloaded_ok = True
        except Exception:
            if not have_bundled:
                raise
            on_progress("Couldn't reach GitHub — using the version bundled with this launcher…")

    source_zip = tmp_zip if downloaded_ok else manual_zip
    on_progress("Extracting MelonLoader…")
    with _open_zip_with_retry(source_zip) as zf:
        zf.extractall(game_dir)
    if downloaded_ok:
        try: os.remove(tmp_zip)
        except Exception: pass

    # A silently-blocked write (Controlled Folder Access is a documented
    # example) can leave extractall() looking like it succeeded — no
    # exception raised — while some or all of the files it just wrote
    # never actually landed on disk. Checking every extracted file's hash
    # would be overkill for something that installs dozens of them; the
    # two files _melonloader_installed already treats as proof of a real
    # install are a reasonable, proportionate stand-in for "did this
    # actually work."
    if not _melonloader_installed(game_dir):
        raise RuntimeError(
            "MelonLoader was extracted without any error, but checking afterward shows "
            "the expected files aren't actually there. This usually means something on "
            "this PC silently blocked the write — most commonly Windows' Controlled "
            "Folder Access, or antivirus real-time protection. Try adding an exclusion "
            "for the game's install folder in Windows Security (or your antivirus), or "
            "temporarily disabling Controlled Folder Access, then try again.")

    meta = _load_mod_meta(game_dir)
    if downloaded_ok and tag:
        meta["melonloader_tag"] = tag
    elif not downloaded_ok:
        # No real tag to record — a marker distinct enough that a later
        # status check (once network access works again) can still tell
        # this apart from "definitely current", prompting a real update.
        meta["melonloader_tag"] = f"bundled:{_sha256_file(manual_zip)[:12]}"
    _save_mod_meta(game_dir, meta)


def _tavernlib_manual_dll_path():
    """Same idea as _melonloader_manual_zip_path — a copy of TavernLib.dll
    shipped with this launcher release, used automatically as a fallback
    if the network download fails or is taking too long."""
    return os.path.join(_app_dir(), "Patch", "TavernLib.dll")


def _install_tavernlib(game_dir, on_progress):
    """Tries downloading the latest TavernLib.dll first; if that fails, or
    a bundled copy exists and the download hasn't finished quickly, falls
    back to whatever shipped in Patch/ — so this succeeds either way
    without ever needing the user to do anything. Always swaps the result
    in atomically, so a failed/interrupted attempt can never leave a
    corrupt half-downloaded file in place."""
    plugins_dir = os.path.join(game_dir, "Plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    dest = os.path.join(plugins_dir, TAVERNLIB_FILENAME)
    tmp_dest = dest + ".download"

    manual_dll   = _tavernlib_manual_dll_path()
    have_bundled = os.path.isfile(manual_dll)
    fingerprint  = ""
    try:
        if have_bundled:
            headers = _download_with_progress(TAVERNLIB_DOWNLOAD_URL, tmp_dest, on_progress,
                                                connect_timeout=8, max_total_seconds=15)
        else:
            headers = _download_with_progress(TAVERNLIB_DOWNLOAD_URL, tmp_dest, on_progress)
        fingerprint = headers.get("ETag") or headers.get("Last-Modified") or ""
    except Exception:
        if not have_bundled:
            raise
        on_progress("Couldn't reach GitHub — using the version bundled with this launcher…")
        shutil.copy2(manual_dll, tmp_dest)
        fingerprint = f"bundled:{_sha256_file(manual_dll)[:12]}"

    # Captured before the replace, since tmp_dest won't exist anymore
    # afterward — os.replace renames it, it doesn't leave a copy behind.
    expected_hash = _sha256_file(tmp_dest)
    os.replace(tmp_dest, dest)  # atomic on Windows — always a full swap, never a partial one
    if not os.path.isfile(dest) or _sha256_file(dest) != expected_hash:
        # A silently-blocked write (Controlled Folder Access is a
        # documented example) can leave os.replace appearing to succeed
        # with the old file — or nothing at all — actually still there.
        # Reading the result back and comparing is the only reliable way
        # to tell a real success apart from that.
        raise RuntimeError(
            "TavernLib.dll was written without any error, but checking it afterward "
            "shows it doesn't match what was just downloaded. This usually means "
            "something on this PC silently blocked the write — most commonly Windows' "
            "Controlled Folder Access, or antivirus real-time protection. Try adding an "
            "exclusion for the game's install folder in Windows Security (or your "
            "antivirus), or temporarily disabling Controlled Folder Access, then try again.")
    if fingerprint:
        meta = _load_mod_meta(game_dir)
        meta["tavernlib_fingerprint"] = fingerprint
        _save_mod_meta(game_dir, meta)


def _melonloader_status(game_dir):
    """Returns 'missing', 'outdated', 'unknown' (installed, but we have no
    baseline to compare — e.g. it was installed by hand before this feature
    existed, or the update check failed), or 'current'."""
    if not _melonloader_installed(game_dir):
        return "missing"
    installed_tag = _load_mod_meta(game_dir).get("melonloader_tag")
    if not installed_tag:
        return "unknown"
    try:
        latest = _get_melonloader_latest_tag()
    except Exception:
        return "unknown"
    if not latest:
        return "unknown"
    return "current" if latest == installed_tag else "outdated"


def _tavernlib_status(game_dir):
    if not _tavernlib_installed(game_dir):
        return "missing"
    installed_fp = _load_mod_meta(game_dir).get("tavernlib_fingerprint")
    if not installed_fp:
        return "unknown"
    try:
        latest_fp = _fetch_remote_fingerprint(TAVERNLIB_DOWNLOAD_URL)
    except Exception:
        return "unknown"
    if not latest_fp:
        return "unknown"
    return "current" if latest_fp == installed_fp else "outdated"


def _mods_need_attention(game_dir):
    """True if either required mod (MelonLoader, TavernLib) is missing or
    outdated — the trigger for flashing the main window's Setup button (see
    _refresh_setup_alert, which also folds in the patch check). Network
    failures during the update checks never trigger a false alarm on their
    own — only a real missing install (a purely local, always-reliable
    check) does that unconditionally."""
    return (_melonloader_status(game_dir) in ("missing", "outdated") or
            _tavernlib_status(game_dir)   in ("missing", "outdated"))


# ── Patch ─────────────────────────────────────────────────────────────────────
# themoddingtavern.dll lives in a Patch/ folder next to this launcher exe.
# Applying the patch means copying it into the game's Assembly folder under
# the name Root.Township.dll (replacing whatever was there before).
# themoddingtavern.dll lives in a Patch/ folder next to this launcher exe,
# but a canonical copy is now also published as a GitHub release asset —
# this lets an already-built launcher pick up a newer patch DLL without
# needing a whole new launcher release, the same way TavernLib/MelonLoader
# updates already work independently of the launcher's own version.
PATCH_DOWNLOAD_URL = "https://github.com/ModdingTavern/TavernDefaults/releases/latest/download/themoddingtavern.dll"
PATCH_SOURCE_FILENAME = "themoddingtavern.dll"
PATCH_TARGET_SUBDIR   = os.path.join("A Township Tale_Data", "Managed")
PATCH_TARGET_FILENAME = "Root.Township.dll"


def _patch_source_path():
    """Full path to themoddingtavern.dll in the Patch/ folder next to the launcher."""
    return os.path.join(_app_dir(), "Patch", PATCH_SOURCE_FILENAME)


def _patch_target_path(game_exe):
    """Full path where Root.Township.dll lives in the game's Managed folder."""
    game_dir = os.path.dirname(game_exe)
    return os.path.join(game_dir, PATCH_TARGET_SUBDIR, PATCH_TARGET_FILENAME)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _patch_is_applied(game_exe):
    """True if the installed Root.Township.dll's content matches the hash
    apply_patch most recently confirmed writing there, recorded in the
    per-game-dir meta file (see _load_mod_meta) rather than compared against
    the local Patch/themoddingtavern.dll fallback copy. Comparing against the
    local bundled copy broke as soon as apply_patch actually used its GitHub
    download (the common case, whenever GitHub is reachable): the installed
    file matched what was genuinely just applied, but not a bundled reference
    that's either stale or missing entirely, so this reported "not applied"
    right after a successful patch. Hashing what we ourselves last wrote
    fixes that regardless of which source supplied it. The meta file lives in
    game_dir, not per-launcher state, so if the client launcher already
    patched a given game install, the server launcher (or vice versa)
    correctly sees it as already done too, as long as they're both pointed
    at the same game folder. No re-patching, no re-flashing."""
    game_dir = os.path.dirname(game_exe)
    dst = _patch_target_path(game_exe)
    recorded = _load_mod_meta(game_dir).get("patch_sha256")
    if not recorded or not os.path.isfile(dst):
        return False
    try:
        return _sha256_file(dst) == recorded
    except OSError:
        return False


def apply_patch(game_exe, on_progress=None):
    """Installs themoddingtavern.dll as Root.Township.dll in the game's
    Managed folder. Prioritizes the latest release published at
    ModdingTavern/TavernDefaults on GitHub — falls back to whatever's
    bundled in Patch/ next to this launcher if GitHub can't be reached
    for any reason (offline, firewall, GitHub itself down, etc.), exactly
    like the old, GitHub-unaware version of this function always did.
    Either way, compares against what's already installed first and skips
    the actual write entirely if it already matches, rather than always
    replacing unconditionally.

    Returns one of:
      "downloaded" — installed the latest version fetched from GitHub
      "bundled"    — GitHub wasn't reachable; installed the local Patch/ copy instead
      "current"    — what's already installed already matches; nothing changed

    Raises RuntimeError with a user-friendly message on any failure —
    including a *silent* one: some Windows security features (Controlled
    Folder Access is a documented example) can intercept a file write and
    let the calling process believe it succeeded without the change
    actually landing on disk. From this code's side, that looks identical
    to a real, successful copy — shutil.copy2/os.replace raise nothing
    either way. The only reliable way to catch it is to read the
    destination back afterward and confirm it actually matches what was
    just written, rather than trusting the write call's own apparent
    success."""
    if on_progress is None:
        on_progress = lambda msg: None

    game_dir = os.path.dirname(game_exe)
    dst = _patch_target_path(game_exe)
    managed_dir = os.path.dirname(dst)
    if not os.path.isdir(managed_dir):
        raise RuntimeError(
            f"Game Managed folder not found:\n{managed_dir}\n\n"
            "Double-check the game exe path at the top of the launcher.")

    tmp_dest = dst + ".download"
    try:
        try:
            on_progress("Checking for the latest patch…")
            _download_with_progress(PATCH_DOWNLOAD_URL, tmp_dest, on_progress,
                                     connect_timeout=8, max_total_seconds=20)
            source = "downloaded"
        except Exception:
            local_src = _patch_source_path()
            if not os.path.isfile(local_src):
                raise RuntimeError(
                    "Couldn't reach GitHub to check for the latest patch, and no "
                    f"bundled copy was found in Patch/ either.\n\nExpected at:\n{local_src}")
            on_progress("Couldn't reach GitHub — using the version bundled with this launcher…")
            shutil.copy2(local_src, tmp_dest)
            source = "bundled"

        new_hash = _sha256_file(tmp_dest)
        already_current = os.path.isfile(dst) and _sha256_file(dst) == new_hash
        if not already_current:
            # Not already exactly what we'd install — swap it in. (If it
            # already matches, skip the write entirely rather than
            # rewriting, and re-triggering AV scanning of, a file that's
            # already correct.)
            os.replace(tmp_dest, dst)  # atomic on Windows — always a full swap, never a partial one
            if not os.path.isfile(dst) or _sha256_file(dst) != new_hash:
                raise RuntimeError(
                    "The file was written without any error, but checking it afterward "
                    "shows it doesn't match what was just installed. This usually means "
                    "something on this PC silently blocked the write — most commonly "
                    "Windows' Controlled Folder Access, or antivirus real-time protection. "
                    "Try adding an exclusion for the game's install folder in Windows "
                    "Security (or your antivirus), or temporarily disabling Controlled "
                    "Folder Access, then try again.")

        # Record what's now confirmed to be sitting at dst so
        # _patch_is_applied (any launcher, any time) can recognize it without
        # needing to compare against the local Patch/ fallback copy, which
        # may be stale or absent by the time this runs.
        meta = _load_mod_meta(game_dir)
        meta["patch_sha256"] = new_hash
        _save_mod_meta(game_dir, meta)

        return "current" if already_current else source
    finally:
        try:
            if os.path.isfile(tmp_dest):
                os.remove(tmp_dest)
        except Exception:
            pass


#  CONSOLE COMMANDS  (for autocomplete in ConsoleWindow)
# ══════════════════════════════════════════════════════════════════════════════
# Extracted from the community command reference doc — command -> list of
# parameter names it expects, in order. Not guaranteed to be 100% exhaustive
# or perfectly current with every game version, but covers the documented
# set well enough to be a genuinely useful autocomplete — same idea as any
# professional console/CLI tool offering completions (with argument hints)
# against a known command list.
CONSOLE_COMMANDS = {
    "agents print": [],
    "audio microphone mute": [],
    "audio microphone unmute": [],
    "chunks check-wipe": [],
    "chunks entities": ["chunk"],
    "chunks force-load": ["chunk", "isForceLoaded"],
    "chunks info": ["chunk"],
    "chunks loadall": ["loaded"],
    "chunks merge": ["prefabHashes"],
    "chunks print-all": [],
    "chunks print-loaded": [],
    "chunks set-load": ["loadDistance", "unloadDistance"],
    "chunks set-receive-count": ["receiveAmount"],
    "chunks set-receive-ms": ["receiveDuration"],
    "chunks set-static-load": ["isLoading"],
    "chunks set-static-per-frame": ["numberToLoadPerFrame"],
    "chunks set-static-sequence": ["loadSequentially"],
    "chunks set-sync-amount": ["syncAmount"],
    "chunks set-sync-interval": ["interval"],
    "chunks set-timed": ["processingPercent", "maxAllowance", "minNeeded"],
    "chunks wipe": ["chunk"],
    "debug count": ["prefab"],
    "debug count-all": [],
    "debug count-behaviours": ["behaviourName", "isEnabled"],
    "debug count-scripts": ["typeName", "isCountingResources", "isFindingChildren"],
    "debug entities": [],
    "debug entity-health": [],
    "debug export-navmesh": ["output"],
    "debug fixedtime": [],
    "debug lag": ["targetFPS"],
    "debug load-marker": ["player"],
    "debug nameid": ["id"],
    "debug open-logs": [],
    "debug prefabcounts": [],
    "debug print-names": ["scriptName"],
    "debug remote-console": [],
    "debug server-stats": [],
    "debug set": ["player", "index"],
    "debug static check-current": [],
    "debug static fix": ["hashes"],
    "debug static list": [],
    "debug static modify": ["hash", "state"],
    "debug tracking get": ["name"],
    "debug tracking remove": ["name"],
    "debug tracking track": ["entityId", "name"],
    "debug turabada-ai": [],
    "festivities info": ["festivity"],
    "festivities list": [],
    "festivities start": ["festivity"],
    "festivities stop": ["festivity"],
    "game connect": ["serverIdentifier", "playerMode"],
    "game connect-player": ["player", "playerMode"],
    "game create-server": ["serverName", "sceneIndex", "region"],
    "game delete-save": ["serverIdentifier"],
    "game find": ["name"],
    "game ip-local": ["sceneIndex", "playerMode", "port"],
    "game join-ip": ["serverIp", "sceneIndex", "playerMode", "port"],
    "game join-server": ["serverIdentifier", "playerMode"],
    "game list-recent": [],
    "game local-test": ["serverIdentifier", "playerMode"],
    "game local-test-scene": ["scene", "playerMode"],
    "game show-all": [],
    "game show-discover": [],
    "game show-online": [],
    "game show-open": [],
    "game show-owned": [],
    "game show-public": [],
    "game start-local": ["sceneIndex", "isExternalLaunch", "port", "isHeadless", "isRunningLocally"],
    "game start-server": ["server", "isExternalLaunch", "port", "isHeadless", "isRunningLocally"],
    "game startclean": ["server", "isExternalLaunch", "port", "isHeadless", "isRunningLocally"],
    "game stop-mode": [],
    "global-population list": [],
    "global-population set-size": ["population", "maxSpawned"],
    "global-population spawned": ["population"],
    "global-population teleport": ["player", "population", "distanceAway"],
    "global-population teleport-to": ["player", "population", "index", "distanceAway"],
    "help": ["path"],
    "help full": ["path"],
    "help modules": [],
    "help search": ["searchString"],
    "impacts sync": ["isSubscribing"],
    "info": [],
    "info cli": [],
    "info player-mode": [],
    "info server": [],
    "info system": [],
    "info user": [],
    "info version": [],
    "landmarks enable": ["enabled"],
    "landmarks load-enabled": ["isLoading"],
    "leaderboard create": ["courseName", "isLowest"],
    "leaderboard get-rank": ["courseName"],
    "leaderboard list": [],
    "leaderboard remove-checkpoint": ["courseName", "isBeginning"],
    "leaderboard set-board": ["courseName"],
    "leaderboard set-checkpoint": ["courseName", "isBeginning"],
    "login": ["username", "password"],
    "logout": [],
    "logs": [],
    "logs change-target": ["targetName", "minLevel", "maxLevel", "loggerNamePattern"],
    "logs config": ["isPrintingAll"],
    "logs destroy-trace": ["isEnabled"],
    "logs warn-stack": ["isEnabled"],
    "maintenance garbage": [],
    "maintenance resources": [],
    "microtutorial active": [],
    "microtutorial exit": ["player"],
    "microtutorial list": [],
    "microtutorial next-step": ["player"],
    "microtutorial set-log-level": ["level"],
    "microtutorial start": ["player", "tutorialSettings"],
    "mods": [],
    "mods add": ["name", "content", "isOverride"],
    "mods path": [],
    "mods refresh": [],
    "mods remove": ["name"],
    "mods restart": ["name"],
    "mods start": ["name"],
    "mods stop": ["name"],
    "player check-stat": ["player", "stat"],
    "player count": [],
    "player cripple": ["players"],
    "player detailed": ["player"],
    "player get-home": ["player"],
    "player getdamagemulti": [],
    "player god-mode": ["players", "isOn"],
    "player id": ["username"],
    "player inventory": ["players"],
    "player inventory load": ["user", "save"],
    "player inventory save": ["user"],
    "player kick": ["players", "reason"],
    "player kill": ["players"],
    "player list": [],
    "player list-detailed": [],
    "player list-stats": ["player"],
    "player message": ["players", "message", "duration"],
    "player modify-stat": ["players", "statDefinition", "valueModifier", "duration", "isMultiplier"],
    "player progression allxp": ["players", "xp"],
    "player progression buyskill": ["slotIndex", "isConsumingExperience"],
    "player progression checkallxp": ["players"],
    "player progression clearall": ["players"],
    "player progression clearpath": ["player", "path"],
    "player progression list": [],
    "player progression offlinelevels": ["userInfo", "path", "levels"],
    "player progression pathlevelup": ["players", "path"],
    "player progression pathxp": ["players", "path", "xp"],
    "player progression printofflinelevels": [],
    "player progression showskills": ["player"],
    "player set-home": ["players", "home"],
    "player set-stat": ["players", "statDefinition", "value", "applicationType"],
    "player set-unlock": ["players", "unlock", "isUnlocked"],
    "player setdamagemulti": ["multiplier"],
    "player teleport": ["players", "target"],
    "player unlock-cancel": ["player"],
    "player unlock-check": ["player", "unlock"],
    "player username": ["userId"],
    "profiling cleanslatememory": ["name", "path"],
    "profiling dumpsize": [],
    "profiling heapdump": [],
    "profiling memorydump": ["name", "path"],
    "profiling sample": ["name", "path", "frames", "postAction"],
    "progress fill-book": ["collection"],
    "progress fill-books": [],
    "progress fillcaveteleporter": ["layer", "fuelQuantity"],
    "progress fillcommunityboxes": [],
    "progress finishboxes": [],
    "progress forgeall": [],
    "progress generatecaves": ["layer", "debugCallback"],
    "progress list-books": [],
    "progress listcaveteleporter": [],
    "progress repaircaveteleporters": ["layer"],
    "quality dynamic-load-multiplier": ["loadMultiplier"],
    "quality lod-bias": ["lodbias"],
    "quality static-load-multiplier": ["loadMultiplier"],
    "quit": [],
    "recent-players test-record": ["name", "id", "interactionType"],
    "repair-box": ["player", "item1", "count1", "item2", "count2", "item3", "count3", "output", "localSpawnPosition"],
    "repair-box list-items": [],
    "repeat": ["count", "intervalInSeconds", "isLoggingProgress"],
    "repeat last": [],
    "repeat stop": ["id"],
    "repeat stopall": [],
    "report-players create": ["userId", "type", "serverId"],
    "report-players list": ["statusFilter", "serverFilter"],
    "save": [],
    "save backup": ["isInstant"],
    "save full-wipe": [],
    "save now": [],
    "save player-wipe": ["user", "isWipingLockbox", "isWipingATM", "isWipingPostbox", "isWipingLandmarks", "isWipingMap"],
    "save player-wipe-all": ["isWipingATM", "isWipingPostbox", "isWipingLandmarks", "isWipingMap"],
    "save wipe": ["isSettingOffline"],
    "save wipe-forced": ["keepPlayers"],
    "save wipe-storage": [],
    "save wipecache": ["isSettingOffline"],
    "save wipecaves": [],
    "select": ["identifier"],
    "select destroy": [],
    "select find": ["player", "distance"],
    "select get": ["identifier"],
    "select look-at": ["player", "isRotatingAllAxis"],
    "select move back": ["amount"],
    "select move down": ["amount"],
    "select move exact": ["position"],
    "select move forward": ["amount"],
    "select move left": ["amount"],
    "select move right": ["amount"],
    "select move up": ["amount"],
    "select prefab": ["prefab", "player"],
    "select rotate exact": ["rotation"],
    "select rotate pitch": ["degrees"],
    "select rotate roll": ["degrees"],
    "select rotate yaw": ["degrees"],
    "select snap-ground": [],
    "select snap-to": ["identifier"],
    "select tostring": [],
    "select unselect": [],
    "server migrate": ["secondsLeftUntilTermination"],
    "server proxy": ["serverId", "proxiedCommand"],
    "server start": ["serverId", "isExternalLaunch", "port", "isHeadless", "isRunningLocally"],
    "settings changesetting": ["settingsTarget", "setting", "value"],
    "settings disable-board": [],
    "settings enable-board": [],
    "settings heat": ["multiplier"],
    "settings infoboard": ["identifier", "value"],
    "settings infoboard-list": [],
    "settings list": ["settingsTarget"],
    "settings population check-time": ["population"],
    "settings population reset-time": ["population"],
    "settings population set-time": ["population", "timeInSeconds"],
    "settings possible": ["settingsTarget", "setting"],
    "settings reset": [],
    "settings save": [],
    "settings settoggle": ["setting", "isOn"],
    "settings toggle": ["setting"],
    "social addfriend": ["player"],
    "social listfriends": ["user"],
    "social removefriend": ["player"],
    "spawn": ["players", "prefab", "arguments"],
    "spawn exact": ["position", "rotation", "prefab", "arguments"],
    "spawn find": ["name"],
    "spawn infodump": [],
    "spawn list": [],
    "spawn list materials": [],
    "spawn local": ["prefab"],
    "spawn moulds": ["players", "prefab"],
    "spawn moulds list": [],
    "spawn multi": ["players", "name", "args"],
    "spawn package drop": ["players", "arguments"],
    "spawn package list": [],
    "spawn pages list": [],
    "spawn pages spawn": ["players", "collection"],
    "spawn population": ["player", "population", "populationType", "size", "startingPopulation", "maxPopulation"],
    "spawn population-list": [],
    "spawn string": ["players", "value"],
    "spawn string-raw": ["value"],
    "time": [],
    "time future": ["progress", "measure"],
    "time keywords": [],
    "time ofday": ["isNormalised"],
    "time set": ["time"],
    "time toggle": [],
    "trade atm add": ["user", "quantityToAdd"],
    "trade atm get": ["user"],
    "trade atm set": ["user", "quantity"],
    "trade empty": [],
    "trade post": ["user", "prefab", "arguments"],
    "trade post-string": ["user", "value"],
    "trial list": [],
    "trial players": ["key"],
    "trial progress": ["key"],
    "trial reset": ["key"],
    "users statistics": ["user"],
    "wacky chisel-deck": [],
    "wacky cleanplayerbody": ["player"],
    "wacky cleanplayerstate": ["player"],
    "wacky destroy": ["id"],
    "wacky destroy-free": ["prefab", "chunks"],
    "wacky destroyall": ["prefab"],
    "wacky inputscale": ["scale"],
    "wacky marcopolo": ["player"],
    "wacky ow-loot": [],
    "wacky replace": ["id"],
    "wacky replace-selected": [],
    "wacky setvolume": ["local", "remote"],
    "wacky smelter": [],
    "websocket subscribe": ["eventType"],
    "websocket subscriptions": [],
    "websocket unsubscribe": ["eventType"],
    "world caves reset": [],
    "world caves teleport": ["players", "layer"],
    "world forest info": ["forest"],
    "world forest list": [],
    "world forest reset": ["forest"],
    "world forest teleport": ["players", "forest", "nodeIndex"],
}

class RemoteConsoleWindow(tk.Toplevel):
    """Lets a player connect to a server owner's console *remotely*,
    authenticating with a console_token the owner shares with them
    directly — never anything read from a local file, unlike server.exe's
    own ConsoleWindow, since (unlike a server admin talking to their own
    locally-running game) there's no local console_token.txt for this
    launcher to trust in the first place. The console UI itself, once
    connected, is identical to server.exe's version — same protocol, same
    autocomplete, same everything — just reached via a connect step
    first instead of an automatic local connection."""
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Remote Console")
        self.configure(bg=BG)
        self.resizable(True, True)
        _set_window_icon(self)
        self._ws_client = None
        self._connected = False
        self._stop      = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_connect_form()
        _enable_dark_titlebar(self)

    def _build_connect_form(self):
        self.geometry("420x320")
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🖥  Remote Console", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Connects to another server owner's console remotely. You'll "
                 "need the server's IP and the console token they give you directly "
                 "— this is never read from any file on your own machine.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=370, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,4))

        _section_label(self, "SERVER IP")
        hf = _field(self)
        self.v_host = tk.StringVar()
        host_entry = tk.Entry(hf, textvariable=self.v_host, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6)
        host_entry.pack(fill="x")

        _section_label(self, "CONSOLE TOKEN")
        tf = _field(self)
        self.v_token = tk.StringVar()
        token_entry = tk.Entry(tf, textvariable=self.v_token, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, show="●")
        token_entry.pack(fill="x")
        tk.Label(self,
            text="Given to you by the server owner — the same token their "
                 "own server.exe uses for its own local console access.",
            bg=BG, fg=MUTED, font=("Segoe UI",8), justify="left", anchor="w",
            wraplength=370).pack(fill="x", padx=22, pady=(2,4))

        self._connect_status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._connect_status, bg=BG, fg=RED,
                 font=("Segoe UI",9), wraplength=370, justify="left"
        ).pack(anchor="w", padx=22, pady=(2,0))

        self._connect_btn = _btn(self, "Connect", self._attempt_connect, "primary",
             font=("Georgia",10,"bold"), pady=10)
        self._connect_btn.pack(fill="x", padx=20, pady=(10,16))

        host_entry.bind("<Return>", lambda e: token_entry.focus_set())
        token_entry.bind("<Return>", lambda e: self._attempt_connect())
        host_entry.focus_set()

        self.update_idletasks()
        self.geometry(f"420x{self.winfo_reqheight()}")

    def _attempt_connect(self):
        host = self.v_host.get().strip()
        token = self.v_token.get().strip()
        if not host or not token:
            self._connect_status.set("Enter both the server IP and the console token.")
            return
        self._connect_btn.config(state="disabled", text="Connecting…")
        self._connect_status.set("")
        self._ws_client = WsConsoleClient()

        def worker():
            ok, msg = self._ws_client.connect(
                host, token,
                on_line=lambda t: self.after(0, lambda p=t: self._append(p)),
                on_disc=lambda r: self.after(0, lambda: self._on_disconnected(r)),
            )
            if ok:
                self._connected = True
                self.after(0, lambda: self._connect_succeeded(host))
            else:
                self.after(0, lambda m=msg: self._connect_failed(m))
        threading.Thread(target=worker, daemon=True).start()

    def _connect_failed(self, msg):
        self._connect_btn.config(state="normal", text="Connect")
        self._connect_status.set(msg)

    def _connect_succeeded(self, host):
        self._host = host
        for child in list(self.winfo_children()):
            child.destroy()
        self.minsize(1, 1)
        self._build_console_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._append("[Connected]\n", "ok")

    def _build_console_ui(self):
        self.geometry("700x560")
        self.minsize(620, 480)
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text=f"🖥  Remote Console — {self._host}", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        self._status_var = tk.StringVar(value="Connected")
        tk.Label(h, textvariable=self._status_var, bg=SURF, fg=MUTED,
                 font=("Segoe UI",9)).pack(side="right", padx=16)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=12, pady=(10,6))
        lb = tk.Frame(lf, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True)
        self.out = tk.Text(lb, bg=SURF, fg="#b09a78", font=MONO, height=20,
                           relief="flat", bd=0, state="disabled", wrap="word")
        sb = _mk_scrollbar(lb, self.out.yview)
        sb.pack(side="right", fill="y")
        self.out.config(yscrollcommand=sb.set)
        self.out.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        for t,c in [("ok",GREEN),("warn",AMBER),("err",RED),("cyan",CYAN)]:
            self.out.tag_config(t, foreground=c)

        cf = tk.Frame(self, bg=BG)
        cf.pack(fill="x", padx=12, pady=(0,12))
        self.v_cmd = tk.StringVar()
        entry = tk.Entry(cf, textvariable=self.v_cmd, bg=SURF, fg=PARCH,
                         insertbackground=AMBER, relief="flat", font=("Consolas",10),
                         bd=6)
        entry.pack(side="left", fill="x", expand=True)
        entry.focus_set()
        _btn(cf, "Send", self._send, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=14).pack(side="left", padx=(6,0))

        # ── Command autocomplete ──────────────────────────────────────────────
        # A floating suggestion list under the entry, positioned via place(in_=)
        # so it overlays correctly regardless of the pack layout around it.
        # Wrapped in its own frame (rather than placing the Listbox directly)
        # so a real Scrollbar can sit alongside it — some command groups
        # (e.g. "player") have 30+ matches for one prefix, far more than
        # comfortably fit on screen at once, and without a scrollbar those
        # extra matches would be completely unreachable by mouse, not just
        # initially hidden.
        self._ac_frame = tk.Frame(self, bg=SURF2, highlightbackground=BORDER,
                                  highlightthickness=1)
        self._ac_listbox = tk.Listbox(self._ac_frame, bg=SURF2, fg=PARCH,
                                      selectbackground=AMBERDIM, selectforeground="#ffd080",
                                      relief="flat", bd=0, highlightthickness=0,
                                      font=("Consolas",10), activestyle="none")
        self._ac_scrollbar = _mk_scrollbar(self._ac_frame, self._ac_listbox.yview)
        self._ac_scrollbar.pack(side="right", fill="y")
        self._ac_listbox.config(yscrollcommand=self._ac_scrollbar.set)
        self._ac_listbox.pack(side="left", fill="both", expand=True)
        self._ac_visible = False
        self._ac_matches = []

        def _hide_autocomplete():
            if self._ac_visible:
                self._ac_frame.place_forget()
                self._ac_visible = False

        def _show_autocomplete(matches):
            self._ac_matches = matches
            self._ac_listbox.delete(0, "end")
            for m in matches:
                params = CONSOLE_COMMANDS.get(m, [])
                display = f"{m}  [{', '.join(params)}]" if params else m
                self._ac_listbox.insert("end", display)
            self._ac_listbox.selection_clear(0, "end")
            self._ac_listbox.selection_set(0)
            self._ac_listbox.config(height=min(8, len(matches)))
            self._ac_frame.place(in_=entry, x=0, rely=0.0, anchor="sw",
                                 width=entry.winfo_width())
            self._ac_frame.lift()
            self._ac_visible = True

        def _update_autocomplete(event=None):
            if event is not None and event.keysym in ("Down","Up","Tab","Return","Escape"):
                return
            text = self.v_cmd.get().strip().lower()
            if not text:
                _hide_autocomplete()
                return
            matches = [c for c in CONSOLE_COMMANDS if c.startswith(text)]
            if matches and matches != [text]:
                _show_autocomplete(matches)
            else:
                _hide_autocomplete()

        def _accept_selected(event=None):
            if not self._ac_visible:
                return None
            sel = self._ac_listbox.curselection()
            idx = sel[0] if sel else 0
            if 0 <= idx < len(self._ac_matches):
                self.v_cmd.set(self._ac_matches[idx] + " ")
                entry.icursor("end")
            _hide_autocomplete()
            return "break"

        def _move_selection(delta):
            if not self._ac_visible:
                return
            sel = self._ac_listbox.curselection()
            idx = sel[0] if sel else 0
            idx = max(0, min(len(self._ac_matches)-1, idx+delta))
            self._ac_listbox.selection_clear(0, "end")
            self._ac_listbox.selection_set(idx)
            self._ac_listbox.activate(idx)
            self._ac_listbox.see(idx)

        def _on_return(event=None):
            if self._ac_visible:
                return _accept_selected()
            self._send()
            return None

        def _on_listbox_click(event=None):
            _accept_selected()
            entry.focus_set()

        entry.bind("<KeyRelease>", _update_autocomplete)
        entry.bind("<Return>", _on_return)
        entry.bind("<Tab>", _accept_selected)
        entry.bind("<Down>", lambda e: (_move_selection(1), "break")[1])
        entry.bind("<Up>", lambda e: (_move_selection(-1), "break")[1])
        entry.bind("<Escape>", lambda e: _hide_autocomplete())
        self._ac_listbox.bind("<ButtonRelease-1>", _on_listbox_click)
        self._hide_autocomplete = _hide_autocomplete

    def _append(self, text, tag=""):
        self.out.config(state="normal")
        self.out.insert("end", text, tag)
        self.out.see("end")
        self.out.config(state="disabled")

    def _on_disconnected(self, msg):
        self._connected = False
        self._status_var.set("Disconnected")
        self._append(f"\n[{msg}]\n", "err")

    def _send(self):
        cmd = self.v_cmd.get().strip()
        if not cmd or not self._connected:
            return
        self.v_cmd.set("")
        self._append(f"> {cmd}\n", "cyan")
        self._ws_client.send(cmd)

    def _on_close(self):
        self._stop.set()
        if hasattr(self, "_ws_client") and self._ws_client:
            self._ws_client.disconnect()
        self.destroy()


class TavernKeeperWindow(tk.Toplevel):
    """A native prefab spawn/select/move/settings/admin tool, on the same
    remote console connection as RemoteConsoleWindow — built to replicate
    the community's Prefabulator tool without depending on it directly (it
    authenticates through Alta's own official servers, which doesn't work
    for a private server that never registers there).

    The console connection here has no idea which in-game player is "the
    one using this" — there's no such concept, it's a plain admin pipe
    into the server. Every position-based command (select find, select
    prefab, select look-at, ...) takes an explicit player name to anchor
    around. That's why there's a Target Player selector rather than any
    assumption baked in about whose position "nearby" means."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("TavernKeeper")
        self.configure(bg=BG)
        self.resizable(True, True)
        _set_window_icon(self)
        self._ws_client = None
        self._connected = False
        self._stop = threading.Event()
        self._prefab_list = None
        self._save_items = []  # list of (label, spawn_string) staged for Save/Load
        self._build_connect_form()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _enable_dark_titlebar(self)

    # ── Connection ───────────────────────────────────────────────────────────

    def _build_connect_form(self):
        self.geometry("420x300")
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🧩  TavernKeeper", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Connects to a server's console, the same way Remote Console does. "
                 "You'll need the server's IP and a console token from the owner.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=370, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,4))

        _section_label(self, "SERVER IP")
        hf = _field(self)
        self.v_host = tk.StringVar()
        host_entry = tk.Entry(hf, textvariable=self.v_host, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6)
        host_entry.pack(fill="x")

        _section_label(self, "CONSOLE TOKEN")
        tf = _field(self)
        self.v_token = tk.StringVar()
        token_entry = tk.Entry(tf, textvariable=self.v_token, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, show="●")
        token_entry.pack(fill="x")

        self._connect_status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._connect_status, bg=BG, fg=RED,
                 font=("Segoe UI",9), wraplength=370, justify="left"
        ).pack(anchor="w", padx=22, pady=(2,0))

        self._connect_btn = _btn(self, "Connect", self._attempt_connect, "primary",
             font=("Georgia",10,"bold"), pady=10)
        self._connect_btn.pack(fill="x", padx=20, pady=(10,16))

        host_entry.bind("<Return>", lambda e: token_entry.focus_set())
        token_entry.bind("<Return>", lambda e: self._attempt_connect())
        host_entry.focus_set()

        self.update_idletasks()
        self.geometry(f"420x{self.winfo_reqheight()}")

    def _attempt_connect(self):
        host = self.v_host.get().strip()
        token = self.v_token.get().strip()
        if not host or not token:
            self._connect_status.set("Enter both the server IP and the console token.")
            return
        self._connect_btn.config(state="disabled", text="Connecting…")
        self._connect_status.set("")
        self._ws_client = WsConsoleClient()

        def worker():
            ok, msg = self._ws_client.connect(
                host, token,
                on_line=lambda t: self.after(0, lambda p=t: self._on_line(p)),
                on_disc=lambda r: self.after(0, lambda: self._on_disconnected(r)),
            )
            if ok:
                self._connected = True
                self.after(0, self._connect_succeeded)
            else:
                self.after(0, lambda m=msg: self._connect_failed(m))
        threading.Thread(target=worker, daemon=True).start()

    def _connect_failed(self, msg):
        self._connect_btn.config(state="normal", text="Connect")
        self._connect_status.set(msg)

    def _connect_succeeded(self):
        for child in list(self.winfo_children()):
            child.destroy()
        self.minsize(1, 1)
        self._build_main_ui()
        self._refresh_players()

    def _on_disconnected(self, msg):
        self._connected = False
        if hasattr(self, "_log_status"):
            self._log_status.set("Disconnected")
        if hasattr(self, "out"):
            self._append_log(f"\n[{msg}]\n", "err")

    def _on_line(self, text):
        """Called by WsConsoleClient for streaming output — display only."""
        self._append_log(text)

    # ── Sending ──────────────────────────────────────────────────────────────

    def _send(self, cmd):
        """Fire-and-forget — output arrives via _on_line callback."""
        if not self._connected:
            return
        self._append_log(f"> {cmd}\n", "cyan")
        self._ws_client.send(cmd)

    def _send_and_capture(self, cmd, on_result, quiet=0.4, max_wait=20.0):
        """Send a command and deliver (result_string, result_data) to on_result.
        Uses WsConsoleClient.send_capture — blocks in a worker thread,
        then calls on_result(result_string, result_data) on the Tk thread.
        quiet and max_wait are kept for API compatibility but max_wait
        is used as the capture timeout."""
        if not self._connected:
            self.after(0, lambda: on_result(None, None))
            return
        self._append_log(f"> {cmd}\n", "cyan")

        def worker():
            rs, rd, err = self._ws_client.send_capture(cmd, timeout=max_wait)
            self.after(0, lambda: on_result(rs, rd))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _q(value):
        """Quotes a value for a console command argument, matching the
        convention already used elsewhere in this project."""
        return f'"{value}"'

    # ── Main UI ──────────────────────────────────────────────────────────────

    def _build_main_ui(self):
        self.minsize(820, 820)
        self.geometry("860x900")
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🧩  TavernKeeper", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        self._log_status = tk.StringVar(value="Connected")
        tk.Label(h, textvariable=self._log_status, bg=SURF, fg=MUTED,
                 font=("Segoe UI",9)).pack(side="right", padx=16)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tf = tk.Frame(self, bg=BG)
        tf.pack(fill="x", padx=14, pady=(10,4))
        tk.Label(tf, text="Target player:", bg=BG, fg=PARCH,
                 font=("Segoe UI",9,"bold")).pack(side="left")
        self.v_target = tk.StringVar()
        self._target_combo = ttk.Combobox(tf, textvariable=self.v_target,
                                          font=("Consolas",10), width=22)
        self._target_combo.pack(side="left", padx=(8,6))
        _btn(tf, "⟳ Refresh", self._refresh_players,
             font=("Segoe UI",8), pady=3, padx=8).pack(side="left")
        tk.Label(tf, text="Actions below happen at this player's location.",
                 bg=BG, fg=MUTED, font=("Segoe UI",8)
        ).pack(side="left", padx=(10,0))

        style = ttk.Style()
        style.configure("Tavk.TNotebook", background=BG, borderwidth=0)
        style.configure("Tavk.TNotebook.Tab", background=SURF2, foreground=PARCH,
                        padding=(12,6), font=("Georgia",9))
        style.map("Tavk.TNotebook.Tab",
                  background=[("selected",AMBERDIM)],
                  foreground=[("selected","#ffd080")])

        nb = ttk.Notebook(self, style="Tavk.TNotebook")
        nb.pack(fill="both", expand=True, padx=12, pady=(6,6))
        spawn_tab    = tk.Frame(nb, bg=BG)
        select_tab   = tk.Frame(nb, bg=BG)
        move_tab     = tk.Frame(nb, bg=BG)
        settings_tab = tk.Frame(nb, bg=BG)
        saveload_tab = tk.Frame(nb, bg=BG)
        admin_tab    = tk.Frame(nb, bg=BG)
        nb.add(spawn_tab,    text="  Spawn  ")
        nb.add(select_tab,   text="  Find & Select  ")
        nb.add(move_tab,     text="  Move & Rotate  ")
        nb.add(settings_tab, text="  Server Settings  ")
        nb.add(saveload_tab, text="  Save & Load  ")
        nb.add(admin_tab,    text="  Player Admin  ")
        self._build_spawn_tab(spawn_tab)
        self._build_select_tab(select_tab)
        self._build_move_tab(move_tab)
        self._build_settings_tab(settings_tab)
        self._build_saveload_tab(saveload_tab)
        self._build_admin_tab(admin_tab)

        _section_label(self, "CONSOLE OUTPUT")
        lf = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lf.pack(fill="x", padx=12, pady=(0,12))
        self.out = tk.Text(lf, bg=SURF, fg="#b09a78", font=MONO, height=6,
                           relief="flat", bd=0, state="disabled", wrap="word")
        sb = _mk_scrollbar(lf, self.out.yview)
        sb.pack(side="right", fill="y")
        self.out.config(yscrollcommand=sb.set)
        self.out.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        for t,c in [("ok",GREEN),("warn",AMBER),("err",RED),("cyan",CYAN)]:
            self.out.tag_config(t, foreground=c)

    def _append_log(self, text, tag=""):
        self.out.config(state="normal")
        self.out.insert("end", text, tag)
        self.out.see("end")
        self.out.config(state="disabled")

    def _refresh_players(self):
        def on_result(rs, rd):
            names = []
            if rd and isinstance(rd, list):
                for item in rd:
                    if isinstance(item, dict):
                        name = item.get("Username") or item.get("username")
                        if name and name not in names:
                            names.append(str(name))
            elif rs and not rs.startswith("System."):
                for line in rs.splitlines():
                    line = line.strip()
                    if not line or "UserID" in line or line.startswith("-"):
                        continue
                    if line.startswith("[CommandService") or line == "Success":
                        continue
                    if line.startswith("System."):
                        continue
                    if " (" in line:
                        name = line.split(" (")[0].strip()
                    else:
                        name = line.split()[0].strip()
                    if name and name not in names:
                        names.append(name)
            # Always update — empty list clears the combo cleanly
            self._target_combo["values"] = names
            if names:
                if not self.v_target.get() or self.v_target.get() not in names:
                    self.v_target.set(names[0])
            else:
                self.v_target.set("")
        self._send_and_capture("player list", on_result)

    def _current_target(self):
        target = self.v_target.get().strip()
        if not target:
            messagebox.showinfo("Pick a target player",
                "Choose or type a target player first.", parent=self)
            return None
        return target

    def _bind_prefab_autocomplete(self, entry, target_var):
        """As-you-type suggestions for a prefab field, matching name or
        hash against the cached prefab list — reads from the same disk
        cache Browse Prefabs uses, without triggering a fetch of its own,
        so typing never causes a surprise network call."""
        ac_frame = tk.Frame(self, bg=SURF2, highlightbackground=BORDER, highlightthickness=1)
        ac_listbox = tk.Listbox(ac_frame, bg=SURF2, fg=PARCH,
                                selectbackground=AMBERDIM, selectforeground="#ffd080",
                                relief="flat", bd=0, highlightthickness=0,
                                font=("Consolas",10), activestyle="none")
        ac_scrollbar = _mk_scrollbar(ac_frame, ac_listbox.yview)
        ac_scrollbar.pack(side="right", fill="y")
        ac_listbox.config(yscrollcommand=ac_scrollbar.set)
        ac_listbox.pack(side="left", fill="both", expand=True)
        state = {"visible": False, "matches": []}

        def hide():
            if state["visible"]:
                ac_frame.place_forget()
                state["visible"] = False

        def show(matches):
            state["matches"] = matches
            ac_listbox.delete(0, "end")
            for h, n in matches:
                ac_listbox.insert("end", f"{n}   [{h}]")
            ac_listbox.selection_clear(0, "end")
            ac_listbox.selection_set(0)
            ac_listbox.config(height=min(6, len(matches)))
            ac_frame.place(in_=entry, x=0, rely=1.0, anchor="nw", width=entry.winfo_width())
            ac_frame.lift()
            state["visible"] = True

        def update_suggestions(event=None):
            if event is not None and event.keysym in ("Down","Up","Tab","Return","Escape"):
                return
            text = target_var.get().strip().lower()
            if not text:
                hide()
                return
            if self._prefab_list is None:
                cached = self._load_prefabs_cache()
                if not cached:
                    return  # nothing to suggest from yet, and typing shouldn't trigger a fetch
                self._prefab_list = cached
            matches = [(h,n) for h,n in self._prefab_list
                       if text in n.lower() or text == str(h)][:50]
            if matches and not (len(matches) == 1 and str(matches[0][0]) == text):
                show(matches)
            else:
                hide()

        def accept_selected(event=None):
            if not state["visible"]:
                return None
            sel = ac_listbox.curselection()
            idx = sel[0] if sel else 0
            if 0 <= idx < len(state["matches"]):
                target_var.set(str(state["matches"][idx][0]))
            hide()
            return "break"

        def move_selection(delta):
            if not state["visible"]:
                return
            sel = ac_listbox.curselection()
            idx = sel[0] if sel else 0
            idx = max(0, min(len(state["matches"])-1, idx+delta))
            ac_listbox.selection_clear(0, "end")
            ac_listbox.selection_set(idx)
            ac_listbox.see(idx)

        entry.bind("<KeyRelease>", update_suggestions)
        entry.bind("<Tab>", accept_selected)
        entry.bind("<Return>", accept_selected)
        entry.bind("<Down>", lambda e: (move_selection(1), "break")[1])
        entry.bind("<Up>", lambda e: (move_selection(-1), "break")[1])
        entry.bind("<Escape>", lambda e: hide())
        ac_listbox.bind("<ButtonRelease-1>", lambda e: (accept_selected(), entry.focus_set()))

    # ── Spawn tab ────────────────────────────────────────────────────────────

    def _build_spawn_tab(self, parent):
        tk.Label(parent, text="Spawns a prefab at the target player's location.",
                 bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=8, pady=(10,6))

        _section_label(parent, "PREFAB")
        pf = _field(parent)
        self.v_spawn_prefab = tk.StringVar()
        row = tk.Frame(pf, bg=SURF)
        row.pack(fill="x")
        spawn_prefab_entry = tk.Entry(row, textvariable=self.v_spawn_prefab, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6)
        spawn_prefab_entry.pack(side="left", fill="x", expand=True)
        self._bind_prefab_autocomplete(spawn_prefab_entry, self.v_spawn_prefab)
        _btn(parent, "Browse Prefabs…", lambda: self._open_prefab_picker(self.v_spawn_prefab), "primary",
             font=("Segoe UI",9,"bold"), pady=6).pack(fill="x", padx=8, pady=(4,10))

        _section_label(parent, "ARGUMENTS  (optional)")
        af = _field(parent)
        self.v_spawn_args = tk.StringVar()
        tk.Entry(af, textvariable=self.v_spawn_args, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6).pack(fill="x")

        _btn(parent, "Spawn at Target Player", self._on_spawn, "primary",
             font=("Georgia",10,"bold"), pady=10).pack(fill="x", padx=8, pady=(8,8))

    def _on_spawn(self):
        target = self._current_target()
        if not target:
            return
        prefab = self.v_spawn_prefab.get().strip()
        if not prefab:
            messagebox.showinfo("Enter a prefab", "Pick or type a prefab first.", parent=self)
            return
        args = self.v_spawn_args.get().strip()
        cmd = f"spawn {self._q(target)} {self._q(prefab)}"
        if args:
            cmd += f" {args}"
        self._send(cmd)

    def _prefabs_cache_path(self):
        return os.path.join(_tavern_data_dir(), "prefabs.json")

    def _load_prefabs_cache(self):
        try:
            with open(self._prefabs_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return [(int(h), n) for h, n in data]
        except Exception:
            return None

    def _save_prefabs_cache(self, prefab_list):
        try:
            with open(self._prefabs_cache_path(), "w", encoding="utf-8") as f:
                json.dump(prefab_list, f)
        except Exception:
            pass  # a failed cache write isn't worth interrupting anything over

    def _open_prefab_picker(self, target_var):
        if self._prefab_list is not None:
            self._show_prefab_picker(target_var)
            return
        cached = self._load_prefabs_cache()
        if cached:
            self._prefab_list = cached
            self._show_prefab_picker(target_var)
            return
        self._fetch_prefab_list(target_var)

    def _fetch_prefab_list(self, target_var):
        self._append_log("Fetching prefab list…\n")
        def on_result(rs, rd):
            prefabs = []
            # rd is a list of {Hash: int, Name: str} objects from spawn list
            if rd and isinstance(rd, list):
                for item in rd:
                    if isinstance(item, dict):
                        h = item.get("Hash") or item.get("hash")
                        n = item.get("Name") or item.get("name") or ""
                        if h is not None:
                            prefabs.append((int(h), str(n)))
            elif rs:
                # Fall back to regex on ResultString
                matches = re.findall(r'\{"Hash":\s*(-?\d+),\s*"Name":\s*"([^"]*)"\}', rs)
                prefabs = [(int(h), n) for h, n in matches]
            if not prefabs:
                messagebox.showinfo("No prefabs found",
                    "The server didn't return a prefab list.", parent=self)
                return
            self._prefab_list = prefabs
            self._save_prefabs_cache(self._prefab_list)
            self._show_prefab_picker(target_var)
        self._send_and_capture("spawn list", on_result, quiet=1.5, max_wait=60.0)
    def _show_prefab_picker(self, target_var):
        win = tk.Toplevel(self)
        win.title("Browse Prefabs")
        win.configure(bg=BG)
        _set_window_icon(win)
        _enable_dark_titlebar(win)
        win.geometry("480x600")
        win.minsize(420, 500)

        h = tk.Frame(win, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="Browse Prefabs", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

        sf = tk.Frame(win, bg=BG)
        sf.pack(fill="x", padx=12, pady=(10,4))
        tk.Label(sf, text="Search:", bg=BG, fg=PARCH, font=("Segoe UI",9)).pack(side="left")
        v_search = tk.StringVar()
        search_entry = tk.Entry(sf, textvariable=v_search, bg=SURF, fg=PARCH,
                                insertbackground=AMBER, relief="flat",
                                font=("Consolas",10), bd=6)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6,0))
        search_entry.focus_set()
        _btn(sf, "⟳", lambda: (win.destroy(), self._fetch_prefab_list(target_var)),
             font=("Segoe UI",9), pady=4, padx=8).pack(side="left", padx=(6,0))

        count_var = tk.StringVar()
        tk.Label(win, textvariable=count_var, bg=BG, fg=MUTED,
                 font=("Segoe UI",8)).pack(anchor="w", padx=14, pady=(2,4))

        lf = tk.Frame(win, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lf.pack(fill="both", expand=True, padx=12, pady=(0,10))
        listbox = tk.Listbox(lf, bg=SURF, fg=PARCH, selectbackground=AMBERDIM,
                             selectforeground="#ffd080", relief="flat", bd=0,
                             font=("Consolas",10), activestyle="none")
        sb = _mk_scrollbar(lf, listbox.yview)
        sb.pack(side="right", fill="y")
        listbox.config(yscrollcommand=sb.set)
        listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        state = {"filtered": list(self._prefab_list)}

        def refresh_list(*_):
            query = v_search.get().strip().lower()
            if query:
                state["filtered"] = [(h,n) for h,n in self._prefab_list
                                      if query in n.lower() or query == str(h)]
            else:
                state["filtered"] = list(self._prefab_list)
            listbox.delete(0, "end")
            shown = state["filtered"][:500]
            for h, n in shown:
                listbox.insert("end", f"{n}   [{h}]")
            extra = f" (showing first 500)" if len(state["filtered"]) > 500 else ""
            count_var.set(f"{len(state['filtered'])} result(s){extra}")

        v_search.trace_add("write", refresh_list)
        refresh_list()

        def use_selected(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            h, n = state["filtered"][sel[0]]
            target_var.set(str(h))
            win.destroy()

        listbox.bind("<Double-Button-1>", use_selected)
        _btn(win, "Use Selected", use_selected, "primary",
             font=("Georgia",10,"bold"), pady=8).pack(fill="x", padx=12, pady=(0,12))

    # ── Find & Select tab ───────────────────────────────────────────────────

    def _build_select_tab(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=8, pady=(10,6))
        tk.Label(top, text="Diameter:", bg=BG, fg=PARCH,
                 font=("Segoe UI",9)).pack(side="left")
        self.v_radius = tk.StringVar(value="10")
        tk.Entry(top, textvariable=self.v_radius, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=4, width=6).pack(side="left", padx=(6,10))
        _btn(top, "🔍 Find Nearby", self._on_find_nearby, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=12).pack(side="left")
        tk.Label(top, text="around the target player",
                 bg=BG, fg=MUTED, font=("Segoe UI",8)).pack(side="left", padx=(8,0))

        # Groups row
        gf = tk.Frame(parent, bg=BG)
        gf.pack(fill="x", padx=8, pady=(0,6))
        tk.Label(gf, text="Group:", bg=BG, fg=PARCH,
                 font=("Segoe UI",9)).pack(side="left")
        self._group_combo_var = tk.StringVar(value="(none)")
        self._group_combo = ttk.Combobox(gf, textvariable=self._group_combo_var,
                                          font=("Consolas",9), width=18, state="readonly")
        self._group_combo["values"] = ["(none)"]
        self._group_combo.pack(side="left", padx=(4,6))
        self._group_combo.bind("<<ComboboxSelected>>", self._on_group_select)
        _btn(gf, "✕ Delete Group", self._on_group_delete,
             font=("Segoe UI",8), pady=3, padx=6).pack(side="left", padx=(0,12))
        self.v_group_name = tk.StringVar()
        tk.Entry(gf, textvariable=self.v_group_name, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",9),
                 bd=4, width=14).pack(side="left", padx=(0,4))
        _btn(gf, "Save Checked as Group", self._on_group_save, "primary",
             font=("Segoe UI",8,"bold"), pady=3, padx=6).pack(side="left")

        _section_label(parent, "NEARBY  (✓ = include in group)")
        lb = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True, padx=8, pady=(0,6))
        # Scrollable checkbox list via Canvas
        self._nearby_canvas = tk.Canvas(lb, bg=SURF, highlightthickness=0)
        sb2 = _mk_scrollbar(lb, self._nearby_canvas.yview)
        sb2.pack(side="right", fill="y")
        self._nearby_canvas.config(yscrollcommand=sb2.set)
        self._nearby_canvas.pack(side="left", fill="both", expand=True)
        self._nearby_inner = tk.Frame(self._nearby_canvas, bg=SURF)
        self._nearby_canvas_window = self._nearby_canvas.create_window(
            (0, 0), window=self._nearby_inner, anchor="nw")
        self._nearby_inner.bind("<Configure>", lambda e: (
            self._nearby_canvas.configure(scrollregion=self._nearby_canvas.bbox("all")),
            self._nearby_canvas.itemconfig(self._nearby_canvas_window,
                                            width=self._nearby_canvas.winfo_width())
        ))
        self._nearby_canvas.bind("<Configure>", lambda e:
            self._nearby_canvas.itemconfig(self._nearby_canvas_window, width=e.width))

        def _nearby_mousewheel(event):
            self._nearby_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._nearby_canvas.bind("<MouseWheel>", _nearby_mousewheel)
        self._nearby_inner.bind("<MouseWheel>", _nearby_mousewheel)
        # Store reference so _populate_nearby can rebind new rows
        self._nearby_mousewheel = _nearby_mousewheel

        # Storage for nearby items: list of (entity_id, name, BooleanVar)
        self._nearby_items = []
        # Saved groups: {name: [(id, name), ...]}
        self._prefab_groups = {}
        # Keep a reference to the listbox for legacy code compatibility
        self._nearby_listbox = None

        _section_label(parent, "SELECT BY PREFAB  (nearest to target player)")
        spf = _field(parent)
        self.v_select_prefab = tk.StringVar()
        row_sp = tk.Frame(spf, bg=SURF)
        row_sp.pack(fill="x")
        select_prefab_entry = tk.Entry(row_sp, textvariable=self.v_select_prefab, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6)
        select_prefab_entry.pack(side="left", fill="x", expand=True)
        self._bind_prefab_autocomplete(select_prefab_entry, self.v_select_prefab)
        _btn(parent, "Browse Prefabs…", lambda: self._open_prefab_picker(self.v_select_prefab), "primary",
             font=("Segoe UI",9,"bold"), pady=6).pack(fill="x", padx=8, pady=(4,4))
        _btn(parent, "Select Nearest", self._on_select_prefab, "primary",
             font=("Segoe UI",9,"bold"), pady=6).pack(fill="x", padx=8, pady=(0,10))

        _section_label(parent, "CURRENT SELECTION")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=8, pady=(0,12))
        _btn(row, "Get String", self._on_get_string,
             font=("Segoe UI",9), pady=7, padx=12).pack(side="left")
        _btn(row, "Snap to Ground", lambda: self._send("select snap-ground"),
             font=("Segoe UI",9), pady=7, padx=12).pack(side="left", padx=6)
        _btn(row, "Look At Target", self._on_look_at,
             font=("Segoe UI",9), pady=7, padx=12).pack(side="left", padx=(0,6))
        _btn(row, "Unselect", lambda: self._send("select unselect"),
             font=("Segoe UI",9), pady=7, padx=12).pack(side="left")
        _btn(row, "✕ Destroy", self._on_destroy_selection, "danger",
             font=("Segoe UI",9,"bold"), pady=7, padx=12).pack(side="right")

    def _populate_nearby(self, items):
        """Rebuild the checkbox list from items = [(id, name), ...]."""
        for widget in self._nearby_inner.winfo_children():
            widget.destroy()
        self._nearby_items = []
        if not items:
            tk.Label(self._nearby_inner, text="(nothing found)", bg=SURF, fg=MUTED,
                     font=("Consolas",9)).pack(anchor="w", padx=6, pady=4)
            return
        for eid, name in items:
            var = tk.BooleanVar(value=False)
            row = tk.Frame(self._nearby_inner, bg=SURF)
            row.pack(fill="x")
            # Checkbutton: light indicator colour so it's visible on dark bg
            cb = tk.Checkbutton(row, variable=var, bg=SURF,
                                 activebackground=SURF,
                                 selectcolor=SURF,
                                 fg=PARCH,
                                 relief="flat", bd=0)
            cb.pack(side="left")
            label_text = f"{str(eid):<12} {name}"
            lbl = tk.Label(row, text=label_text, bg=SURF, fg=PARCH,
                           font=("Consolas",9), cursor="hand2", anchor="w")
            lbl.pack(side="left", fill="x", expand=True)

            def _refresh_row_bg(row=row, var=var):
                """Colour the whole row amber when checked, normal when not."""
                checked = var.get()
                bg = AMBERDIM if checked else SURF
                row.config(bg=bg)
                for c in row.winfo_children():
                    try: c.config(bg=bg)
                    except Exception: pass

            # Checkbox toggle → update row background
            cb.config(command=_refresh_row_bg)

            # Click label → select entity on server AND highlight row
            def on_click(e, eid=eid, row=row):
                # Reset all rows to their checked/unchecked colour
                for r_eid, r_name, r_var in self._nearby_items:
                    pass  # _refresh done below
                # Re-apply checked colours for all rows
                for child in self._nearby_inner.winfo_children():
                    child_bg = SURF
                    for r_eid, r_name, r_var in self._nearby_items:
                        # match row by iterating winfo_children order
                        pass
                # Simpler: just re-run refresh on all items
                for idx, (r_eid, r_name, r_var) in enumerate(self._nearby_items):
                    rows = self._nearby_inner.winfo_children()
                    if idx < len(rows):
                        bg = AMBERDIM if r_var.get() else SURF
                        rows[idx].config(bg=bg)
                        for c in rows[idx].winfo_children():
                            try: c.config(bg=bg)
                            except Exception: pass
                # Always highlight this specific row orange (selected)
                row.config(bg=AMBERDIM)
                for c in row.winfo_children():
                    try: c.config(bg=AMBERDIM)
                    except Exception: pass
                self._send(f"select {eid}")
            lbl.bind("<Button-1>", on_click)
            # Mousewheel on each row and its children
            mwh = getattr(self, "_nearby_mousewheel", None)
            if mwh:
                row.bind("<MouseWheel>", mwh)
                cb.bind("<MouseWheel>", mwh)
                lbl.bind("<MouseWheel>", mwh)
            self._nearby_items.append((eid, name, var))
        self._nearby_canvas.update_idletasks()
        self._nearby_canvas.configure(scrollregion=self._nearby_canvas.bbox("all"))

    def _on_find_nearby(self):
        target = self._current_target()
        if not target:
            return
        try:
            diameter = float(self.v_radius.get().strip())
        except ValueError:
            messagebox.showinfo("Invalid value", "Enter a number for the search diameter.", parent=self)
            return
        for widget in self._nearby_inner.winfo_children():
            widget.destroy()
        tk.Label(self._nearby_inner, text="Searching…", bg=SURF, fg=MUTED,
                 font=("Consolas",9)).pack(anchor="w", padx=6, pady=4)
        def on_result(rs, rd):
            items = []
            if rd and isinstance(rd, list):
                for item in rd:
                    if isinstance(item, dict):
                        eid  = item.get("Identifier") or item.get("identifier", "")
                        name = item.get("Name") or item.get("name") or item.get("OriginalName") or ""
                        if eid != "":
                            items.append((eid, name))
            elif rs:
                for line in rs.splitlines():
                    line = line.strip()
                    if not line or "Name" in line or line.startswith("-"):
                        continue
                    if line.startswith("[CommandService") or line == "Success":
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        try:
                            items.append((int(parts[0]), parts[1]))
                        except ValueError:
                            pass
            self._populate_nearby(items)
        self._send_and_capture(f"select find {self._q(target)} {diameter}", on_result, quiet=0.6)

    def _on_group_save(self):
        name = self.v_group_name.get().strip()
        if not name:
            messagebox.showinfo("Enter a name", "Type a group name first.", parent=self)
            return
        checked = [(eid, n) for eid, n, var in self._nearby_items if var.get()]
        if not checked:
            messagebox.showinfo("Nothing checked",
                "Check at least one item in the Nearby list first.", parent=self)
            return
        self._prefab_groups[name] = checked
        vals = ["(none)"] + list(self._prefab_groups.keys())
        self._group_combo["values"] = vals
        self._group_combo_var.set(name)
        self.v_group_name.set("")
        self._append_log(f"[Group '{name}' saved: {len(checked)} item(s)]\n", "ok")

    def _on_group_select(self, event=None):
        name = self._group_combo_var.get()
        if name == "(none)" or name not in self._prefab_groups:
            return
        items = self._prefab_groups[name]
        self._populate_nearby(items)
        self._append_log(f"[Group '{name}' loaded: {len(items)} item(s)]\n", "ok")

    def _on_group_delete(self):
        name = self._group_combo_var.get()
        if name == "(none)" or name not in self._prefab_groups:
            return
        del self._prefab_groups[name]
        vals = ["(none)"] + list(self._prefab_groups.keys())
        self._group_combo["values"] = vals
        self._group_combo_var.set("(none)")
        self._append_log(f"[Group '{name}' deleted]\n", "warn")

    def _on_destroy_selection(self):
        self._send("select destroy")
        # Refresh nearby list so the destroyed object disappears
        self.after(400, self._on_find_nearby)

    def _on_select_prefab(self):
        target = self._current_target()
        if not target:
            return
        prefab = self.v_select_prefab.get().strip()
        if not prefab:
            messagebox.showinfo("Pick a prefab", "Browse for a prefab first.", parent=self)
            return
        self._send(f"select prefab {prefab} {self._q(target)}")

    def _on_get_string(self):
        def on_result(rs, rd):
            if rs:
                self._append_log(f"[Selection string]\n{rs}\n", "ok")
        self._send_and_capture("select tostring", on_result)

    def _on_look_at(self):
        target = self._current_target()
        if not target:
            return
        self._send(f"select look-at {self._q(target)}")

    # ── Move & Rotate tab ───────────────────────────────────────────────────

    def _build_move_tab(self, parent):
        tk.Label(parent, text="Acts on whatever is currently selected in Find & Select.",
                 bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=8, pady=(10,12))

        outer = tk.Frame(parent, bg=BG)
        outer.pack()

        move_box = tk.Frame(outer, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        move_box.pack(side="left", padx=(0,20), pady=4, ipadx=12, ipady=12)
        mrow = tk.Frame(move_box, bg=SURF)
        mrow.pack()
        tk.Label(mrow, text="Move amount", bg=SURF, fg=PARCH,
                 font=("Segoe UI",9,"bold")).grid(row=0, column=0, columnspan=3, pady=(0,6))
        self.v_move_amount = tk.StringVar(value="0.5")
        tk.Entry(mrow, textvariable=self.v_move_amount, bg=SURF2, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",11),
                 bd=6, width=6, justify="center").grid(row=0, column=3, pady=(0,6))

        moves = [("↑","forward",1,1),("←","left",2,0),("→","right",2,2),
                 ("↓","back",3,1),("Up","up",1,3),("Down","down",3,3)]
        for label, direction, r, c in moves:
            _btn(mrow, label, lambda d=direction: self._on_move(d), "primary",
                 font=("Segoe UI",11,"bold"), width=5, pady=10).grid(row=r, column=c, padx=5, pady=5)

        rotate_box = tk.Frame(outer, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        rotate_box.pack(side="left", pady=4, ipadx=12, ipady=12)
        rrow = tk.Frame(rotate_box, bg=SURF)
        rrow.pack()
        tk.Label(rrow, text="Rotate degrees", bg=SURF, fg=PARCH,
                 font=("Segoe UI",9,"bold")).grid(row=0, column=0, columnspan=2, pady=(0,6))
        self.v_rotate_degrees = tk.StringVar(value="15")
        tk.Entry(rrow, textvariable=self.v_rotate_degrees, bg=SURF2, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",11),
                 bd=6, width=6, justify="center").grid(row=0, column=2, pady=(0,6))

        rotations = [("Pitch −","pitch",-1),("Pitch +","pitch",1),
                     ("Roll −","roll",-1),("Roll +","roll",1),
                     ("Yaw −","yaw",-1),("Yaw +","yaw",1)]
        for i, (label, axis, sign) in enumerate(rotations):
            _btn(rrow, label, lambda a=axis, s=sign: self._on_rotate(a, s), "primary",
                 font=("Segoe UI",9,"bold"), pady=9, padx=8).grid(row=1+i//2, column=i%2, padx=5, pady=5)

        # ── Scale ──────────────────────────────────────────────────────────
        _section_label(parent, "SCALE")
        scale_hint = tk.Label(parent, text="Sets the uniform scale of the selected object (1.0 = normal).",
                 bg=BG, fg=MUTED, font=("Segoe UI",8), wraplength=680, justify="left")
        scale_hint.pack(anchor="w", padx=10, pady=(0,6))
        sf = tk.Frame(parent, bg=BG)
        sf.pack(padx=8, pady=(0,10))
        _btn(sf, "−", lambda: self._scale_step(-0.25), "primary",
             font=("Segoe UI",12,"bold"), width=3, pady=6).pack(side="left")
        self.v_scale = tk.StringVar(value="1.0")
        scale_entry = tk.Entry(sf, textvariable=self.v_scale, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",11),
                 bd=6, width=7, justify="center")
        scale_entry.pack(side="left", padx=6)
        scale_entry.bind("<Return>", lambda e: self._on_set_scale())
        _btn(sf, "+", lambda: self._scale_step(0.25), "primary",
             font=("Segoe UI",12,"bold"), width=3, pady=6).pack(side="left")
        _btn(sf, "Set Scale", self._on_set_scale, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=10).pack(side="left", padx=(12,0))

    def _scale_step(self, delta):
        try:
            v = round(float(self.v_scale.get().strip()) + delta, 4)
        except ValueError:
            v = 1.0
        v = max(0.01, min(10.23, v))
        self.v_scale.set(f"{v:.2f}")

    def _on_set_scale(self):
        try:
            scale = float(self.v_scale.get().strip())
        except ValueError:
            messagebox.showinfo("Invalid scale", "Enter a number for the scale.", parent=self)
            return
        scale = max(0.01, min(10.23, scale))
        self.v_scale.set(f"{scale:.4f}".rstrip("0").rstrip("."))
        ids = self._active_entity_ids()
        if ids:
            self._rescale_entity_list(ids, 0, scale)
        else:
            self._rescale_single(scale)

    def _rescale_single(self, scale):
        """Rescale whatever is currently selected on the server."""
        def on_got_string(rs, rd):
            if not rs or rs.startswith("System."):
                messagebox.showinfo("No selection", "Select an object first.", parent=self)
                return
            rescaled = self._rescale_spawn_string(rs, scale)
            if rescaled is None:
                messagebox.showinfo("Scale failed",
                    "Couldn't parse the spawn string for rescaling.", parent=self)
                return
            target = self._current_target()
            if not target:
                return
            self._send("select destroy")
            self._send(f"spawn string {self._q(target)} {self._q(rescaled)}")
        self._send_and_capture("select tostring", on_got_string)

    def _rescale_entity_list(self, ids, i, scale):
        """Recursively rescale each entity in the list."""
        if i >= len(ids):
            return
        self._send(f"select {ids[i]}")
        def on_got_string(rs, rd):
            if rs and not rs.startswith("System."):
                rescaled = self._rescale_spawn_string(rs, scale)
                if rescaled:
                    target = self._current_target()
                    if target:
                        self._send("select destroy")
                        self._send(f"spawn string {self._q(target)} {self._q(rescaled)}")
            self.after(200, lambda: self._rescale_entity_list(ids, i + 1, scale))
        self.after(80, lambda: self._send_and_capture("select tostring", on_got_string))

    @staticmethod
    def _rescale_spawn_string(spawn_string, scale):
        """
        Rewrite the scale in an ATT spawn string.
        Format: "w0,w1,...,w10,...[|part2|...]"
        Word index 10 in the first pipe-section is the scale stored as the
        raw uint32 bit-representation of a float32 (big-endian).
        Matches Prefabulator's rescaleString() exactly.
        """
        import struct
        try:
            scale = max(0.01, min(10.23, scale))
            parts = spawn_string.split("|")
            words = parts[0].split(",")
            bits = struct.unpack(">I", struct.pack(">f", scale))[0]
            words[10] = str(bits)
            parts[0] = ",".join(words)
            return "|".join(parts)
        except Exception:
            return None

    def _active_entity_ids(self):
        """Return list of entity IDs to act on.
        Priority:
        1. A saved group is selected in the dropdown → use that group's IDs
        2. Any checkboxes are ticked in the nearby list → use those IDs
        3. Neither → return None (act on current single server selection)
        """
        name = self._group_combo_var.get()
        if name != "(none)" and name in self._prefab_groups:
            return [eid for eid, _ in self._prefab_groups[name]]
        checked = [eid for eid, n, var in self._nearby_items if var.get()]
        if checked:
            return checked
        return None

    def _send_to_selection(self, cmd):
        """Send a command, repeating it for each entity in the active group
        (selecting each in turn), or just once for single selection."""
        ids = self._active_entity_ids()
        if ids:
            def send_next(i=0):
                if i >= len(ids):
                    return
                self._send(f"select {ids[i]}")
                self.after(80, lambda: (self._send(cmd), self.after(80, lambda: send_next(i+1))))
            send_next()
        else:
            self._send(cmd)

    def _on_move(self, direction):
        try:
            amount = float(self.v_move_amount.get().strip())
        except ValueError:
            messagebox.showinfo("Invalid amount", "Enter a number for the move amount.", parent=self)
            return
        self._send_to_selection(f"select move {direction} {amount}")

    def _on_rotate(self, axis, sign):
        try:
            degrees = float(self.v_rotate_degrees.get().strip()) * sign
        except ValueError:
            messagebox.showinfo("Invalid amount", "Enter a number for the rotate degrees.", parent=self)
            return
        self._send_to_selection(f"select rotate {axis} {degrees}")

    # ── Server Settings tab ─────────────────────────────────────────────────

    # The exact settings Prefabulator's own Server Settings tab exposed —
    # (label, setting name, default shown to the user, is this a toggle).
    _SERVER_SETTINGS_FIELDS = [
        ("Drop all on death",                    "DropAllOnDeath",            True),
        ("Seconds before respawn",                "RespawnTimeSeconds",        False),
        ("Time speed multiplier",                 "TimeSpeedMultiplier",       False),
        ("Experience multiplier",                 "XPX",                       False),
        ("Global damage multiplier",              "DamageX",                   False),
        ("PVP damage enabled",                    "IsPVPEnabled",              True),
        ("PVP damage multiplier",                 "PVPMultiplier",             False),
        ("PVP cripple multiplier",                "PVPCrippleMultiplier",      False),
        ("Hunger deals damage",                   "HungerDealsDamage",         True),
        ("Hunger tick rate",                      "HungerTick",                False),
        ("Community storage multiplier",          "CommunityStorageMultiplier",False),
    ]

    def _build_settings_tab(self, parent):
        canvas_frame = tk.Frame(parent, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vsb = _mk_scrollbar(canvas_frame, canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _settings_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _settings_mousewheel)
        inner.bind("<MouseWheel>", _settings_mousewheel)

        _section_label(inner, "SERVER CLOCK")
        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", padx=8, pady=(0,4))
        self.v_time_value = tk.StringVar(value="12:00")
        tk.Entry(row, textvariable=self.v_time_value, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=4, width=8).pack(side="left")
        _btn(row, "Set Time", self._on_time_set, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=10).pack(side="left", padx=(6,6))
        _btn(row, "Toggle Day/Night", lambda: self._send("time toggle"),
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left")
        _hint(inner, "24-hour time, e.g. 14:30 for 2:30pm.")

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=12)
        _section_label(inner, "SERVER SETTINGS")
        _hint(inner, "Values apply immediately but are not read back from the server.")

        self._settings_vars = {}
        for label, name, is_toggle in self._SERVER_SETTINGS_FIELDS:
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", padx=8, pady=4)
            tk.Label(row, text=label, bg=BG, fg=PARCH, font=("Segoe UI",9),
                     width=34, anchor="w").pack(side="left")
            if is_toggle:
                _btn(row, "On", lambda n=name: self._on_settings_apply(n, "true"),
                     font=("Segoe UI",9,"bold"), pady=4, padx=10).pack(side="left")
                _btn(row, "Off", lambda n=name: self._on_settings_apply(n, "false"),
                     font=("Segoe UI",9), pady=4, padx=10).pack(side="left", padx=(6,0))
            else:
                v = tk.StringVar(value="")
                self._settings_vars[name] = v
                tk.Entry(row, textvariable=v, bg=SURF, fg=PARCH,
                         insertbackground=AMBER, relief="flat", font=("Consolas",10),
                         bd=4, width=10).pack(side="left")
                _btn(row, "Set", lambda n=name, var=v: self._on_settings_apply(n, var.get().strip()),
                     "primary", font=("Segoe UI",9,"bold"), pady=4, padx=10).pack(side="left", padx=(6,0))

    def _on_time_set(self):
        raw = self.v_time_value.get().strip()
        # Matches the same HH:MM -> HH.MM conversion Prefabulator itself
        # used for its server clock field.
        value = raw.replace(":", ".", 1)
        self._send(f"time set {value}")

    def _on_settings_apply(self, name, value):
        if not value:
            messagebox.showinfo("Enter a value", f"Type a value for {name} first.", parent=self)
            return
        self._send(f"settings changesetting server {name} {value}")

    # ── Save & Load tab ──────────────────────────────────────────────────────

    def _build_saveload_tab(self, parent):
        tk.Label(parent, text="Build a list of prefabs to save, then export it to a file you "
                              "can load again later — even on a different server.",
                 bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=8, pady=(10,8))

        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=8, pady=(0,6))
        _btn(row, "+ Add Current Selection", self._on_add_to_save_list, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=10).pack(side="left")
        _btn(row, "− Remove Selected", self._on_remove_from_save_list, "danger",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=6)

        _section_label(parent, "STAGED ITEMS")
        lb = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True, padx=8, pady=(0,8))
        self._save_listbox = tk.Listbox(lb, bg=SURF, fg=PARCH,
                                        selectbackground=AMBERDIM, selectforeground="#ffd080",
                                        relief="flat", bd=0, font=("Consolas",10),
                                        activestyle="none")
        sb3 = _mk_scrollbar(lb, self._save_listbox.yview)
        sb3.pack(side="right", fill="y")
        self._save_listbox.config(yscrollcommand=sb3.set)
        self._save_listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self._save_listbox.bind("<Double-Button-1>", self._on_rename_save_item)

        row2 = tk.Frame(parent, bg=BG)
        row2.pack(fill="x", padx=8, pady=(0,12))
        _btn(row2, "💾 Save to File…", self._on_save_to_file, "primary",
             font=("Segoe UI",9,"bold"), pady=7, padx=12).pack(side="left")
        _btn(row2, "📂 Load from File…", self._on_load_from_file,
             font=("Segoe UI",9), pady=7, padx=12).pack(side="left", padx=6)
        _btn(row2, "🪄 Spawn All", self._on_spawn_all_saved, "primary",
             font=("Segoe UI",9,"bold"), pady=7, padx=12).pack(side="right")

    @staticmethod
    def _clean_spawn_string(rs):
        """Extract the raw spawn string from a select tostring ResultString.
        The WS console returns the spawn string directly as ResultString.
        Strip whitespace and skip useless type-name noise."""
        if not rs:
            return None
        s = rs.strip()
        if not s or s.startswith("System."):
            return None
        return s

    def _on_add_to_save_list(self):
        ids = self._active_entity_ids()
        if ids:
            # Group is active — capture tostring for each entity in sequence
            self._add_group_to_save_list(ids, 0)
        else:
            # Single selection
            def on_result(rs, rd):
                spawn_string = TavernKeeperWindow._clean_spawn_string(rs) if rs else None
                if not spawn_string:
                    messagebox.showinfo("Nothing to add",
                        "Select something in Find & Select first.", parent=self)
                    return
                label = f"Item {len(self._save_items)+1}"
                self._save_items.append((label, spawn_string))
                self._save_listbox.insert("end", label)
            self._send_and_capture("select tostring", on_result)

    def _add_group_to_save_list(self, ids, i):
        """Recursively select each group entity, capture its tostring, add to list."""
        if i >= len(ids):
            return
        eid = ids[i]
        self._send(f"select {eid}")
        def on_result(rs, rd):
            spawn_string = TavernKeeperWindow._clean_spawn_string(rs) if rs else None
            if spawn_string:
                label = f"Item {len(self._save_items)+1}"
                self._save_items.append((label, spawn_string))
                self._save_listbox.insert("end", label)
            # Move to next entity after a short delay
            self.after(150, lambda: self._add_group_to_save_list(ids, i + 1))
        self.after(80, lambda: self._send_and_capture("select tostring", on_result))

    def _on_rename_save_item(self, event=None):
        sel = self._save_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        old_label, spawn_string = self._save_items[idx]
        new_label = simpledialog.askstring(
            "Rename item", "Enter a new name:",
            initialvalue=old_label, parent=self)
        if new_label and new_label.strip():
            new_label = new_label.strip()
            self._save_items[idx] = (new_label, spawn_string)
            self._save_listbox.delete(idx)
            self._save_listbox.insert(idx, new_label)
            self._save_listbox.selection_set(idx)

    def _on_remove_from_save_list(self):
        sel = self._save_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        del self._save_items[idx]
        self._save_listbox.delete(idx)

    def _on_save_to_file(self):
        if not self._save_items:
            messagebox.showinfo("Nothing staged",
                "Add at least one item to the list first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="Save prefab collection", defaultextension=".json",
            filetypes=[("JSON files","*.json"),("All files","*.*")], parent=self)
        if not path:
            return
        try:
            data = [{"label": label, "string": s} for label, s in self._save_items]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            messagebox.showinfo("Saved", f"Saved {len(data)} item(s).", parent=self)
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e), parent=self)

    def _on_load_from_file(self):
        path = filedialog.askopenfilename(
            title="Load prefab collection",
            filetypes=[("JSON files","*.json"),("All files","*.*")], parent=self)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._save_items = [(d.get("label","Item"), d["string"]) for d in data if "string" in d]
            self._save_listbox.delete(0, "end")
            for label, _ in self._save_items:
                self._save_listbox.insert("end", label)
        except Exception as e:
            messagebox.showerror("Couldn't load", str(e), parent=self)

    def _on_spawn_all_saved(self):
        if not self._save_items:
            messagebox.showinfo("Nothing staged",
                "Add or load some items first.", parent=self)
            return
        def spawn_next(i=0):
            if i >= len(self._save_items):
                return
            _, s = self._save_items[i]
            self._send(f"spawn string-raw {self._q(s)}")
            self.after(200, lambda: spawn_next(i+1))
        spawn_next()

    # ── Player Admin tab ─────────────────────────────────────────────────────

    def _build_admin_tab(self, parent):
        # Outer scrollable canvas so stats list doesn't overflow
        cf = tk.Frame(parent, bg=BG)
        cf.pack(fill="both", expand=True)
        canvas = tk.Canvas(cf, bg=BG, highlightthickness=0)
        vsb = _mk_scrollbar(cf, canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))

        def _admin_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<MouseWheel>", _admin_mousewheel)
        inner.bind("<MouseWheel>", _admin_mousewheel)

        tk.Label(inner, text="Acts on the target player selected above.",
                 bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=680, justify="left"
        ).pack(anchor="w", padx=8, pady=(10,10))

        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", padx=8, pady=(0,10))
        _btn(row, "Kick", self._on_kick, "primary",
             font=("Segoe UI",9,"bold"), pady=7, padx=16).pack(side="left")
        _btn(row, "Kill", self._on_kill, "danger",
             font=("Segoe UI",9,"bold"), pady=7, padx=16).pack(side="left", padx=6)

        _section_label(inner, "MESSAGE")
        mf = _field(inner)
        self.v_admin_message = tk.StringVar()
        tk.Entry(mf, textvariable=self.v_admin_message, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6).pack(fill="x")
        durf = tk.Frame(inner, bg=BG)
        durf.pack(fill="x", padx=8, pady=(0,4))
        tk.Label(durf, text="Duration (seconds):", bg=BG, fg=PARCH,
                 font=("Segoe UI",9)).pack(side="left")
        self.v_admin_duration = tk.StringVar(value="5")
        tk.Entry(durf, textvariable=self.v_admin_duration, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=4, width=6).pack(side="left", padx=(6,10))
        _btn(durf, "Send Message", self._on_message, "primary",
             font=("Segoe UI",9,"bold"), pady=6, padx=10).pack(side="left")
        _btn(durf, "Send to All", self._on_message_all,
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=(6,0))

        _section_label(inner, "TELEPORT TO")
        ttf = _field(inner)
        self.v_teleport_target = tk.StringVar()
        tk.Entry(ttf, textvariable=self.v_teleport_target, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6).pack(fill="x")
        _hint(inner, "Another player's name, or a position if the server supports it.")
        _btn(inner, "Teleport", self._on_teleport, "primary",
             font=("Segoe UI",9,"bold"), pady=6).pack(fill="x", padx=8, pady=(4,10))

        _section_label(inner, "SET HOME")
        shf = _field(inner)
        self.v_sethome_value = tk.StringVar(value="reset")
        tk.Entry(shf, textvariable=self.v_sethome_value, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10), bd=6).pack(fill="x")
        _hint(inner, "'reset' clears it back to the respawn point.")
        _btn(inner, "Set Home", self._on_set_home, "primary",
             font=("Segoe UI",9,"bold"), pady=6).pack(fill="x", padx=8, pady=(4,10))

        # ── Player Stats ─────────────────────────────────────────────────
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=(4,0))
        sh = tk.Frame(inner, bg=BG)
        sh.pack(fill="x", padx=8, pady=(6,2))
        _section_label(inner, "PLAYER STATS")
        _btn(inner, "Refresh Stats", self._on_refresh_stats, "primary",
             font=("Segoe UI",8,"bold"), pady=4, padx=8).pack(anchor="w", padx=8, pady=(0,8))

        # stat sliders grid — two columns
        stats_frame = tk.Frame(inner, bg=BG)
        stats_frame.pack(fill="x", padx=8, pady=(0,12))
        self._stat_vars = {}  # name -> DoubleVar
        stats = [('health', 'Health', 0, 2), ('maxhealth', 'Max Health', 0, 31), ('speed', 'Speed', 0, 15), ('damage', 'Damage', 0, 15), ('poison', 'Poison', 0, 31), ('hunger', 'Hunger', 0, 2), ('damageprotection', 'Damage Protection', 0.1, 10), ('luminosity', 'Luminosity', 0, 15), ('cripplehealth', 'Cripple Health', 0, 1), ('xpboost', 'XP Boost', 0.1, 10), ('nightmare', 'Nightmare', 0, 1), ('fullness', 'Fullness', 0, 6), ('left-hand-stamina', 'L. Stamina', 0, 0.2), ('right-hand-stamina', 'R. Stamina', 0, 0.2), ('aggro', 'Aggro', 1, 10), ('Frost', 'Frost', 0, 1)]
        for i, (stat_name, label, smin, smax) in enumerate(stats):
            col = i % 2
            row_idx = i // 2
            cell = tk.Frame(stats_frame, bg=SURF, highlightbackground=BORDER,
                             highlightthickness=1)
            cell.grid(row=row_idx, column=col, sticky="ew", padx=(0 if col else 0, 6 if col==0 else 0),
                      pady=3, ipadx=6, ipady=4)
            stats_frame.columnconfigure(col, weight=1)
            tk.Label(cell, text=label, bg=SURF, fg=PARCH,
                     font=("Segoe UI",8,"bold"), anchor="w").pack(fill="x", padx=6, pady=(4,0))
            var = tk.DoubleVar(value=0.0)
            self._stat_vars[stat_name] = var
            entry_row = tk.Frame(cell, bg=SURF)
            entry_row.pack(fill="x", padx=4, pady=(2,4))
            entry = tk.Entry(entry_row, textvariable=var, bg=SURF2, fg=PARCH,
                             insertbackground=AMBER, relief="flat",
                             font=("Consolas",10), bd=4, width=8, justify="center")
            entry.pack(side="left", padx=(0,4))
            tk.Label(entry_row, text=f"{smin}–{smax}", bg=SURF, fg=MUTED,
                     font=("Segoe UI",7)).pack(side="left", padx=(0,6))
            _btn(entry_row, "Set", lambda sn=stat_name, v=var: self._on_set_stat(sn, v),
                 font=("Segoe UI",8,"bold"), pady=3, padx=8).pack(side="left")

    def _on_kick(self):
        target = self._current_target()
        if not target:
            return
        self._send(f"player kick {self._q(target)}")

    def _on_kill(self):
        target = self._current_target()
        if not target:
            return
        self._send(f"player kill {self._q(target)}")

    def _on_message(self):
        target = self._current_target()
        if not target:
            return
        msg = self.v_admin_message.get().strip()
        if not msg:
            return
        try:
            duration = float(self.v_admin_duration.get().strip())
        except ValueError:
            duration = 5
        self._send(f"player message {self._q(target)} {self._q(msg)} {duration}")

    def _on_message_all(self):
        msg = self.v_admin_message.get().strip()
        if not msg:
            return
        try:
            duration = float(self.v_admin_duration.get().strip())
        except ValueError:
            duration = 5
        self._send(f"player message * {self._q(msg)} {duration}")

    def _on_teleport(self):
        target = self._current_target()
        if not target:
            return
        dest = self.v_teleport_target.get().strip()
        if not dest:
            messagebox.showinfo("Enter a destination", "Type where to teleport to.", parent=self)
            return
        self._send(f"player teleport {self._q(target)} {self._q(dest)}")

    def _on_set_home(self):
        target = self._current_target()
        if not target:
            return
        home = self.v_sethome_value.get().strip() or "reset"
        self._send(f"player set-home {self._q(target)} {self._q(home)}")

    def _on_set_stat(self, stat_name, var):
        target = self._current_target()
        if not target:
            return
        try:
            value = float(var.get())
        except (ValueError, tk.TclError):
            messagebox.showinfo("Invalid value", f"Enter a number for {stat_name}.", parent=self)
            return
        def on_refresh(rs, rd):
            if rd and isinstance(rd, list):
                for item in rd:
                    if isinstance(item, dict):
                        n = item.get("Name","")
                        v = item.get("Value")
                        if n in self._stat_vars and v is not None:
                            try:
                                self._stat_vars[n].set(round(float(v), 4))
                            except Exception:
                                pass
        self._send(f"player set-stat {self._q(target)} {stat_name} {value}")
        self._send_and_capture(f"player list-stats {self._q(target)}", on_refresh)

    def _on_refresh_stats(self):
        target = self._current_target()
        if not target:
            return
        def on_result(rs, rd):
            if rd and isinstance(rd, list):
                for item in rd:
                    if isinstance(item, dict):
                        n = item.get("Name","")
                        v = item.get("Value")
                        if n in self._stat_vars and v is not None:
                            try:
                                self._stat_vars[n].set(round(float(v), 4))
                            except Exception:
                                pass
            elif rs:
                self._append_log(f"[Stats]\n{rs}\n", "ok")
        self._send_and_capture(f"player list-stats {self._q(target)}", on_result)

    def _on_close(self):
        self._stop.set()
        if hasattr(self, "_ws_client") and self._ws_client:
            self._ws_client.disconnect()
        self.destroy()

class TicketsWindow(tk.Toplevel):
    """Player-facing support tickets — create one, see the server owner's
    replies, respond, or close it yourself. Tied to whichever server +
    username the player is actually using, since a ticket only means
    anything in the context of one specific server's own ticket database."""
    def __init__(self, parent, default_host="", default_username=""):
        super().__init__(parent)
        self.title("Support Tickets")
        self.configure(bg=BG)
        self.geometry("640x600")
        self.resizable(True, True)
        _set_window_icon(self)
        ttk.Style().theme_use("clam")
        self._tickets = []
        self._selected_ticket = None
        self._build(default_host, default_username)
        _enable_dark_titlebar(self)
        self._refresh(silent=True)
        # Same reasoning as the main launcher windows — start at exactly
        # what the fully-built layout needs, then set that as the floor,
        # so shrinking the window can never clip the Reply/Close Ticket row.
        self.update_idletasks()
        fit_w = max(640, self.winfo_reqwidth())
        fit_h = max(600, self.winfo_reqheight())
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)

    def _build(self, default_host, default_username):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🎫  Support Tickets", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        sf = tk.Frame(self, bg=BG)
        sf.pack(fill="x", padx=16, pady=(10,4))
        tk.Label(sf, text="Server:", bg=BG, fg=MUTED, font=("Segoe UI",9)).pack(side="left")
        self.v_host = tk.StringVar(value=default_host)
        tk.Entry(sf, textvariable=self.v_host, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, width=20).pack(side="left", padx=(6,10))
        tk.Label(sf, text="Username:", bg=BG, fg=MUTED, font=("Segoe UI",9)).pack(side="left")
        self.v_username = tk.StringVar(value=default_username)
        tk.Entry(sf, textvariable=self.v_username, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, width=14).pack(side="left", padx=(6,0))
        _btn(sf, "⚑ Pick Saved", self._pick_saved_server,
             font=("Segoe UI",8), pady=4, padx=6).pack(side="left", padx=(8,0))
        _hint(self, "Tickets are tied to whichever username+server you actually play on.")

        br = tk.Frame(self, bg=BG)
        br.pack(fill="x", padx=16, pady=(0,8))
        _btn(br, "⟳ Refresh My Tickets", self._refresh, "primary",
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left")
        _btn(br, "+ New Ticket", self._new_ticket,
             font=("Segoe UI",9), pady=6, padx=10).pack(side="left", padx=(6,0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0,10))

        # A fixed-height outer frame for the tree — _mk_tree's own wrapper
        # always requests expand=True internally, which would otherwise
        # compete with `detail` below for space and squeeze out the Reply/
        # Close Ticket row. The tree only ever needs to show a short list,
        # so it gets just its natural size; `detail` (below) claims
        # whatever's actually left over.
        tree_container = tk.Frame(body, bg=BG)
        tree_container.pack(fill="x")
        self.tree = _mk_tree(tree_container, ("title","status","updated"), [280,80,140], height=7)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = tk.Frame(body, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        detail.pack(fill="both", expand=True, pady=(8,0))

        self.v_detail_title = tk.StringVar(value="Select a ticket, or open a new one.")
        tk.Label(detail, textvariable=self.v_detail_title, bg=SURF, fg=AMBER,
                 font=("Georgia",10,"bold"), wraplength=560, justify="left"
                 ).pack(anchor="w", padx=10, pady=(10,4))

        thread_frame = tk.Frame(detail, bg=BG)
        thread_frame.pack(fill="both", expand=True, padx=10, pady=(0,6))
        self.thread_text = tk.Text(thread_frame, bg=SURF2, fg=PARCH, relief="flat",
                                   bd=0, wrap="word", state="disabled",
                                   font=("Segoe UI",9), height=8)
        tsb = _mk_scrollbar(thread_frame, self.thread_text.yview)
        tsb.pack(side="right", fill="y")
        self.thread_text.config(yscrollcommand=tsb.set)
        self.thread_text.pack(side="left", fill="both", expand=True)
        self.thread_text.tag_config("player", foreground=CYAN)
        self.thread_text.tag_config("owner", foreground=AMBER)
        self.thread_text.tag_config("meta", foreground=MUTED)

        action_row = tk.Frame(detail, bg=SURF)
        action_row.pack(fill="x", padx=10, pady=(0,10))
        self.v_reply = tk.StringVar()
        tk.Entry(action_row, textvariable=self.v_reply, bg=SURF2, fg=PARCH,
                 insertbackground=AMBER, relief="flat",
                 highlightbackground=BORDER, highlightcolor=AMBER, highlightthickness=1,
                 font=("Consolas",9), bd=6).pack(side="left", fill="x", expand=True)
        _btn(action_row, "Reply", self._respond, font=("Segoe UI",9),
             pady=6, padx=8).pack(side="left", padx=(6,0))
        _btn(action_row, "Close Ticket", self._close_ticket, "danger",
             font=("Segoe UI",9), pady=6, padx=8).pack(side="left", padx=(6,0))

    def _current_host_username(self, silent=False):
        host = self.v_host.get().strip()
        username = self.v_username.get().strip()
        if not host or not username:
            if not silent:
                messagebox.showerror("Missing info",
                    "Enter both a server and a username.", parent=self)
            return None, None
        return host, username

    def _pick_saved_server(self):
        def on_select(ip, name, port="1757"):
            self.v_host.set(ip)
            self._refresh(silent=True)
        ServerListPanel(self, on_select)

    def _refresh(self, silent=False):
        host, username = self._current_host_username(silent=silent)
        if not host: return
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "list_mine", username, token)
            except Exception as e:
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Couldn't fetch tickets", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                # Most common cause here: this username+server combo has
                # never actually joined the server, so there's nothing to
                # recognize yet — completely normal the first time this
                # window is opened, not worth surfacing as an error unless
                # the player explicitly clicked Refresh to ask.
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Error", resp.get("message","Unknown error"), parent=self))
                return
            self.after(0, lambda: self._apply_tickets(resp.get("tickets", [])))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_tickets(self, tickets):
        self._tickets = tickets
        for r in self.tree.get_children(): self.tree.delete(r)
        for t in sorted(tickets, key=lambda t: t["updated_at"], reverse=True):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["updated_at"]))
            self.tree.insert("", "end", iid=t["ticket_id"], values=(t["title"], t["status"], ts))

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel: return
        t = next((x for x in self._tickets if x["ticket_id"] == sel[0]), None)
        if not t: return
        self._selected_ticket = t
        self.v_detail_title.set(f"{t['title']}  ({t['status']})")
        self.thread_text.config(state="normal")
        self.thread_text.delete("1.0", "end")
        self.thread_text.insert("end", t["description"] + "\n\n")
        for c in t.get("comments", []):
            who = "Server Owner" if c["from"] == "owner" else "You"
            tag = "owner" if c["from"] == "owner" else "player"
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["at"]))
            self.thread_text.insert("end", f"[{ts}] {who}: ", tag)
            self.thread_text.insert("end", f"{c['message']}\n")
        self.thread_text.see("end")
        self.thread_text.config(state="disabled")

    def _new_ticket(self):
        host, username = self._current_host_username()
        if not host: return

        win = tk.Toplevel(self)
        win.title("New Ticket")
        win.configure(bg=BG)
        win.resizable(False, False)
        _set_window_icon(win)

        _section_label(win, "TITLE")
        tf = _field(win)
        v_title = tk.StringVar()
        tk.Entry(tf, textvariable=v_title, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(fill="x")

        _section_label(win, "DESCRIPTION")
        df = tk.Frame(win, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        df.pack(fill="both", expand=True, padx=20, pady=(0,6))
        desc_text = tk.Text(df, bg=SURF, fg=PARCH, insertbackground=AMBER,
                            relief="flat", bd=6, wrap="word", height=8,
                            font=("Segoe UI",9))
        desc_text.pack(fill="both", expand=True)

        def _submit():
            title = v_title.get().strip()
            description = desc_text.get("1.0","end").strip()
            if not title or not description:
                messagebox.showerror("Missing info",
                    "Title and description are both required.", parent=win)
                return
            resolved = _resolve_ip_for_game(host)
            token, _ = _get_or_create_token(resolved, username)
            def worker():
                try:
                    resp = ticket_request(resolved, "create", username, token,
                                          title=title, description=description, server=host)
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror(
                        "Couldn't submit ticket", str(e), parent=win))
                    return
                if resp.get("status") != "ok":
                    self.after(0, lambda: messagebox.showerror(
                        "Error", resp.get("message","Unknown error"), parent=win))
                    return
                def _done():
                    win.destroy()
                    self._refresh()
                self.after(0, _done)
            threading.Thread(target=worker, daemon=True).start()

        tk.Frame(win, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(4,6))
        _btn(win, "📨  Submit Ticket", _submit, "primary",
             font=("Georgia",11,"bold"), pady=12).pack(fill="x", padx=20, pady=(0,16))

        win.update_idletasks()
        win.geometry(f"420x{win.winfo_reqheight()}")
        _enable_dark_titlebar(win)
        win.transient(self)
        win.grab_set()

    def _selected_ticket_id(self):
        if not self._selected_ticket:
            messagebox.showinfo("No selection", "Select a ticket first.", parent=self)
            return None
        return self._selected_ticket["ticket_id"]

    def _respond(self):
        tid = self._selected_ticket_id()
        if not tid: return
        msg = self.v_reply.get().strip()
        if not msg: return
        if self._selected_ticket.get("status") != "open":
            messagebox.showinfo("Ticket closed", "This ticket is already closed.", parent=self)
            return
        host, username = self._current_host_username()
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "respond", username, token,
                                      ticket_id=tid, message=msg)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Couldn't send reply", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                self.after(0, lambda: messagebox.showerror(
                    "Error", resp.get("message","Unknown error"), parent=self))
                return
            def _done():
                self.v_reply.set("")
                self._refresh()
            self.after(0, _done)
        threading.Thread(target=worker, daemon=True).start()

    def _close_ticket(self):
        tid = self._selected_ticket_id()
        if not tid: return
        if self._selected_ticket.get("status") != "open":
            messagebox.showinfo("Already closed", "This ticket is already closed.", parent=self)
            return
        msg = simpledialog.askstring("Close Ticket",
            "Optional closing message (e.g. \"fixed it myself\"):", parent=self) or ""
        if not messagebox.askyesno("Close Ticket",
                "Close this ticket? You won't be able to reply to it afterward.", parent=self):
            return
        host, username = self._current_host_username()
        resolved = _resolve_ip_for_game(host)
        token, _ = _get_or_create_token(resolved, username)
        def worker():
            try:
                resp = ticket_request(resolved, "close", username, token,
                                      ticket_id=tid, message=msg)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Couldn't close ticket", str(e), parent=self))
                return
            if resp.get("status") != "ok":
                self.after(0, lambda: messagebox.showerror(
                    "Error", resp.get("message","Unknown error"), parent=self))
                return
            self.after(0, self._refresh)
        threading.Thread(target=worker, daemon=True).start()


class SetupWindow(tk.Toplevel):
    """Patch, MelonLoader, TavernLib, in that order — the fixed sequence
    everything else in the launcher depends on. Each step's button stays
    disabled until the step before it has been installed at least once
    (see _lock_row) — a fresh install can't jump ahead — but that lock never
    blocks re-running a step that's already installed (an update or a
    reinstall), even if an earlier step has since gone missing again."""

    def __init__(self, parent, exe_path, on_status_change=None):
        super().__init__(parent)
        self.title("Setup")
        self.configure(bg=BG)
        self.geometry("520x480")
        self.resizable(False, False)
        self._exe = exe_path
        self._game_dir = os.path.dirname(exe_path)
        self._busy = False
        self._on_status_change = on_status_change
        self._build()
        self.update_idletasks()
        self.geometry(f"520x{self.winfo_reqheight()}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _enable_dark_titlebar(self)

    def _on_close(self):
        if self._on_status_change: self._on_status_change()
        self.destroy()

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🛠  Setup", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        self._auto_btn = _btn(h, "⚡ Automatic Setup", self._on_automatic_setup,
                              style="primary", font=("Segoe UI",9,"bold"), pady=6, padx=12)
        self._auto_btn.pack(side="right", padx=12)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="These set up modding for A Township Tale on this machine, in "
                 "order: Patch, then MelonLoader, then TavernLib. If GitHub can't "
                 "be reached (some networks/antivirus block it), the version "
                 "bundled with this launcher is used automatically instead.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,8))

        _section_label(self, "IN ORDER")
        self._patch_btn = self._mod_row(
            "Patch", "Enables hosting and connecting to custom servers.",
            self._on_patch_click)
        self._ml_btn = self._mod_row(
            "MelonLoader", "Universal Mod Loader for Unity Games.",
            self._on_melonloader_click)
        self._tl_btn = self._mod_row(
            "TavernLib", "MelonLoader plugin to keep the game alive.",
            self._on_tavernlib_click)

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=470, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,10))

        self._refresh_states()

    def _mod_row(self, title, subtitle, on_click):
        row = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", padx=20, pady=4)
        dotvar = tk.StringVar(value="○")
        dot = tk.Label(row, textvariable=dotvar, bg=SURF, fg=MUTED, font=("Segoe UI",13))
        dot.pack(side="left", padx=(14,10), pady=10)
        tf = tk.Frame(row, bg=SURF)
        tf.pack(side="left", fill="both", expand=True, pady=8)
        tk.Label(tf, text=title, bg=SURF, fg=PARCH, font=("Georgia",10,"bold")).pack(anchor="w")
        tk.Label(tf, text=subtitle, bg=SURF, fg=MUTED, font=("Segoe UI",8),
                 wraplength=280, justify="left").pack(anchor="w")
        # Its own line, separate from the (static) description above, so
        # "Up to date." / a version tag / a lock message doesn't get run
        # into the description text — and this line is always reserved
        # (even when empty) so a row doesn't change height when it appears.
        notevar = tk.StringVar(value="")
        note = tk.Label(tf, textvariable=notevar, bg=SURF, fg=MUTED, font=("Segoe UI",8),
                        wraplength=280, justify="left")
        note.pack(anchor="w")
        # Fixed width so the row doesn't shift when the label changes length
        # ("⬇ Install" vs "⟳ Reinstall") as a step's state changes.
        btn = _btn(row, "…", on_click, font=("Segoe UI",9), pady=6, padx=12, width=11)
        btn.pack(side="right", padx=12)
        btn._dotvar = dotvar
        btn._dotlabel = dot
        btn._notevar = notevar
        return btn

    # ── Status ───────────────────────────────────────────────────────────────

    _STATE_STYLE = {
        "missing":  ("○", MUTED, "⬇ Install"),
        "outdated": ("⚠", AMBER, "⟳ Update"),
        "unknown":  ("●", MUTED, "⟳ Reinstall"),
        "current":  ("●", GREEN, "⟳ Reinstall"),
    }
    _STATE_NOTE = {
        "missing": "",
        "outdated": "Update available.",
        "unknown": "",
        "current": "Up to date.",
    }

    def _refresh_states(self):
        self._status.set("Checking status…")
        exe, game_dir = self._exe, self._game_dir
        def worker():
            patch_state = "current" if _patch_is_applied(exe) else "missing"
            ml = _melonloader_status(game_dir)
            tl = _tavernlib_status(game_dir)
            ml_tag = _load_mod_meta(game_dir).get("melonloader_tag")
            self.after(0, lambda: self._apply_states(patch_state, ml, tl, ml_tag))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_states(self, patch_state, ml_state, tl_state, ml_tag):
        self._apply_row_state(self._patch_btn, patch_state)
        self._apply_row_state(self._ml_btn, ml_state)
        self._apply_row_state(self._tl_btn, tl_state)
        # A real release tag (not the "bundled:<hash>" fallback marker) is
        # worth showing so it's obvious exactly what got installed, not just
        # that something did.
        if ml_tag and not ml_tag.startswith("bundled:"):
            note = self._ml_btn._notevar.get()
            self._ml_btn._notevar.set(f"{note}  ({ml_tag})" if note else f"({ml_tag})")
        self._lock_row(self._ml_btn, ml_state, patch_state, "Patch")
        self._lock_row(self._tl_btn, tl_state, ml_state, "MelonLoader")
        self._status.set("")
        if self._on_status_change: self._on_status_change()

    def _apply_row_state(self, btn, state):
        dot, color, text = self._STATE_STYLE[state]
        btn._dotvar.set(dot)
        btn._dotlabel.config(fg=color)
        btn.config(text=text)
        btn._notevar.set(self._STATE_NOTE[state])

    def _lock_row(self, btn, state, prior_state, prior_name):
        """Disables a step's button only when it's never been installed AND
        its prerequisite hasn't either — never blocks updating/reinstalling
        a step that's already there, no matter what the earlier step is
        doing right now."""
        locked = state == "missing" and prior_state == "missing"
        if not self._busy:
            btn.config(state="disabled" if locked else "normal")
        if locked:
            note = btn._notevar.get()
            lock_msg = f"Install {prior_name} first"
            btn._notevar.set(f"{note}  ·  {lock_msg}" if note else lock_msg)

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._auto_btn.config(state=state)
        self._patch_btn.config(state=state)
        self._ml_btn.config(state=state)
        self._tl_btn.config(state=state)
        self._status.set(msg)

    def _on_patch_click(self):
        if self._busy: return
        self._set_busy(True, "Checking for the latest patch…")
        exe = self._exe
        def worker():
            try:
                result = apply_patch(exe, lambda m: self.after(0, lambda: self._status.set(m)))
                messages = {
                    "downloaded": "Downloaded the latest Tavern patch from GitHub and applied it.",
                    "bundled": "Couldn't reach GitHub, so the version bundled with this "
                               "launcher was applied instead.",
                    "current": "Already up to date — no changes were needed.",
                }
                msg = messages.get(result, "Root.Township.dll has been replaced with the Tavern patch.")
                self.after(0, lambda: self._finish_install(True, msg))
            except RuntimeError as e:
                self.after(0, lambda err=str(e): self._finish_install(False, f"Patch failed: {err}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_melonloader_click(self):
        if self._busy: return
        arch = _detect_exe_arch(self._exe)
        if not arch:
            messagebox.showerror("Can't tell architecture",
                "Couldn't determine whether the game is 32- or 64-bit from "
                "the selected .exe. Try re-browsing to it on the main screen.", parent=self)
            return
        self._set_busy(True, f"Detected {arch} game — starting install…")

        def worker():
            try:
                _install_melonloader(self._game_dir, arch,
                    lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish_install(True, "MelonLoader installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_tavernlib_click(self):
        if self._busy: return
        if not _melonloader_installed(self._game_dir):
            messagebox.showwarning("Install MelonLoader first",
                "TavernLib is a MelonLoader plugin — install MelonLoader above first.", parent=self)
            return
        self._set_busy(True, "Starting TavernLib install…")

        def worker():
            try:
                _install_tavernlib(self._game_dir,
                    lambda m: self.after(0, lambda: self._status.set(m)))
                self.after(0, lambda: self._finish_install(True, "TavernLib installed."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Install failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _on_automatic_setup(self):
        if self._busy: return
        self._set_busy(True, "Running automatic setup…")
        exe, game_dir = self._exe, self._game_dir

        def worker():
            try:
                if not _patch_is_applied(exe):
                    self.after(0, lambda: self._status.set("Applying patch…"))
                    apply_patch(exe, lambda m: self.after(0, lambda: self._status.set(m)))

                if _melonloader_status(game_dir) != "current":
                    arch = _detect_exe_arch(exe)
                    if not arch:
                        raise RuntimeError(
                            "Couldn't determine whether the game is 32- or 64-bit from "
                            "the selected .exe. Try re-browsing to it on the main screen.")
                    self.after(0, lambda: self._status.set(f"Detected {arch} game — installing MelonLoader…"))
                    _install_melonloader(game_dir, arch,
                        lambda m: self.after(0, lambda: self._status.set(m)))

                if _tavernlib_status(game_dir) != "current":
                    self.after(0, lambda: self._status.set("Installing TavernLib…"))
                    _install_tavernlib(game_dir,
                        lambda m: self.after(0, lambda: self._status.set(m)))

                self.after(0, lambda: self._finish_install(True, "Automatic setup complete."))
            except Exception as e:
                self.after(0, lambda e=e: self._finish_install(False, f"Automatic setup failed: {e}"))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_install(self, ok, msg):
        self._set_busy(False, msg)
        self._refresh_states()

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LAUNCHER
# ══════════════════════════════════════════════════════════════════════════════

class ClientLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        _modmanager.set_helpers(sys.modules[__name__])
        self.title("TavernLauncher - Client")
        self.configure(bg=BG)
        # This is the one window players stare at while the log scrolls —
        # letting it resize means the log area actually gets to use whatever
        # space is available instead of being locked to one fixed height.
        self.resizable(True, True)
        self.geometry("540x820")  # placeholder; resized to fit content below
        _set_window_icon(self)
        ttk.Style().theme_use("clam")
        self._tailer      = None
        self._server_ok   = False   # True once Check Server succeeds
        self._checked_host = None
        # Tracks whether the currently-filled-in server (from the Community
        # browser) is an official Tavern server or a headless/direct-connect
        # one — controls whether Join Server goes through the auth handshake
        # at all. Manually typed IPs and Saved/Recent selections always reset
        # this back to "official", matching how they've always behaved.
        self._selected_kind = "official"
        self._setup_animating  = False
        self._setup_anim_job   = None
        self._setup_anim_phase = 0
        self._exe_check_job   = None
        # Join-time mod reconciliation cache: host -> (mods_hash, mods_list),
        # so rejoining a server whose mods haven't changed since last time
        # skips the extra "mods_list" round trip entirely (see _get_server_mods).
        self._mods_list_cache = {}
        self._mod_manager_win = None
        self._build_ui()
        self._load()
        # Start at exactly the size the fully-built layout needs, then set
        # that as the floor — shrinking further would start cutting into
        # either the log area or the bottom toggle row (whichever runs out
        # of room first), while growing beyond it just gives the log more
        # room to breathe. fit_w used to be a hardcoded guess that went
        # stale every time a row gained another button/checkbox — measuring
        # it the same way fit_h already was is what actually keeps this
        # correct going forward.
        self.update_idletasks()
        fit_w = max(540, self.winfo_reqwidth())
        fit_h = self.winfo_reqheight()
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)
        _enable_dark_titlebar(self)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._header()

        _section_label(self, "Path to 'A Township Tale.exe'")
        pf = _field(self)
        self.v_exe = tk.StringVar()
        self.v_exe.trace_add("write", self._on_exe_changed)
        tk.Entry(pf, textvariable=self.v_exe, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True)
        _btn(pf, "Browse", self._browse, font=("Segoe UI",9),
             padx=10, pady=6).pack(side="right")
        btn_row_mods = tk.Frame(self, bg=BG)
        btn_row_mods.pack(fill="x", padx=20, pady=(4,0))
        self._setup_btn = _btn(btn_row_mods, "🛠 Setup", self._open_setup,
             font=("Segoe UI",9), pady=5, padx=10)
        self._setup_btn.pack(side="left")
        self._mod_manager_btn = _btn(btn_row_mods, "📦 Mod Manager",
             self._open_mod_manager, font=("Segoe UI",9), pady=5, padx=10)
        self._mod_manager_btn.pack(side="left", padx=(6,0))
        _hint(self, "Run Setup before you launch the game, in order — Patch, then MelonLoader, then TavernLib")

        _divider(self)

        _section_label(self, "CHOOSE YOUR USERNAME")
        nf = _field(self)
        self.v_username = tk.StringVar()
        tk.Entry(nf, textvariable=self.v_username, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(fill="x")
        _hint(self, f"Your save is tied to this name. Max {USERNAME_MAX_LEN} characters. "
                    "Letters, numbers, spaces, hyphens, and underscores only.")

        _section_label(self, "CHOOSE YOUR PLATFORM")
        pf2 = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        pf2.pack(fill="x", padx=20, pady=(0,4))
        self.v_platform = tk.StringVar(value="SteamVR")
        _mk_combobox(pf2, self.v_platform, ["SteamVR","Quest"])

        _divider(self)

        _section_label(self, "Destination (leave blank for localhost)")
        sf = _field(self)
        self.v_ip = tk.StringVar()
        self.v_ip.trace_add("write", self._on_ip_changed)
        tk.Entry(sf, textvariable=self.v_ip, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6).pack(side="left", fill="x", expand=True)
        self.v_port = tk.StringVar(value="1757")
        tk.Entry(sf, textvariable=self.v_port, bg=SURF, fg=PARCH,
                 insertbackground=AMBER, relief="flat", font=("Consolas",10),
                 bd=6, width=6, justify="center").pack(side="left", padx=(4,0))

        btn_row_dest = tk.Frame(self, bg=BG)
        btn_row_dest.pack(fill="x", padx=20, pady=(4,0))
        _btn(btn_row_dest, "⚑ Saved",             self._open_server_list,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")
        _btn(btn_row_dest, "🌍 Community Servers", self._open_community,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=6)
        _btn(btn_row_dest, "🎫 Tickets",           self._open_tickets,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=(0,6))
        _btn(btn_row_dest, "🖥 Remote Console",    self._open_remote_console,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left", padx=(0,6))
        _btn(btn_row_dest, "🧩 TavernKeeper",     self._open_tavernkeeper,
             font=("Segoe UI",9), pady=5, padx=10).pack(side="left")

        # ── Action area ──────────────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=20, pady=6)

        # Status line shown after Check Server — plain label, not a box, with
        # the mod-sync button beside it. The button is packed only once a check
        # comes back from a server that speaks the mod protocol (see _check_ok),
        # so it never shows for a server that has nothing to sync against.
        check_row = tk.Frame(self, bg=BG)
        check_row.pack(fill="x", padx=22, pady=(0,4))
        self._check_status = tk.StringVar(value="")
        self._check_label = tk.Label(check_row, textvariable=self._check_status,
                 bg=BG, fg=MUTED, font=("Segoe UI",9),
                 justify="left", anchor="w", wraplength=420)
        self._check_label.pack(side="left")
        self._sync_btn = _btn(check_row, "⇄ Sync Mods", self._on_sync_mods,
                              font=("Segoe UI",9), pady=4, padx=10)

        # Check Server and Join Server sit side by side — checking is purely
        # optional/informational now, never a gate on joining.
        action_row = tk.Frame(self, bg=BG)
        action_row.pack(fill="x", padx=20, pady=(0,4))
        self._check_btn = _btn(action_row, "🔍  Check Server", self._do_check,
                                font=("Georgia",12,"bold"), pady=14)
        self._check_btn.pack(side="left", fill="x", expand=True, padx=(0,4))
        self._action_btn = _btn(action_row, "⚔  Join Server", self._on_join_clicked,
                                style="primary", font=("Georgia",12,"bold"), pady=14)
        self._action_btn.pack(side="left", fill="x", expand=True, padx=(4,0))

        # ── Log ───────────────────────────────────────────────────────────────
        _section_label(self, "GAME LOG")
        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=20, pady=(0,8))
        lb = tk.Frame(lf, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        lb.pack(fill="both", expand=True)
        self.log = tk.Text(lb, bg=SURF, fg="#b09a78", font=MONO,
                           relief="flat", bd=0, state="disabled", height=12,
                           wrap="none")
        sb = _mk_scrollbar(lb, self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        for t,c in [("ok",GREEN),("warn",AMBER),("err",RED),
                    ("cyan",CYAN),("dim",MUTED),("error",RED),
                    ("info","#b09a78"),("debug",MUTED)]:
            self.log.tag_config(t, foreground=c)

        # ── Enhanced Debugging / Show MelonLoader toggles ────────────────────
        df = tk.Frame(self, bg=BG)
        df.pack(side="bottom", fill="x", padx=14, pady=(0,6))
        self.v_debug_helper = tk.BooleanVar(value=False)
        tk.Checkbutton(df, text="Enhanced Debugging", variable=self.v_debug_helper,
                       command=self._save, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       font=("Segoe UI",8)).pack(side="left")
        self.v_show_melonloader = tk.BooleanVar(value=False)
        tk.Checkbutton(df, text="Show MelonLoader", variable=self.v_show_melonloader,
                       command=self._save, bg=BG, fg=MUTED, selectcolor=SURF,
                       activebackground=BG, activeforeground=AMBER,
                       font=("Segoe UI",8)).pack(side="left", padx=(14,0))
        _btn(df, "🗑 Wipe Cache", self._wipe_cache,
             font=("Segoe UI",7), pady=2, padx=6).pack(side="right")

    def _header(self):
        h = tk.Frame(self, bg=SURF, height=64)
        h.pack(fill="x"); h.pack_propagate(False)

        canvas = tk.Canvas(h, bg=SURF, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self._header_canvas   = canvas
        self._header_bg_photo = None
        self._header_bg_item  = None
        if _HEADER_BANNER_IMG is not None:
            self._header_bg_item = canvas.create_image(0, 0, anchor="nw")

        canvas.create_rectangle(0, 0, 4, 64, fill=AMBER, width=0)
        canvas.create_text(18, 32, text="⚔", fill=AMBER, font=("Georgia",22), anchor="w")
        canvas.create_text(66, 21, text="The Modding Tavern", fill=AMBER,
                           font=("Georgia",14,"bold"), anchor="w")
        canvas.create_text(66, 42, text=f"Client Launcher  ·  v{APP_VERSION}", fill=AMBER,
                           font=("Segoe UI",9), anchor="w")

        self._discord_btn = tk.Button(canvas, text="💬 Discord", bg=SURF2, fg=AMBER,
                                      activebackground=AMBERDIM, activeforeground="#ffd080",
                                      relief="flat", bd=0, cursor="hand2",
                                      font=("Segoe UI",9,"bold"), padx=10, pady=4,
                                      command=lambda: webbrowser.open(DISCORD_URL))
        self._discord_btn_item = canvas.create_window(0, 32, anchor="e", window=self._discord_btn)

        # Token badge — a real Button (for its existing click/animation
        # logic) embedded onto the canvas so it layers correctly over the
        # banner image; created hidden, _show_token_button() reveals it.
        self._token_note = (
            "A token file has been created for you. This file is used to prove who you "
            "are when connecting to a server with your chosen username. It can be found "
            "in your %AppData%\\Roaming\\TheModdingTavern\\tokens folder. Make sure to keep "
            "this file safe, as you won't be able to connect with this account if it is "
            "lost. If you do lose it - please reach out to the server owner to get it back."
        )
        self._token_animating = False
        self._token_anim_job  = None
        self._token_anim_phase = 0
        self._token_btn = tk.Button(canvas, text="🔑 Token", bg=SURF2, fg=AMBER,
                                    activebackground=AMBERDIM, activeforeground="#ffd080",
                                    relief="flat", bd=0, cursor="hand2",
                                    font=("Segoe UI",9,"bold"), padx=10, pady=4,
                                    command=self._on_token_button_click)
        self._token_btn_item = canvas.create_window(0, 32, anchor="e",
                                                     window=self._token_btn, state="hidden")

        canvas.bind("<Configure>", self._on_header_resize)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _on_header_resize(self, event):
        """Rescales the banner to fill the header exactly, and keeps the
        Discord/token badges right-aligned — none of this reflows on its
        own, since a Canvas doesn't auto-stretch or reposition children."""
        w, hgt = event.width, event.height
        if w < 2 or hgt < 2:
            return
        if _HEADER_BANNER_IMG is not None and self._header_bg_item is not None:
            try:
                box = _header_crop_box(_HEADER_BANNER_IMG.width, _HEADER_BANNER_IMG.height, w, hgt)
                resized = _HEADER_BANNER_IMG.crop(box).resize((w, hgt), _PILImage.LANCZOS)
                # Uniform darken so the amber/parchment text stays legible
                # regardless of which part of the artwork ends up behind it.
                resized = _PILImageEnhance.Brightness(resized).enhance(0.5)
                photo = _PILImageTk.PhotoImage(resized)
                self._header_canvas.itemconfig(self._header_bg_item, image=photo)
                self._header_bg_photo = photo  # keep a reference or Tk drops it
            except Exception:
                pass
        self._header_canvas.coords(self._discord_btn_item, w - 14, hgt // 2)
        discord_w = self._discord_btn.winfo_reqwidth()
        self._header_canvas.coords(self._token_btn_item, w - 14 - discord_w - 10, hgt // 2)

    # ── Token badge / animation ─────────────────────────────────────────────

    def _show_token_button(self):
        """Reveal the token badge. Called at startup if a token file already
        exists, and after every successful connection. Only starts the
        flash if the player hasn't already clicked through the "Yes, I
        understand" acknowledgment — once they have, it stays a plain,
        non-flashing button for the rest of time, on this machine."""
        if self._header_canvas.itemcget(self._token_btn_item, "state") != "normal":
            self._header_canvas.itemconfigure(self._token_btn_item, state="normal")
            # Position it correctly immediately — otherwise it sits at the
            # placeholder (0, ...) coordinate from creation until the next
            # window resize happens to trigger a reposition. Same formula as
            # _on_header_resize: left of the always-visible Discord button.
            w   = self._header_canvas.winfo_width()
            hgt = self._header_canvas.winfo_height()
            discord_w = self._discord_btn.winfo_reqwidth()
            self._header_canvas.coords(self._token_btn_item, w - 14 - discord_w - 10, hgt // 2)
        if not self._token_animating and not load_cfg().get("token_ack", False):
            self._start_token_animation()

    def _start_token_animation(self):
        self._token_animating = True
        self._token_anim_phase = 0
        self._animate_token_btn()

    def _stop_token_animation(self):
        self._token_animating = False
        if self._token_anim_job:
            try: self.after_cancel(self._token_anim_job)
            except Exception: pass
            self._token_anim_job = None
        try: self._token_btn.config(bg=SURF2, fg=AMBER)
        except Exception: pass

    def _animate_token_btn(self):
        if not self._token_animating: return
        bg, fg = (SURF2, AMBER) if self._token_anim_phase % 2 == 0 else ("#5a3d0e", "#ffd080")
        try: self._token_btn.config(bg=bg, fg=fg)
        except Exception: return
        self._token_anim_phase += 1
        self._token_anim_job = self.after(450, self._animate_token_btn)

    def _on_token_button_click(self):
        win = tk.Toplevel(self)
        win.title("About Your Token File")
        win.configure(bg=BG)
        win.resizable(False, False)
        _set_window_icon(win)
        tk.Label(win, text=self._token_note, bg=BG, fg=PARCH, justify="left",
                 wraplength=360, font=("Segoe UI",9)).pack(padx=20, pady=(20,16))

        def _ack():
            cfg = load_cfg()
            cfg["token_ack"] = True
            save_cfg(cfg)
            self._stop_token_animation()
            win.destroy()

        _btn(win, "Yes, I understand", _ack, "primary",
             font=("Segoe UI",10,"bold"), pady=10).pack(fill="x", padx=20, pady=(0,20))
        win.update_idletasks()
        win.geometry(f"400x{win.winfo_reqheight()}")
        _enable_dark_titlebar(win)
        win.transient(self)
        win.grab_set()
        # Block until it's dismissed. The first-token case opens this from the
        # middle of a join, and the mod comparison follows right after; without
        # the wait both windows appear at once and the comparison's own grab
        # takes the input, leaving the token note unreadable and unclickable.
        win.wait_window()

    # ── Mods alert / animation ──────────────────────────────────────────────
    # Unlike the token badge, this flashes only *while there's a problem* —
    # a mod missing or out of date — and stops on its own once resolved.

    def _on_exe_changed(self, *_):
        if self._exe_check_job:
            try: self.after_cancel(self._exe_check_job)
            except Exception: pass
        self._exe_check_job = self.after(800, self._refresh_tool_states)

    def _refresh_tool_states(self):
        """Enables/disables the Setup and Mod Manager buttons based on
        whether a valid game exe is selected, then separately refreshes the
        Setup button's own flashing-alert condition. State is only ever
        touched here, and the animation loop below only ever touches bg/fg —
        kept deliberately separate so neither path can clobber the other."""
        exe = self.v_exe.get().strip()
        valid = bool(exe and os.path.isfile(exe))
        state = "normal" if valid else "disabled"
        try: self._setup_btn.config(state=state)
        except Exception: pass
        try: self._mod_manager_btn.config(state=state)
        except Exception: pass
        self._refresh_setup_alert()

    def _refresh_setup_alert(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            self._set_setup_alert(False)
            return
        game_dir = os.path.dirname(exe)
        def worker():
            try:
                need = _mods_need_attention(game_dir) or (
                    os.path.isfile(_patch_source_path()) and not _patch_is_applied(exe))
            except Exception:
                need = False
            self.after(0, lambda: self._set_setup_alert(need))
        threading.Thread(target=worker, daemon=True).start()

    def _set_setup_alert(self, needed):
        if needed: self._start_setup_animation()
        else:      self._stop_setup_animation()

    def _start_setup_animation(self):
        if self._setup_animating: return
        self._setup_animating = True
        self._setup_anim_phase = 0
        self._animate_setup_btn()

    def _animate_setup_btn(self):
        if not self._setup_animating: return
        bg, fg = (SURF2, AMBER) if self._setup_anim_phase % 2 == 0 else ("#5a3d0e", "#ffd080")
        try: self._setup_btn.config(bg=bg, fg=fg)
        except Exception: return
        self._setup_anim_phase += 1
        self._setup_anim_job = self.after(450, self._animate_setup_btn)

    def _stop_setup_animation(self):
        self._setup_animating = False
        if self._setup_anim_job:
            try: self.after_cancel(self._setup_anim_job)
            except Exception: pass
            self._setup_anim_job = None
        try: self._setup_btn.config(bg=SURF2, fg=PARCH)
        except Exception: pass

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        cfg = load_cfg()
        self.v_exe.set(cfg.get("game_exe",""))
        self.v_username.set(cfg.get("username",""))
        # "none" (flatscreen) is temporarily disabled — exploitable — so a
        # value saved before this change doesn't silently keep working just
        # because it's already sitting in the user's config file. Also
        # translates a pre-rename save ("OpenVR"/"Oculus") to the current
        # display names, so upgrading doesn't silently reset this choice.
        saved_platform = cfg.get("platform", "SteamVR")
        saved_platform = PLATFORM_LEGACY_TO_DISPLAY.get(saved_platform, saved_platform)
        self.v_platform.set(saved_platform if saved_platform in ("SteamVR", "Quest") else "SteamVR")
        self.v_ip.set(cfg.get("last_ip",""))
        self.v_port.set(cfg.get("last_port","1757"))
        self.v_debug_helper.set(cfg.get("debug_helper", False))
        self.v_show_melonloader.set(cfg.get("show_melonloader", False))
        self._print("Ready. Enter a server IP, then Check Server (optional) or Join Server.", "dim")
        self._start_log_tailer()
        if _any_token_files_exist():
            self._show_token_button()
        # Immediate check at startup — the trace-driven debounce from
        # v_exe.set above will also fire, but 800ms later; this makes the
        # Patch/Mods button states correct from the very first frame.
        self._refresh_tool_states()
        # Update check runs a couple seconds after startup, off the UI
        # thread, so it never delays the window actually appearing.
        self.after(2000, self._check_for_launcher_update)

    def _check_for_launcher_update(self):
        if _updater is None:
            return
        def worker():
            result = _updater.check_for_update(APP_VERSION, UPDATE_APP_FOLDER)
            if result:
                tag, url = result
                self.after(0, lambda: self._prompt_launcher_update(tag, url))
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_launcher_update(self, tag, url):
        if not messagebox.askyesno("Update Available",
                f"A new version is available: {tag} (you have {APP_VERSION}).\n\n"
                "Update now? The launcher will restart automatically.", parent=self):
            return
        self._print(f"Updating to {tag}…", "warn")
        def worker():
            try:
                _updater.download_and_apply_update(url, UPDATE_APP_FOLDER,
                    on_progress=lambda m: self.after(0, lambda: self._print(m, "warn")))
                # download_and_apply_update relaunches and calls os._exit()
                # on success — if we get here at all, something went wrong
                # after the point of no return.
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Update failed",
                    f"Couldn't apply the update:\n{e}\n\n"
                    "The current version is unaffected — nothing was replaced.", parent=self))
        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        cfg = load_cfg()
        cfg.update({"game_exe": self.v_exe.get(), "username": self.v_username.get(),
                    "platform": self.v_platform.get(), "last_ip": self.v_ip.get(),
                    "last_port": self.v_port.get(),
                    "debug_helper": self.v_debug_helper.get(),
                    "show_melonloader": self.v_show_melonloader.get()})
        save_cfg(cfg)

    def _wipe_cache(self):
        if not messagebox.askyesno("Wipe Launcher Cache",
                "This will delete this launcher's saved settings file:\n\n"
                f"{CONFIG_FILE}\n\n"
                "That includes your saved username, game path, last server "
                "IP, and toggle preferences — giving you a completely fresh, "
                "unconfigured launcher next time it starts.\n\n"
                "Your token files, patch, and installed mods are NOT affected.\n\n"
                "This cannot be undone. Continue?", icon="warning", parent=self):
            return
        try:
            if os.path.isfile(CONFIG_FILE):
                os.remove(CONFIG_FILE)
            messagebox.showinfo("Cache Wiped",
                "Launcher cache cleared. The app will now close — "
                "reopen it for a fresh start.", parent=self)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Wipe failed", str(e), parent=self)

    def _browse(self):
        p = filedialog.askopenfilename(
            title="Select A Township Tale.exe",
            filetypes=[("Executable","*.exe"),("All","*.*")])
        if p: self.v_exe.set(p.replace("/","\\")); self._save()

    def _open_server_list(self):
        def on_select(ip, name, port="1757"):
            self.v_ip.set(ip); self.v_port.set(str(port))
            self._selected_kind = "official"; self._save()
        ServerListPanel(self, on_select)

    def _open_community(self):
        def on_select(ip, name, kind, port="1757"):
            self.v_ip.set(ip); self.v_port.set(str(port))
            self._selected_kind = kind; self._save()
        CommunityBrowser(self, on_select)

    def _open_tickets(self):
        TicketsWindow(self, default_host=self.v_ip.get().strip(),
                     default_username=self.v_username.get().strip())

    def _open_remote_console(self):
        win = RemoteConsoleWindow(self)
        host = self.v_ip.get().strip()
        if host:
            win.v_host.set(host)

    def _open_tavernkeeper(self):
        win = TavernKeeperWindow(self)
        host = self.v_ip.get().strip()
        if host:
            win.v_host.set(host)

    def _open_setup(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return
        SetupWindow(self, exe, on_status_change=self._refresh_setup_alert)

    def _open_mod_manager(self):
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return
        if self._mod_manager_win and self._mod_manager_win.winfo_exists():
            self._mod_manager_win.lift(); return
        self._mod_manager_win = _modmanager.ModManagerWindow(
            self, os.path.dirname(exe), side="client", on_change=self._refresh_setup_alert)

    def _on_ip_changed(self, *_):
        """Clear the stale check-status line whenever the IP field changes —
        Check Server and Join Server are both independent from here on, so
        there's no button mode to reset, just the leftover status text.
        Also resets to "official" — a manually-typed IP isn't something we
        know the kind of, so fall back to the flow that's always applied."""
        self._server_ok    = False
        self._checked_host = None
        self._selected_kind = "official"
        self._check_status.set("")
        try: self._check_label.config(fg=MUTED)
        except: pass
        try: self._show_sync_button(False)
        except AttributeError: pass   # fired by the trace before _build_ui ran

    # ── Log helpers ─────────────────────────────────────────────────────────

    def _print(self, msg, tag=""):
        self.log.config(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
        self.log.see("end"); self.log.config(state="disabled")
        self.update_idletasks()

    def _start_log_tailer(self):
        TAG = {"error":"err","Error":"err","warn":"warn","Warn":"warn",
               "info":"info","Info":"info","debug":"debug","Debug":"debug"}
        def on_line(ts, lv, lg, msg):
            tag = TAG.get(lv,"info")
            short = lg.split(".")[-1] if lg else ""
            pre   = f"[{ts}]" + (f" [{short}]" if short else "")
            self.after(0, lambda: self._append_log(f"{pre} {msg}", tag))
        self._tailer = GameLogTailer(GAME_LOG_PATH, on_line)
        self._tailer.start()

    def _append_log(self, line, tag):
        self.log.config(state="normal")
        self.log.insert("end", line+"\n", tag)
        if float(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0","1000.0")
        self.log.see("end"); self.log.config(state="disabled")

    # ── Check Server (optional, informational) / Join Server ──────────────────

    def _popen_console_kwargs(self):
        """Hiding MelonLoader's console means redirecting the child's std
        handles at process-creation time — that's what makes it skip
        AllocConsole(). Showing it just means not touching them at all."""
        if self.v_show_melonloader.get():
            return {}
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

    def _on_join_clicked(self):
        if self._selected_kind == "headless":
            self._do_launch_headless()
        else:
            self._do_launch(password=None)

    def _do_check(self):
        if self._selected_kind == "headless":
            # No port-1762 auth gate exists on these — there's nothing to
            # check. Say so plainly instead of attempting a doomed connection
            # that would just read as a generic failure.
            self._check_status.set(
                "Direct-connect (headless) server — no health check available. "
                "Just press Join Server.")
            try: self._check_label.config(fg=MUTED)
            except: pass
            return
        ip   = self.v_ip.get().strip()
        host = ip if ip else "127.0.0.1"
        self._check_btn.config(state="disabled")
        self._check_status.set(f"Checking {host}…")
        try: self._check_label.config(fg=MUTED)
        except: pass
        threading.Thread(target=self._run_check, args=(host,), daemon=True).start()

    def _run_check(self, host):
        try:
            resp, ms = ping_server(host)
            if resp.get("status") == "pong":
                sv_name = resp.get("server_name", host)
                pw_req  = resp.get("password_required", False)
                wl      = resp.get("whitelist_enabled", False)
                game_port = resp.get("game_port")
                mods_count = resp.get("mods_count")
                lines   = [f"✔  {sv_name}  —  {ms} ms"]
                flags   = []
                if pw_req: flags.append("🔒 Password required")
                if wl:     flags.append("📋 Whitelist active")
                if game_port: flags.append(f"Port {game_port}")
                if mods_count is not None:
                    flags.append(f"🧩 {mods_count} mod{'s' if mods_count != 1 else ''}")
                if flags:  lines.append("  ".join(flags))
                msg = "\n".join(lines)
                self.after(0, lambda: self._check_ok(host, msg, game_port, mods_count))
            else:
                self.after(0, lambda: self._check_fail(f"Unexpected response from {host}"))
        except Exception as e:
            self.after(0, lambda e=e: self._check_fail(f"✘  Cannot reach server — {e}"))

    def _check_ok(self, host, msg, game_port=None, mods_count=None):
        self._server_ok    = True
        self._checked_host = host
        self._check_status.set(msg)
        self._check_label.config(fg=GREEN)
        self._check_btn.config(state="normal")
        # Offered whenever the server answered with a mod count at all, zero
        # included: a server running no mods still means anything currently
        # active here gets deactivated on join, which is a change worth being
        # able to see and apply up front.
        self._show_sync_button(mods_count is not None)
        # The server just told us its actual configured port — trust that
        # over whatever was already in the field, since it's the ground truth.
        if game_port:
            self.v_port.set(str(game_port))

    def _check_fail(self, msg):
        self._server_ok    = False
        self._checked_host = None
        self._check_status.set(msg)
        self._check_label.config(fg=RED)
        self._check_btn.config(state="normal")
        self._show_sync_button(False)

    def _show_sync_button(self, show):
        """Sync Mods only makes sense against a server we just successfully
        checked, so it appears with a good result and goes away the moment that
        result is cleared or replaced."""
        if show:
            self._sync_btn.pack(side="right", padx=(8,0))
        else:
            self._sync_btn.pack_forget()

    # ── Launch ────────────────────────────────────────────────────────────────

    def _try_headless_fallback(self, display_host, resolved_host):
        """Only reached when the normal auth service at port 1762 couldn't
        be contacted at all. Checks the community list for a *currently
        registered* headless server at this address, and only proceeds with
        an unauthenticated join if that's confirmed — this is a fallback
        for a known, already-vouched-for server, never a way to silently
        skip auth for an address that just happens to be unreachable or
        misconfigured. Tries the exact string the player typed first (in
        case it matches a verified hostname), then the resolved IP (in case
        they typed a hostname that isn't what the server registered with,
        but happens to point at the same place)."""
        candidates = [display_host]
        if resolved_host and resolved_host != display_host:
            candidates.append(resolved_host)

        for address in candidates:
            try:
                params = urlencode({"address": address})
                req = urllib.request.Request(f"{COMMUNITY_API}/lookup?{params}",
                    headers={"User-Agent": "TavernLauncher/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as e:
                self._print(f"Could not check community list: {e}", "warn")
                continue

            if data.get("found") and data.get("kind") == "headless":
                self._print(f"'{address}' is a known headless server — "
                            "joining directly, no auth.", "warn")
                if data.get("port"):
                    self.v_port.set(str(data["port"]))
                self._selected_kind = "headless"
                self._do_launch_headless()
                return True

        return False

    # Pre-join mod reconciliation

    def _get_server_mods(self, host):
        """The server's full installed-mods list, for plan_join. Uses the
        ping/pong's mods_hash as a cache key (self._mods_list_cache) so
        rejoining a server whose mods haven't changed since the last check
        skips the extra "mods_list" round trip entirely. Returns [] if the
        server doesn't send mods_hash at all (an older TavernLib/launcher) -
        reconciliation is then a no-op, same as a server with no mods."""
        try:
            resp, _ms = ping_server(host)
        except Exception:
            return []
        mods_hash = resp.get("mods_hash")
        if not mods_hash:
            return []
        cached = self._mods_list_cache.get(host)
        if cached and cached[0] == mods_hash:
            return cached[1]
        try:
            mods = fetch_server_mods(host)
        except Exception as e:
            self._print(f"Could not fetch the server's mod list: {e}", "warn")
            return []
        self._mods_list_cache[host] = (mods_hash, mods)
        return mods

    # Sync Mods (the button beside the Check Server result)

    def _on_sync_mods(self):
        """Brings Mods/ in line with the server just checked, without launching
        the game, so the downloading can be done up front and the actual join is
        instant. Same plan/confirm/apply flow the join path runs; the only
        difference is that nothing launches at the end.

        Unlike that path, everything here runs on a worker thread: the player
        pressed a button and is watching the window, so resolving an index and
        pulling down mods must not freeze it."""
        host = self._checked_host
        if not host:
            return
        exe = self.v_exe.get().strip()
        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Game not found",
                "Please set the path to 'A Township Tale.exe' above first.", parent=self)
            return

        game_dir = os.path.dirname(exe)
        self._set_sync_busy(True)
        self._print(f"Checking which mods {host} needs…", "dim")

        def worker():
            try:
                kind, payload = self._build_mod_plan(game_dir, host)
            except Exception as e:
                self.after(0, lambda e=e: self._sync_failed(e))
                return
            self.after(0, lambda: self._sync_planned(game_dir, host, kind, payload))
        threading.Thread(target=worker, daemon=True).start()

    def _sync_planned(self, game_dir, host, kind, payload):
        """Back on the UI thread with a resolved plan: show the comparison, which
        then does the applying and reports the result in place.

        The window opens whatever the plan says. A blocking one disables Apply
        for itself, and seeing which mod is unavailable beside everything that's
        fine is what tells the player whether to add a source or ask the server
        owner; an all-matching one is the answer to the question they asked by
        pressing the button."""
        self._set_sync_busy(False)
        if kind == "skip":
            self._print(payload, "warn")
            return
        if kind == "error":
            self._print(f"Couldn't work out this server's mods: {payload}", "err")
            return

        plan, server_mods = payload
        self._open_mod_diff(plan, server_mods, game_dir, host, joining=False,
                            on_applied=self._sync_done,
                            on_cancel=lambda win: self._set_sync_busy(False))

    def _sync_done(self, win):
        self._set_sync_busy(False)
        self._print("Mods are now in sync with the server.", "ok")
        win.apply_finished(True, "Your mods now match this server, so joining "
                                 "won't need to download anything.")

    def _sync_failed(self, err):
        self._set_sync_busy(False)
        self._print(f"Mod sync failed: {err}", "err")

    def _set_sync_busy(self, busy):
        """Sync, Check and Join all move the same Mods/ folder or depend on its
        state, so none of them may run while a sync is mid-flight."""
        state = "disabled" if busy else "normal"
        for btn in (self._sync_btn, self._check_btn, self._action_btn):
            btn.config(state=state)

    def _open_mod_diff(self, plan, server_mods, game_dir, host, joining,
                       on_applied=None, on_cancel=None):
        """Shows the full client-vs-server comparison, so nothing is installed,
        moved, or switched off without being seen first, and then hosts the
        applying too: pressing Apply turns the same table into a progress view
        and the window reports the outcome itself.

        Returns straight away. on_applied(window) runs once the render has
        finished, on the UI thread, and owns what happens next (verify, launch,
        or just say it's done). on_cancel(window) runs instead if the window
        closes without a successful apply. With neither, it's a read-only view
        of a plan that can't be applied.

        The window edits the plan before applying: a mod the server doesn't run
        at all is the player's to keep, and keeping it drops that id from
        plan.to_deactivate. So this plan object is the one to render."""
        rows = _modmanager.build_mod_diff(game_dir, server_mods, plan)
        label = f"{host}  ({'joining' if joining else 'sync only'})"
        action = None
        if on_applied is not None:
            action = lambda win: self._apply_mod_plan(win, game_dir, plan,
                                                      on_applied, host)
        return _modmanager.ModDiffWindow(
            self, rows, plan, server_label=label, apply_action=action,
            close_on_success=joining, on_cancel=on_cancel)

    def _apply_mod_plan(self, win, game_dir, plan, on_applied, host=None):
        """Renders the plan on a worker, feeding the window's progress view as it
        goes, then hands back to on_applied. Same work either path does; only
        what follows it differs."""
        self._set_sync_busy(True)
        if host:
            # Remember which recommended mods were turned off for this server
            # before any rendering, so the choice survives even if applying
            # fails partway. Written whole, so taking a choice back sticks too.
            try:
                _modmanager.set_declined(load_cfg(), host, win.declined)
            except Exception as e:
                self._print(f"Could not save your recommended-mod choices: {e}", "warn")

        def report(msg):
            self._print(msg, "dim")
            win.set_status(msg)

        def failed(err):
            self._set_sync_busy(False)
            self._print(f"Applying the mod changes failed: {err}", "err")
            win.apply_finished(False, f"Couldn't apply the changes: {err}")

        def worker():
            try:
                _modmanager.render_active_set(
                    game_dir, plan,
                    lambda m: self.after(0, lambda m=m: report(m)),
                    lambda i, s: self.after(0, lambda i=i, s=s: win.set_step(i, s)))
            except Exception as e:
                self.after(0, lambda e=e: failed(e))
                return
            self.after(0, lambda: on_applied(win))
        threading.Thread(target=worker, daemon=True).start()

    def _build_mod_plan(self, game_dir, host):
        """Shared front half of both mod-sync paths (the Sync Mods button and
        the pre-launch reconcile): cache what's installed, ask the server what
        it runs, and resolve that into a JoinPlan. Changes nothing on disk and
        prompts for nothing, so each caller decides for itself what a given
        outcome means. Safe to call off the UI thread.

        Returns (kind, payload):
          ("skip",  reason)               nothing to sync against, which is a
                                          normal outcome: MelonLoader/TavernLib
                                          aren't installed, the server reported
                                          no mods, or the index is unreachable
          ("error", message)              the server's mods couldn't be resolved
          ("plan",  (plan, server_mods))  a usable plan, possibly blocking
        """
        if not (_melonloader_installed(game_dir) and _tavernlib_installed(game_dir)):
            return "skip", ("Community mods need MelonLoader and TavernLib "
                            "installed before they can load. Install them from "
                            "the Mods window first.")

        # Cache whatever is in Mods/ right now, BEFORE anything below can
        # overwrite it. Without this, a mod installed straight to Mods/ (the
        # Community Mods window, or an older launcher with no cache at all) is
        # unknown to the cache, so rendering an active set that needs a
        # different version of it replaces the folder and destroys the only copy
        # of the version that was there - making a later switch back a
        # re-download instead of the file move the cache exists to guarantee.
        # Idempotent: an already-cached version is skipped.
        try:
            adopted = _modmanager.adopt_installed_mods(game_dir)
            if adopted:
                self._print(f"Cached {len(adopted)} already-installed mod(s): "
                            f"{', '.join(adopted)}", "dim")
        except Exception as e:
            self._print(f"Could not cache the currently-installed mods: {e}", "warn")

        server_mods = self._get_server_mods(host)
        if not server_mods:
            return "skip", ("This server didn't report any mods, so there's "
                            "nothing to sync.")

        cfg = load_cfg()
        repo_bases = _modmanager.list_repos(cfg)
        try:
            index = _modmanager.fetch_indexes(repo_bases)
        except Exception as e:
            self._print(f"Could not check the community mod index: {e}", "warn")
            return "skip", f"Couldn't reach the mod index: {e}"

        pinned = _modmanager.list_pinned(cfg)
        # Recommended mods this player has already turned down for this server
        # stay turned down, rather than being re-offered on every join.
        declined = _modmanager.list_declined(cfg, host)
        try:
            plan = _modmanager.plan_join(game_dir, server_mods, index, repo_bases,
                                         pinned, declined)
        except _modmanager.ModManagerError as e:
            return "error", str(e)
        return "plan", (plan, server_mods)

    def _verify_mod_parity(self, game_dir, server_mods):
        """Every mod the server requires of clients, checked against what's
        actually enabled here at EXACTLY the right version (the same rule the
        server applies on join). Returns the mismatches, empty when parity
        holds. Just a Mods/ scan, so it's cheap enough to run inline.

        Mods the server doesn't require are left out, matching
        ModParity.ValidateClient: the
        server won't refuse a join over one, so neither should this. Checking
        them here would block launches the server would have allowed, which is
        the whole point of the distinction."""
        required = {m["id"]: m["version"] for m in server_mods
                    if m.get("client_side")
                    and _modmanager.parity_required(m)}
        _, _, installed_mods = _modmanager.handshake_snapshot(game_dir)
        have = {m["id"]: m["version"] for m in installed_mods}
        return [(mid, ver, have.get(mid)) for mid, ver in required.items()
                if have.get(mid) != ver]

    def _reconcile_mods_async(self, host, exe, on_ready):
        """Brings Mods/ in line with this server and then calls on_ready() to
        continue the launch, or never calls it if the launch shouldn't proceed
        (having already said why and re-enabled the buttons).

        Continuation-passing rather than a blocking call, because the slow parts
        here are a repo index fetch and potentially hundreds of megabytes of
        downloads. Run inline they would freeze the window for the whole of it,
        with no redraw and no way to cancel; the download helper's own wall-clock
        cap is 30 minutes, so that freeze is not theoretical. The two slow steps
        run on workers and everything that touches Tk hops back via after().

        The order is fixed: resolve, show the comparison, apply, then verify.
        Verification is deliberately not skipped even when the render reported
        success, since an accepted pin conflict or a fail-soft render can still
        leave a mismatch that would otherwise only surface as an in-game
        rejection."""
        game_dir = os.path.dirname(exe)
        self._set_sync_busy(True)

        def abort(msg=None, tag="err"):
            if msg:
                self._print(msg, tag)
            self._set_sync_busy(False)

        def verified(server_mods, win=None):
            """Parity check after the render. A mismatch here is reported in the
            comparison window when there is one, so the player is looking at the
            table the rows came from rather than a detached error box."""
            mismatched = self._verify_mod_parity(game_dir, server_mods)
            if mismatched:
                lines = "; ".join(f"{mid} needs {ver}, have {have_ver or 'nothing'}"
                                  for mid, ver, have_ver in mismatched)
                self._print(f"Mods still don't match this server ({lines}); "
                            "joining would be rejected, not launching.", "err")
                abort()
                if win is not None:
                    win.apply_finished(False,
                        "Your mods still don't match this server: " + lines +
                        ". Joining would be rejected, so the game wasn't launched.")
                return
            if win is not None:
                win.apply_finished(True)      # closes it, the launch follows
            self._set_sync_busy(False)
            on_ready()

        def planned(kind, payload):
            if kind == "skip":
                self._set_sync_busy(False)
                on_ready()                    # fail open, never block a join
                return
            if kind == "error":
                abort(f"Could not resolve this server's required mods: {payload}")
                return

            plan, server_mods = payload
            if plan.blocking:
                # Show the comparison rather than a bare error: seeing which mod
                # is unavailable, beside everything that is fine, is what tells
                # the player whether to add a source or ask the server owner.
                # No apply_action, so this is a read-only view and can't continue.
                self._print("This server needs mods that aren't in any source "
                            "you've added; not launching.", "err")
                abort()
                self._open_mod_diff(plan, server_mods, game_dir, host, joining=True)
                return

            changed = [e for e in plan.entries if not e.active]
            if not (changed or plan.to_deactivate or plan.needs_repo or plan.pin_conflicts):
                verified(server_mods)         # already correct, nothing to apply
                return

            def cancelled(win):
                # A close after a failed apply has already been explained, both
                # in the log and in the window itself; only a close with no
                # attempt behind it is a decline.
                if win.apply_attempted:
                    self._set_sync_busy(False)
                    return
                abort("Mod changes declined; not launching.", "warn")

            # Hand over to the window: it applies, shows progress, and calls back
            # here when the render is done. Buttons come back on if it's closed
            # without applying, which is also what stops the launch.
            self._set_sync_busy(False)
            self._open_mod_diff(
                plan, server_mods, game_dir, host, joining=True,
                on_applied=lambda win: verified(server_mods, win),
                on_cancel=cancelled)

        def worker():
            try:
                kind, payload = self._build_mod_plan(game_dir, host)
            except Exception as e:
                self.after(0, lambda e=e: abort(f"Could not check this server's mods: {e}"))
                return
            self.after(0, lambda: planned(kind, payload))
        threading.Thread(target=worker, daemon=True).start()

    def _do_launch(self, password, _token_state=None):
        exe      = self.v_exe.get().strip()
        username = self.v_username.get().strip()
        platform = self.v_platform.get()
        platform = PLATFORM_DISPLAY_TO_BACKEND.get(platform, platform)
        ip       = self.v_ip.get().strip()
        display_host = ip if ip else "127.0.0.1"
        # Resolved once and used everywhere identity-sensitive matters (token
        # lookup, the auth handshake, and the game's own launch arg) — a
        # server reachable by both a hostname and its IP is still one server,
        # and needs to be treated as one for token purposes. Without this,
        # joining once via "myserver.com" and later via its raw IP would look
        # like two different servers locally, generate two different tokens,
        # and get rejected as "that name is taken by someone else" the second
        # time — even though it's the same account on the same server.
        host = _resolve_ip_for_game(display_host)

        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Not found",
                "Could not find the game.\nPlease browse to 'A Township Tale.exe'.", parent=self)
            return
        if not username:
            messagebox.showerror("Missing name",
                "Please enter your username before connecting.", parent=self)
            return
        if len(username) > USERNAME_MAX_LEN:
            messagebox.showerror("Name too long",
                f"Usernames can be at most {USERNAME_MAX_LEN} characters.", parent=self)
            return
        if not _is_valid_username(username):
            messagebox.showerror("Invalid name",
                "Usernames can only contain letters, numbers, spaces, hyphens, and underscores.", parent=self)
            return

        self._save()
        self._action_btn.config(state="disabled")
        self._print(f"Authenticating '{username}'…", "dim")

        # Resolve (and, on first contact with this server, create) the token
        # once per launch attempt so a password retry doesn't regenerate it.
        if _token_state is None:
            had_token_before = _any_token_files_exist()
            token, token_is_new = _get_or_create_token(host, username)
        else:
            token, token_is_new, had_token_before = _token_state

        user_id, error = authenticate(host, username, token, password=password)

        if error == "NEEDS_PASSWORD":
            self._action_btn.config(state="normal")
            pw = simpledialog.askstring("Password Required",
                "This server requires a password:", show="*", parent=self)
            if pw: self._do_launch(password=pw,
                                    _token_state=(token, token_is_new, had_token_before))
            return

        if error and error.startswith("CANNOT_REACH::"):
            detail = error.split("::", 1)[1]
            self._print(f"No official auth service at {host}:{AUTH_PORT} — "
                        "checking community list for a headless registration…", "warn")
            if self._try_headless_fallback(display_host, host):
                return
            self._print(f"Rejected: Cannot reach server at {host}:{AUTH_PORT} — {detail}", "err")
            self._action_btn.config(state="normal")
            return

        if error:
            self._print(f"Rejected: {error}", "err")
            self._action_btn.config(state="normal")
            return

        self._print(f"Welcomed as {username} (ID {user_id})", "ok")
        self._show_token_button()
        if token_is_new and not had_token_before:
            # The very first token file this launcher has ever created on this
            # machine — open the explainer immediately instead of waiting for a click.
            self._on_token_button_click()

        # Record in recent — use server_name from last ping if available
        cfg = load_cfg()
        sv_name = display_host
        status_text = self._check_status.get()
        if status_text.startswith("✔"):
            # Parse the server name out of the status line "✔  ServerName  —  Xms"
            try: sv_name = status_text.split("✔")[1].split("—")[0].strip()
            except: pass
        recent = [r for r in cfg.get("recent_servers",[]) if r.get("ip") != display_host]
        recent.insert(0, {"name": sv_name, "ip": display_host, "port": self.v_port.get()})
        cfg["recent_servers"] = recent[:20]
        save_cfg(cfg)

        # Mod reconciliation can download hundreds of megabytes, so it runs off
        # the UI thread and calls back only when the launch should go ahead.
        # Everything above this point is quick.
        self._reconcile_mods_async(host, exe, lambda: self._launch_game(
            exe, ip, host, platform, username, user_id, token,
            lambda: self._do_launch(password=None)))

    def _launch_game(self, exe, ip, host, platform, username, user_id, token, relaunch):
        """Builds the tokens and starts the game. The shared tail of both join
        paths, which differ only in the token they carry (the auth flow has a
        real one, the direct flow has none) and where a rejection should rejoin
        to. Runs on the UI thread and returns immediately: everything slow has
        already finished by the time anything reaches here."""
        # Report what's actually enabled in Mods/ (after the render) so the
        # server's exact-version parity check has something to compare against;
        # see build_tokens' mods_claim.
        mods_claim = ""
        try:
            _, _, mods_list = _modmanager.handshake_snapshot(os.path.dirname(exe))
            mods_claim = json.dumps({m["id"]: m["version"] for m in mods_list})
        except Exception as e:
            self._print(f"Could not read installed mods for the join handshake: {e}", "warn")

        access, refresh, identity = build_tokens(user_id, username, token, mods_claim)
        args = [exe, "/force_offline",
                "/access_token", access, "/refresh_token", refresh,
                "/identity_token", identity, "/join_local_server"]

        if platform == "none":
            args.insert(-1, "/fly")
        elif platform:
            args[-1:] = ["/vrmode", platform, "/join_local_server"]
        if ip:
            # Already resolved to a canonical IP by the caller, the same value
            # used for the token lookup and the auth handshake, so all three
            # agree on which server this is.
            args += ["/dev_server_ip", host]
        args += ["/dev_server_port", str(_valid_port(self.v_port.get()))]
        if self.v_debug_helper.get():
            args.append("/debug_helper")

        # Delete any stale rejection file before launching, so a file present
        # after THIS process exits was written by THIS attempt (the actual
        # staleness guard is that we wait for this exact process and nothing
        # else runs in between; see _watch_for_rejection).
        _delete_last_rejection_file()

        self._print(f"Launching on {platform or 'default'}…", "warn")
        try:
            # Whether MelonLoader's own console window shows up is controlled
            # by the Show MelonLoader toggle: hiding it means redirecting the
            # child's std handles at creation time (which is what makes it
            # skip AllocConsole); showing it means leaving them alone.
            proc = subprocess.Popen(args, cwd=os.path.dirname(exe),
                                    **self._popen_console_kwargs())
            self._print(f"Game running (PID {proc.pid})", "ok")
            threading.Thread(target=self._watch_for_rejection,
                             args=(proc, host, relaunch), daemon=True).start()
        except Exception as e:
            self._print(f"Launch failed: {e}", "err")
        self._action_btn.config(state="normal")

    def _watch_for_rejection(self, proc, host, relaunch):
        """Runs on a background thread once the game process is launched.
        Waits for it to exit, then checks for TavernLib's
        last_rejection.json, written by this same client's own TavernLib
        instance right before exiting, if (and only if) the server denied the
        join for a mod-mismatch reason. The launch path already deleted any
        stale copy before this process started, so a file present now was
        written by this exact attempt.

        relaunch is the caller's own way back in, so recovery rejoins via the
        path the player actually used - _do_launch for the auth-gated flow,
        _do_launch_headless for the direct one. Hardcoding _do_launch here
        would send a headless join through an auth handshake its server never
        answers."""
        proc.wait()
        path = _last_rejection_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self.after(0, lambda: self._print(f"Could not read the rejection file: {e}", "warn"))
            return
        finally:
            try: os.remove(path)   # consumed either way, never re-act on it twice
            except OSError: pass

        if payload.get("schema") != 1:
            return
        server = payload.get("server") or {}
        # Extra cross-check beyond the delete-before-launch guard above: a
        # host mismatch means this file wasn't actually about the server we
        # just tried to join.
        if server.get("host") and server.get("host") != host:
            return
        missing = payload.get("missing") or []
        if not missing:
            return
        self.after(0, lambda: self._offer_rejection_recovery(payload, relaunch))

    def _offer_rejection_recovery(self, payload, relaunch):
        """The server rejected the join over exact mods the client didn't
        have. Offers to fetch exactly those (and their dependencies), same
        confirm-before-install and never-auto-add-a-repo rules as everywhere
        else, then rejoins.

        Only the prompt runs here, on the UI thread; the index fetch and the
        downloads go to a worker (see below), same as every other install path
        in this file. Doing them inline would freeze the whole launcher for the
        length of a download - up to the 30-minute wall-clock cap - with no
        window redraw and no way to cancel."""
        missing = payload.get("missing") or []
        names = "\n".join(f"  • {m['id']} {m['version']}" for m in missing)
        if not messagebox.askyesno("Mods needed to rejoin",
                f"That server rejected the join because these mods didn't "
                f"match:\n\n{names}\n\nInstall the exact versions it needs "
                f"and try rejoining?", parent=self):
            return

        exe = self.v_exe.get().strip()
        game_dir = os.path.dirname(exe)
        self._action_btn.config(state="disabled")
        self._print("Recovering the missing mods…", "warn")

        def worker():
            try:
                cfg = load_cfg()
                repo_bases = _modmanager.list_repos(cfg)
                index = _modmanager.fetch_indexes(repo_bases)
                roots, dependencies = _modmanager.resolve_missing_mods(missing, index, repo_bases)
                libraries = _modmanager.collect_library_dependencies(roots + dependencies)
                for mod in roots + dependencies:
                    _modmanager.install_mod(
                        game_dir, mod,
                        lambda m, _i=mod: self.after(0, lambda: self._print(
                            f"{_i.id}: {m}", "dim")))
                for lib in libraries:
                    _modmanager.install_library_dependency(
                        game_dir, lib,
                        lambda m, _l=lib: self.after(0, lambda: self._print(
                            f"{_l.filename}: {m}", "dim")))
            except Exception as e:
                self.after(0, lambda e=e: self._recovery_failed(e))
                return
            self.after(0, lambda: self._recovery_done(relaunch))

        threading.Thread(target=worker, daemon=True).start()

    def _recovery_failed(self, err):
        self._action_btn.config(state="normal")
        messagebox.showerror("Recovery failed", str(err), parent=self)

    def _recovery_done(self, relaunch):
        self._action_btn.config(state="normal")
        self._print("Recovered the missing mods, rejoining…", "ok")
        relaunch()

    def _do_launch_headless(self):
        """Same launch as _do_launch, minus the port-1762 auth handshake, for
        servers hosted directly via the game itself with no auth gate.
        user_id has no server to come from here, so it's derived locally
        instead (see _headless_user_id); everything after that point is
        identical to the official flow.

        Mod reconciliation still runs, because it is a separate concern from
        auth. TavernLib's join-time parity check (PlayerJoinFilter) is a
        Harmony patch on the join pipeline, not part of the auth service, so it
        applies to this path exactly as it does to the official one: a server
        reached this way still rejects a client whose mods don't match. Skipping
        the mod work here would therefore mean presenting an empty mods claim to
        a server that requires mods and being rejected every single time, with
        no rejection watcher running to recover from it either. If the host
        serves no mod info at all, _reconcile_mods_async is a no-op."""
        exe      = self.v_exe.get().strip()
        username = self.v_username.get().strip()
        platform = self.v_platform.get()
        platform = PLATFORM_DISPLAY_TO_BACKEND.get(platform, platform)
        ip       = self.v_ip.get().strip()
        host     = ip if ip else "127.0.0.1"

        if not exe or not os.path.isfile(exe):
            messagebox.showerror("Not found",
                "Could not find the game.\nPlease browse to 'A Township Tale.exe'.", parent=self)
            return
        if not username:
            messagebox.showerror("Missing name",
                "Please enter your username before connecting.", parent=self)
            return
        if len(username) > USERNAME_MAX_LEN:
            messagebox.showerror("Name too long",
                f"Usernames can be at most {USERNAME_MAX_LEN} characters.", parent=self)
            return
        if not _is_valid_username(username):
            messagebox.showerror("Invalid name",
                "Usernames can only contain letters, numbers, spaces, hyphens, and underscores.", parent=self)
            return

        self._save()
        self._action_btn.config(state="disabled")
        self._print(f"Joining headless server directly (no auth gate) as '{username}'…", "warn")

        user_id = _headless_user_id(username)

        cfg = load_cfg()
        recent = [r for r in cfg.get("recent_servers",[]) if r.get("ip") != host]
        recent.insert(0, {"name": host, "ip": host, "port": self.v_port.get()})
        cfg["recent_servers"] = recent[:20]
        save_cfg(cfg)

        # Same resolution the /dev_server_ip arg below uses, so the host the
        # reconcile and the rejection watcher key off is the one the game
        # actually connects to (TavernLib records the resolved address in the
        # rejection payload, and _watch_for_rejection cross-checks against it).
        resolved_host = _resolve_ip_for_game(host)
        # Off the UI thread, same as the auth-gated path. No token to carry
        # here, and a rejection rejoins through this same direct flow.
        self._reconcile_mods_async(resolved_host, exe, lambda: self._launch_game(
            exe, ip, resolved_host, platform, username, user_id, "",
            self._do_launch_headless))


if __name__ == "__main__":
    if _updater is not None:
        _updater.finish_update_if_requested()  # never returns if this launch is finishing an update
        _updater.cleanup_previous_update()
    app = ClientLauncher()
    app.mainloop()
