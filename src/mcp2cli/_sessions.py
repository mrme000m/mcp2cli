"""Session daemon management — persistent MCP connections."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio

from mcp2cli._cache import CACHE_DIR
from mcp2cli._helpers import _extract_content_parts

SESSIONS_DIR = CACHE_DIR / "sessions"


def _session_meta_path(name: str) -> Path:
    return SESSIONS_DIR / f"{name}.json"


def _session_sock_path(name: str) -> Path:
    return SESSIONS_DIR / f"{name}.sock"


def _session_is_alive(meta: dict) -> bool:
    pid = meta.get("pid")
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def session_list() -> list[dict]:
    """List active sessions."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for meta_file in SESSIONS_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        name = meta_file.stem
        meta["name"] = name
        meta["alive"] = _session_is_alive(meta)
        sessions.append(meta)
    return sessions


def session_stop(name: str):
    """Stop a named session."""
    meta_path = _session_meta_path(name)
    sock_path = _session_sock_path(name)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            pid = meta.get("pid")
            if pid and _session_is_alive(meta):
                os.kill(pid, signal.SIGTERM)
                # Wait briefly for clean shutdown
                for _ in range(10):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.1)
                    except OSError:
                        break
        except (json.JSONDecodeError, OSError):
            pass
        meta_path.unlink(missing_ok=True)
    sock_path.unlink(missing_ok=True)


