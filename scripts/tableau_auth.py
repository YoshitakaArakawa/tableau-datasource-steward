"""OAuth (PKCE) authentication helper for tableau-datasource-steward skills.

Loads .env from the nearest ancestor directory, runs OAuth 2.0
Authorization Code + PKCE flow against Tableau Cloud, injects the
resulting access_token into a tableauserverclient Server instance,
and yields it as a context manager.

Required .env variables: SERVER, SITE_NAME
Optional .env variables: OAUTH_CALLBACK_PORT (default 8765)

Usage:

    from tableau_auth import signed_in_server
    with signed_in_server() as server:
        for flow in TSC.Pager(server.flows):
            ...
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

try:
    import tableauserverclient as TSC
except ImportError:
    sys.exit("ERROR: tableauserverclient is required. Install with: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("ERROR: python-dotenv is required. Install with: pip install -r requirements.txt")


CLIENT_TYPE = "tableau-datasource-steward"
USER_AGENT = "tableau-datasource-steward/0.1 (python)"
DEFAULT_CALLBACK_PORT = 8765
OAUTH_TIMEOUT_SECONDS = 300


def find_env_file(start: Path | None = None) -> Path | None:
    """Look for .env in start (or cwd) and ancestors (up to 6 levels)."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(6):
        candidate = cur / ".env"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def load_credentials() -> dict:
    env_path = find_env_file()
    if env_path:
        load_dotenv(env_path)
        print(f"[auth] Loaded .env from: {env_path}", file=sys.stderr)
    else:
        print("[auth] WARNING: No .env file found. Relying on environment only.", file=sys.stderr)

    server_url = (os.environ.get("SERVER") or "").rstrip("/")
    if not server_url:
        sys.exit("ERROR: Missing required env var: SERVER")

    return {
        "server_url": server_url,
        "site_name":  os.environ.get("SITE_NAME", ""),
        "port":       int(os.environ.get("OAUTH_CALLBACK_PORT", DEFAULT_CALLBACK_PORT)),
    }


# ----- PKCE -----
def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


# ----- Local callback listener -----
class _CallbackResult:
    def __init__(self):
        self.code: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_callback_handler(expected_state: str, result: _CallbackResult):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/Callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            received_state = (query.get("state") or [None])[0]
            if received_state != expected_state:
                result.error = f"state mismatch: expected {expected_state!r}, got {received_state!r}"
            else:
                result.code = (query.get("code") or [None])[0]
                if "error" in query:
                    result.error = (query.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>OAuth callback received.</h2>"
                b"<p>You can close this tab and return to your terminal.</p></body></html>"
            )
            result.event.set()

        def log_message(self, *_args, **_kwargs):
            return

    return Handler


# ----- raw HTTP helpers (stdlib only) -----
def _http_post_form(url: str, body: dict[str, str], timeout: int = 30) -> dict:
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise SystemExit(f"[auth] ERROR: POST {url} -> HTTP {e.code}\nbody: {body_text}")


