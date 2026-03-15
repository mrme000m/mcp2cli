"""OAuth support — token storage, callback handler, provider builder."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp2cli._cache import CACHE_DIR

OAUTH_DIR = CACHE_DIR / "oauth"


class FileTokenStorage:
    """File-based token storage for OAuth tokens and client info."""

    def __init__(self, server_url: str):
        key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        self._dir = OAUTH_DIR / key
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tokens_path = self._dir / "tokens.json"
        self._client_path = self._dir / "client.json"

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        if not self._tokens_path.exists():
            return None
        try:
            data = json.loads(self._tokens_path.read_text())
            return OAuthToken(**data)
        except Exception:
            return None

    async def set_tokens(self, tokens) -> None:
        self._tokens_path.write_text(tokens.model_dump_json())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        if not self._client_path.exists():
            return None
        try:
            data = json.loads(self._client_path.read_text())
            return OAuthClientInformationFull(**data)
        except Exception:
            return None

    async def set_client_info(self, client_info) -> None:
        self._client_path.write_text(client_info.model_dump_json())


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth authorization code callback."""

    auth_code: str | None = None
    state: str | None = None
    error: str | None = None
    done = threading.Event()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.error = params["error"][0]
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            _CallbackHandler.state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if _CallbackHandler.error:
            self.wfile.write(
                b"<h1>Authorization failed</h1><p>You can close this tab.</p>"
            )
        else:
            self.wfile.write(
                b"<h1>Authorization successful</h1><p>You can close this tab.</p>"
            )
        _CallbackHandler.done.set()

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_oauth_provider(
    server_url: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str | None = None,
) -> "httpx.Auth":
    """Build an OAuth provider for HTTP connections.

    If client_id and client_secret are provided, uses client credentials flow.
    Otherwise, uses authorization code + PKCE with a local callback server.
    """
    storage = FileTokenStorage(server_url)

    if client_id and client_secret:
        from mcp.client.auth.extensions.client_credentials import (
            ClientCredentialsOAuthProvider,
        )

        return ClientCredentialsOAuthProvider(
            server_url=server_url,
            storage=storage,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scope,
        )

    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )

    # Reset callback handler state
    _CallbackHandler.auth_code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None
    _CallbackHandler.done = threading.Event()

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)

    async def redirect_handler(auth_url: str) -> None:
        print(f"Opening browser for authorization...", file=sys.stderr)
        print(f"If browser doesn't open, visit: {auth_url}", file=sys.stderr)
        webbrowser.open(auth_url)

    async def callback_handler() -> tuple[str, str | None]:
        # Run the HTTP server in a thread, wait for the callback
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        # Wait with timeout
        if not _CallbackHandler.done.wait(timeout=300):
            server.server_close()
            raise TimeoutError("OAuth callback timed out after 5 minutes")
        server.server_close()
        if _CallbackHandler.error:
            raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
        if not _CallbackHandler.auth_code:
            raise RuntimeError("No authorization code received")
        return (_CallbackHandler.auth_code, _CallbackHandler.state)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