def session_start(
    name: str,
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
):
    """Start a persistent session daemon."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running
    meta_path = _session_meta_path(name)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if _session_is_alive(meta):
                print(
                    f"Session '{name}' is already running (PID {meta['pid']})",
                    file=sys.stderr,
                )
                sys.exit(1)
        except (json.JSONDecodeError, OSError):
            pass
        # Stale session — clean up
        meta_path.unlink(missing_ok=True)
        _session_sock_path(name).unlink(missing_ok=True)

    # Spawn daemon
    daemon_script = json.dumps(
        {
            "name": name,
            "source": source,
            "is_stdio": is_stdio,
            "auth_headers": auth_headers,
            "env_vars": env_vars,
            "transport": transport,
        }
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import mcp2cli; mcp2cli._run_session_daemon({json.dumps(daemon_script)})",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    # Wait for socket to appear
    sock_path = _session_sock_path(name)
    deadline = time.time() + 15
    while time.time() < deadline:
        if sock_path.exists():
            print(f"Session '{name}' started (PID {proc.pid})")
            return
        if proc.poll() is not None:
            print(
                f"Error: session daemon exited with code {proc.returncode}",
                file=sys.stderr,
            )
            sys.exit(1)
        time.sleep(0.1)

    print("Error: session daemon did not start in time", file=sys.stderr)
    proc.kill()
    sys.exit(1)


async def _dispatch_list_tools(session, params):
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
        for t in result.tools
    ]


async def _dispatch_call_tool(session, params):
    result = await session.call_tool(params["name"], params.get("arguments", {}))
    return _extract_content_parts(result.content)


async def _dispatch_list_resources(session, params):
    result = await session.list_resources()
    return [
        {"name": r.name, "uri": str(r.uri), "description": r.description or "", "mimeType": r.mimeType or ""}
        for r in result.resources
    ]


async def _dispatch_read_resource(session, params):
    from pydantic import AnyUrl

    result = await session.read_resource(AnyUrl(params["uri"]))
    return _extract_content_parts(result.contents, attrs=("text", "blob"))


async def _dispatch_list_resource_templates(session, params):
    result = await session.list_resource_templates()
    return [
        {"name": t.name, "uriTemplate": str(t.uriTemplate), "description": t.description or "", "mimeType": t.mimeType or ""}
        for t in result.resourceTemplates
    ]


async def _dispatch_list_prompts(session, params):
    result = await session.list_prompts()
    return [
        {
            "name": p.name,
            "description": p.description or "",
            "arguments": [
                {"name": a.name, "description": a.description or "", "required": a.required or False}
                for a in (p.arguments or [])
            ],
        }
        for p in result.prompts
    ]


async def _dispatch_get_prompt(session, params):
    result = await session.get_prompt(params["name"], params.get("arguments", {}))
    messages = []
    for msg in result.messages:
        content = msg.content
        if hasattr(content, "text"):
            messages.append({"role": msg.role, "content": content.text})
        else:
            messages.append({"role": msg.role, "content": json.dumps(content.model_dump())})
    return {"description": result.description or "", "messages": messages}


_SESSION_DISPATCH = {
    "list_tools": _dispatch_list_tools,
    "call_tool": _dispatch_call_tool,
    "list_resources": _dispatch_list_resources,
    "read_resource": _dispatch_read_resource,
    "list_resource_templates": _dispatch_list_resource_templates,
    "list_prompts": _dispatch_list_prompts,
    "get_prompt": _dispatch_get_prompt,
}


def _run_session_daemon(config_json: str):
    """Entry point for the session daemon process."""
    config = json.loads(config_json)
    name = config["name"]
    source = config["source"]
    is_stdio = config["is_stdio"]
    auth_headers = [tuple(h) for h in config["auth_headers"]]
    env_vars = config["env_vars"]
    transport = config["transport"]

    sock_path = _session_sock_path(name)
    meta_path = _session_meta_path(name)

    async def _dispatch(session, method: str, params: dict):
        """Dispatch a method call to the MCP session."""
        handler = _SESSION_DISPATCH.get(method)
        if handler is None:
            raise ValueError(f"Unknown method: {method}")
        return await handler(session, params)

    async def _daemon():
        from mcp import ClientSession

        async def _run_with_session(session):
            await session.initialize()

            # Write metadata
            meta = {
                "pid": os.getpid(),
                "source": source,
                "transport": "stdio" if is_stdio else "http",
                "created_at": time.time(),
            }
            meta_path.write_text(json.dumps(meta))

            # Start Unix domain socket server
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server_sock.bind(str(sock_path))
                server_sock.listen(5)
                server_sock.settimeout(1.0)

                # Handle SIGTERM
                _shutdown = False

                def _on_sigterm(*_):
                    nonlocal _shutdown
                    _shutdown = True

                signal.signal(signal.SIGTERM, _on_sigterm)

                def _blocking_accept():
                    """Accept a connection in a thread (blocks until connection or timeout)."""
                    while not _shutdown:
                        try:
                            return server_sock.accept()
                        except socket.timeout:
                            continue
                        except OSError:
                            return None, None
                    return None, None

                while not _shutdown:
                    conn, _ = await anyio.to_thread.run_sync(_blocking_accept)
                    if conn is None:
                        break

                    try:
                        conn.settimeout(5)

                        def _recv_request(c):
                            data = b""
                            while True:
                                chunk = c.recv(65536)
                                if not chunk:
                                    break
                                data += chunk
                                if b"\n" in data:
                                    break
                            return data

                        raw = await anyio.to_thread.run_sync(
                            lambda: _recv_request(conn)
                        )
                        line = raw.split(b"\n", 1)[0]
                        if not line:
                            conn.close()
                            continue

                        request = json.loads(line)
                        req_id = request.get("id", 0)
                        method = request.get("method", "")
                        params = request.get("params", {})

                        try:
                            resp_data = await _dispatch(session, method, params)
                            response = (
                                json.dumps({"id": req_id, "result": resp_data}) + "\n"
                            )
                        except Exception as e:
                            response = (
                                json.dumps({"id": req_id, "error": str(e)}) + "\n"
                            )

                        def _send(c, data):
                            c.sendall(data)

                        await anyio.to_thread.run_sync(
                            lambda: _send(conn, response.encode())
                        )
                    except Exception:
                        pass
                    finally:
                        conn.close()

            finally:
                server_sock.close()
                sock_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

        if is_stdio:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            parts = shlex.split(source)
            env = {**os.environ, **env_vars}
            params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await _run_with_session(session)
        else:
            headers = dict(auth_headers) if auth_headers else None

            async def _via_streamable():
                from mcp.client.streamable_http import streamablehttp_client

                async with streamablehttp_client(source, headers=headers) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await _run_with_session(session)

            async def _via_sse():
                from mcp.client.sse import sse_client

                async with sse_client(source, headers=headers) as (read, write):
                    async with ClientSession(read, write) as session:
                        await _run_with_session(session)

            if transport == "sse":
                await _via_sse()
            elif transport == "streamable":
                await _via_streamable()
            else:
                try:
                    await _via_streamable()
                except Exception:
                    await _via_sse()

    anyio.run(_daemon)


def _session_request(name: str, method: str, params: dict | None = None) -> any:
    """Send a request to a session daemon and return the result."""
    sock_path = _session_sock_path(name)
    if not sock_path.exists():
        print(f"Error: session '{name}' not found", file=sys.stderr)
        sys.exit(1)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(str(sock_path))
        request = json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n"
        conn.sendall(request.encode())
        conn.shutdown(socket.SHUT_WR)

        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk

        response = json.loads(data.decode())
        if "error" in response:
            print(f"Error: {response['error']}", file=sys.stderr)
            sys.exit(1)
        return response["result"]
    finally:
        conn.close()
