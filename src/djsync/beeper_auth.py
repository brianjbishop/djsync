"""OAuth dynamic client registration against local Beeper Desktop."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

from djsync.config import get_beeper_settings

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"


def _fetch_json(url: str, *, method: str = "GET", data: dict | None = None) -> Any:
    body = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def _spec_paths(spec: dict[str, Any]) -> dict[str, str]:
    """Extract OAuth and API paths from /v1/spec (shape varies by version)."""
    paths: dict[str, str] = {}
    raw_paths = spec.get("paths") or spec.get("routes") or {}
    if isinstance(raw_paths, dict):
        for path in raw_paths:
            norm = str(path)
            if "register" in norm:
                paths.setdefault("register", norm)
            elif "authorize" in norm:
                paths.setdefault("authorize", norm)
            elif "token" in norm:
                paths.setdefault("token", norm)
    for key, default in (
        ("register", "/oauth/register"),
        ("authorize", "/oauth/authorize"),
        ("token", "/oauth/token"),
    ):
        paths.setdefault(key, spec.get(f"oauth_{key}") or default)
    return paths


def _wait_for_code(redirect_uri: str) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    result: dict[str, str | None] = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in query:
                result["code"] = query["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><p>Authorized. You can close this tab.</p></body></html>"
                )
            else:
                result["error"] = query.get("error", ["unknown"])[0]
                self.send_response(400)
                self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer((host, port), Handler)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    if result["error"]:
        raise RuntimeError(f"OAuth authorization failed: {result['error']}")
    if not result["code"]:
        raise RuntimeError("OAuth authorization timed out waiting for callback")
    return str(result["code"])


def run_beeper_auth(*, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    """Perform OAuth against local Beeper Desktop and return the access token."""
    base_url, _, _ = get_beeper_settings()
    base = base_url.rstrip("/")

    try:
        _fetch_json(f"{base}/v1/info")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(
            "Beeper Desktop is not running. Start Beeper Desktop and retry."
        ) from exc

    info = _fetch_json(f"{base}/v1/info")
    oauth = ((info or {}).get("endpoints") or {}).get("oauth") or {}

    # /v1/info publishes the OAuth endpoints authoritatively. Deriving them by
    # scanning /v1/spec picked /v1/app/setup/register - a different endpoint
    # that answers 409. Fall back to spec-scanning only if info is missing them.
    spec = _fetch_json(f"{base}/v1/spec")
    paths = _spec_paths(spec if isinstance(spec, dict) else {})

    register_url = oauth.get("registration_endpoint") or f"{base}{paths['register']}"
    client_name = "djsync"
    register_body: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    for alt_key in ("application_type",):
        if alt_key in (spec.get("oauth_register_fields") or []):
            register_body["application_type"] = "native"

    reg = _fetch_json(register_url, method="POST", data=register_body)
    client_id = reg.get("client_id") or reg.get("clientId")
    if not client_id:
        raise RuntimeError("Beeper OAuth registration did not return a client_id")

    state = secrets.token_urlsafe(16)
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    authorize_base = oauth.get("authorization_endpoint") or f"{base}{paths['authorize']}"
    authorize_url = f"{authorize_base}?{urllib.parse.urlencode(auth_params)}"
    print("Open this URL in your browser to authorize djsync:", flush=True)
    print(authorize_url, flush=True)
    try:
        webbrowser.open(authorize_url)
    except OSError:
        pass

    code = _wait_for_code(redirect_uri)

    token_url = (oauth.get("token_endpoint") or f"{base}{paths['token']}")
    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    token_resp = _fetch_json(token_url, method="POST", data=token_body)
    token = token_resp.get("access_token") or token_resp.get("accessToken")
    if not token:
        raise RuntimeError("Beeper OAuth token exchange did not return an access_token")
    return str(token)


def print_token_instructions(token: str) -> None:
    """Print setup instructions without logging the token value."""
    print("\nAdd this line to your .env file:", flush=True)
    print("DJSYNC_BEEPER_TOKEN=<paste the token shown below>", flush=True)
    print("\n--- copy from the next line ---", flush=True)
    print(f"DJSYNC_BEEPER_TOKEN={token}", flush=True)
    print("--- copy to the previous line ---", flush=True)