def _http_get_json(url: str, headers: dict[str, str], timeout: int = 30) -> dict:
    req = urllib.request.Request(url=url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise SystemExit(f"[auth] ERROR: GET {url} -> HTTP {e.code}\nbody: {body_text}")


def _probe_session(server_url: str, api_version: str, access_token: str) -> dict | None:
    """Best-effort liveness check. Returns sessions/current JSON on 200, None otherwise."""
    req = urllib.request.Request(
        url=f"{server_url}/api/{api_version}/sessions/current",
        method="GET",
        headers={
            "Accept": "application/json",
            "X-Tableau-Auth": access_token,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


# ----- Session cache (repo-internal, gitignored) -----
def _cache_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".auth-cache" / "session.json"


def _load_cached_session(server_url: str, site_name: str, api_version: str) -> dict | None:
    """Return {access_token, user_id} if cache matches (server, site) and token is still alive."""
    path = _cache_path()
    if not path.exists():
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[auth] cache unreadable, ignoring: {e}", file=sys.stderr)
        return None
    if cache.get("server_url") != server_url or cache.get("site_name") != site_name:
        return None
    access_token = cache.get("access_token")
    if not access_token:
        return None
    session = _probe_session(server_url, api_version, access_token)
    if session is None:
        print("[auth] cached token rejected by server, will re-auth", file=sys.stderr)
        try:
            path.unlink()
        except OSError:
            pass
        return None
    user_id = session["session"]["user"]["id"]
    print(f"[auth] reused cached session (user_id={user_id})", file=sys.stderr)
    return {"access_token": access_token, "user_id": user_id}


def _save_session(server_url: str, site_name: str, access_token: str) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {
        "server_url": server_url,
        "site_name": site_name,
        "access_token": access_token,
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    print(f"[auth] cached session at {path}", file=sys.stderr)


# ----- OAuth authorization_code (PKCE) flow -----
def _run_oauth_flow(server_url: str, site_name: str, port: int) -> str:
    """Drive PKCE browser sign-in. Returns access_token (3-part `id1|id2|site-luid`)."""
    redirect_uri = f"http://127.0.0.1:{port}/Callback"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    client_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())

    result = _CallbackResult()
    handler_cls = _make_callback_handler(state, result)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[auth] callback listener: http://127.0.0.1:{port}/Callback", file=sys.stderr)

    auth_params = {
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "device_id": device_id,
        "device_name": "tableau-datasource-steward (python)",
        "target_site": site_name,
        "client_type": CLIENT_TYPE,
    }
    auth_url = f"{server_url}/oauth2/v1/auth?" + urllib.parse.urlencode(auth_params)
    print("[auth] opening browser for sign-in...", file=sys.stderr)
    webbrowser.open(auth_url)

    print(f"[auth] waiting for callback (up to {OAUTH_TIMEOUT_SECONDS}s)...", file=sys.stderr)
    received = result.event.wait(timeout=OAUTH_TIMEOUT_SECONDS)
    httpd.shutdown()
    if not received:
        raise SystemExit("[auth] ERROR: timeout waiting for OAuth callback")
    if result.error or not result.code:
        raise SystemExit(f"[auth] ERROR: callback error: {result.error!r}")

    token = _http_post_form(
        f"{server_url}/oauth2/v1/token",
        {
            "grant_type": "authorization_code",
            "code": result.code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },
    )
    access_token = token["access_token"]
    print(f"[auth] access_token acquired (expires_in={token.get('expires_in')})", file=sys.stderr)
    return access_token


def _derive_site_luid(access_token: str) -> str:
    parts = access_token.split("|")
    if len(parts) != 3:
        raise SystemExit(
            f"[auth] ERROR: unexpected access_token shape (expected 3 parts, got {len(parts)})"
        )
    return parts[2]


def _fetch_user_id(server_url: str, api_version: str, access_token: str) -> str:
    session = _http_get_json(
        f"{server_url}/api/{api_version}/sessions/current",
        headers={
            "Accept": "application/json",
            "X-Tableau-Auth": access_token,
            "User-Agent": USER_AGENT,
        },
    )
    return session["session"]["user"]["id"]


@contextlib.contextmanager
def signed_in_server():
    """Yield a TSC.Server bound to a Tableau Cloud access_token.

    Reuses a cached token from <repo>/.auth-cache/session.json when one is alive;
    otherwise runs OAuth (PKCE) browser sign-in and caches the result. Does NOT
    sign out on exit — call `python scripts/tableau_auth.py logout` to invalidate.
    """
    creds = load_credentials()
    server_url = creds["server_url"]
    site_name = creds["site_name"]
    port = creds["port"]

    # use_server_version=True fetches /api/3.0/serverinfo unauthenticated
    # to resolve the highest supported REST API version.
    server = TSC.Server(server_url, use_server_version=True)
    api_version = server.version

    cached = _load_cached_session(server_url, site_name, api_version)
    if cached:
        access_token = cached["access_token"]
        user_id = cached["user_id"]
        site_luid = _derive_site_luid(access_token)
    else:
        access_token = _run_oauth_flow(server_url, site_name, port)
        site_luid = _derive_site_luid(access_token)
        user_id = _fetch_user_id(server_url, api_version, access_token)
        _save_session(server_url, site_name, access_token)

    server._set_auth(site_luid, user_id, access_token, site_url=site_name)
    print(
        f"[auth] signed in: site_name={site_name!r} site_luid={site_luid} user_id={user_id}",
        file=sys.stderr,
    )

    yield server


# ----- CLI: logout / status -----
def _cli_logout() -> int:
    path = _cache_path()
    if not path.exists():
        print("[auth] no cached session to remove")
        return 0
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        cache = None
    if cache:
        server_url = cache.get("server_url")
        access_token = cache.get("access_token")
        if server_url and access_token:
            try:
                tmp_server = TSC.Server(server_url, use_server_version=True)
                api_version = tmp_server.version
                req = urllib.request.Request(
                    url=f"{server_url}/api/{api_version}/auth/signout",
                    method="POST",
                    headers={
                        "X-Tableau-Auth": access_token,
                        "User-Agent": USER_AGENT,
                    },
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
                print("[auth] server-side sign_out OK")
            except Exception as e:
                print(f"[auth] server-side sign_out best-effort failed: {e}")
    path.unlink()
    print(f"[auth] deleted cache: {path}")
    return 0


def _cli_status() -> int:
    path = _cache_path()
    print(f"cache path: {path}")
    if not path.exists():
        print("status: no cached session")
        return 0
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"status: unreadable ({e})")
        return 1
    server_url = cache.get("server_url")
    site_name = cache.get("site_name")
    access_token = cache.get("access_token")
    print(f"server_url: {server_url}")
    print(f"site_name: {site_name!r}")
    if not (server_url and access_token):
        print("status: malformed cache")
        return 1
    tmp_server = TSC.Server(server_url, use_server_version=True)
    session = _probe_session(server_url, tmp_server.version, access_token)
    if session is None:
        print("status: expired or invalid (next signed_in_server() will re-auth)")
        return 1
    user_id = session["session"]["user"]["id"]
    print(f"status: alive (user_id={user_id})")
    return 0


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("logout", "status"):
        print("usage: python scripts/tableau_auth.py {logout|status}", file=sys.stderr)
        return 2
    if argv[1] == "logout":
        return _cli_logout()
    return _cli_status()


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
