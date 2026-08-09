"""
Auth-port protocol client (port 1762): login handshake, tickets, ping,
and the offline-mode JWT construction used to launch the game itself.
"""
import socket
import json
import time
import base64
import hashlib
import hmac as _hmac

AUTH_PORT   = 1762


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
        # No auth service reachable at all (vs. a real rejection) --
        # _do_launch uses this to decide whether a headless fallback is worth trying.
        return None, f"CANNOT_REACH::{e}", False
    status = resp.get("status")
    if status == "ok":
        return resp.get("user_id"), None, bool(resp.get("quest_scene_required", False))
    if status == "needs_password": return None, "NEEDS_PASSWORD", False
    if status == "wrong_password": return None, "Wrong password.", False
    if status == "not_whitelisted": return None, "NOT_WHITELISTED", False
    return None, resp.get("message", "Authentication failed."), False


def register_whitelist_application(host, username, timeout=8):
    """Sends a register_whitelist_application request to TavernLib's own
    AuthManager (port 1762) -- this works against ANY server running
    TavernLib, headless or launcher-managed, since TavernLib itself (not
    this launcher) is what writes whitelist_requests.json. The server's
    own IP-detection is authoritative; the client never sends its own IP.
    Returns (was_new, error) -- was_new is False if this exact
    username+IP already had a pending application."""
    payload = {"register_whitelist_application": True, "username": username}
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, AUTH_PORT))
        s.sendall(json.dumps(payload).encode())
        raw = s.recv(4096)
        s.close()
        resp = json.loads(raw.decode())
    except Exception as e:
        return False, str(e)
    if resp.get("status") != "whitelist_application_received":
        return False, resp.get("message", "Unexpected response from server.")
    return (not resp.get("already_pending", False)), None


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
    mod as {"id", "version", "client_side", "server_side", "parity_required",
    "source_repo"} -- the same six fields handshake_snapshot builds and
    TavernLib's ModHandshake.Entry serialises, so both kinds of server answer
    this identically. parity_required decides whether a client must match a mod
    or may decline it, and source_repo is a hint only (never resolved into a
    pull on its own); omitting either from a caller's expectations turns a
    "recommended" mod into a hard join failure.
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
    community mod currently enabled on this machine, the client's own side of
    TavernLib's exact-version mod-parity check (PlayerJoinFilter /
    ModParity.ValidateClient), read off the same "TavernMods" claim. Empty
    string when there's nothing to report (no mods enabled, or the caller
    didn't compute one); a server with no client_side-required mods ignores it
    either way.

    Both TavernToken and TavernMods go on the identity token as well as the
    access token, because the identity one is what the game actually sends as
    RequestJoinMessage.UserCredentials - the only token a server ever reads
    these off. On the access token alone the claim never arrives, the server
    sees a client with no mods, and every mod it requires of clients comes
    back as a mismatch no matter what's installed."""
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


HEADLESS_USER_ID_BASE  = 1_000_000_000


HEADLESS_USER_ID_RANGE = 999_999_999


def _headless_user_id(username):
    h = int(hashlib.sha256(username.strip().lower().encode()).hexdigest(), 16)
    return HEADLESS_USER_ID_BASE + (h % HEADLESS_USER_ID_RANGE)

