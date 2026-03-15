"""Utility functions used across modules."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx


def resolve_secret(value: str) -> str:
    """Resolve a secret value from env var, file, or literal.

    Supports:
      env:VAR_NAME   — read from environment variable
      file:/path     — read from file (trailing newline stripped)
      literal value  — returned as-is
    """
    if value.startswith("env:"):
        var = value[4:]
        resolved = os.environ.get(var)
        if resolved is None:
            print(f"Error: environment variable {var!r} is not set", file=sys.stderr)
            sys.exit(1)
        return resolved
    if value.startswith("file:"):
        path = Path(value[5:])
        if not path.exists():
            print(f"Error: secret file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text().rstrip("\n")
    return value


def _parse_kv_list(
    items: list[str],
    delimiter: str,
    label: str,
    *,
    resolve_values: bool = False,
) -> list[tuple[str, str]]:
    """Parse a list of 'KEY<delimiter>VALUE' strings into (key, value) pairs.

    Exits with an error message if any item is missing the delimiter.
    When *resolve_values* is True, each value is passed through :func:`resolve_secret`.
    """
    result: list[tuple[str, str]] = []
    for item in items:
        if delimiter not in item:
            print(f"Error: invalid {label} format: {item!r}", file=sys.stderr)
            sys.exit(1)
        k, v = item.split(delimiter, 1)
        k, v = k.strip(), v.strip()
        if resolve_values:
            v = resolve_secret(v)
        result.append((k, v))
    return result


def read_stdin_json(context: str):
    raw = sys.stdin.read()
    if not raw.strip():
        print(
            f"Error: --stdin expects JSON for {context}, but stdin was empty.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"Error: invalid JSON on stdin for {context} "
            f"(line {exc.lineno}, column {exc.colno}).",
            file=sys.stderr,
        )
        sys.exit(1)


def schema_type_to_python(schema: dict) -> tuple[type | None, str]:
    t = schema.get("type")
    if t == "integer":
        return int, ""
    if t == "number":
        return float, ""
    if t == "boolean":
        return None, ""
    if t == "array":
        return str, " (JSON array)"
    if t == "object":
        return str, " (JSON object)"
    return str, ""


def _coerce_item(value: str, item_type: str | None):
    """Coerce a single string value to the given JSON schema type."""
    if item_type == "integer":
        return int(value)
    if item_type == "number":
        return float(value)
    if item_type == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


def coerce_value(value, schema: dict):
    if value is None:
        return None
    t = schema.get("type")
    if t == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            item_type = schema.get("items", {}).get("type")
            if "," in value:
                return [_coerce_item(v.strip(), item_type) for v in value.split(",")]
            return [_coerce_item(value, item_type)]
        return value
    if t == "object":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if t == "boolean":
        return bool(value)
    if t == "integer":
        return int(value)
    if t == "number":
        return float(value)
    return value


def to_kebab(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    return s.replace("_", "-").lower()


def _find_toon_cli() -> str | None:
    """Return the command to invoke the TOON CLI, or None if unavailable."""
    if shutil.which("toon"):
        return "toon"
    # Check for npx (ships with Node.js)
    if shutil.which("npx"):
        return "npx @toon-format/cli"
    return None


def _toon_encode(json_str: str) -> str | None:
    """Pipe JSON through the TOON CLI. Returns TOON text or None on failure."""
    cmd = _find_toon_cli()
    if cmd is None:
        return None
    try:
        result = subprocess.run(
            cmd.split(),
            input=json_str,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def output_result(data, *, pretty: bool = False, raw: bool = False, toon: bool = False):
    if raw:
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data))
        return
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            print(data)
            return
    if toon:
        encoded = _toon_encode(json.dumps(data))
        if encoded is not None:
            print(encoded, end="")
            return
        print(
            "Warning: --toon requires the TOON CLI (@toon-format/cli). "
            "Install with: npm install -g @toon-format/cli",
            file=sys.stderr,
        )
        # Fall through to normal output
    if pretty or sys.stdout.isatty():
        print(json.dumps(data, indent=2))
    else:
        print(json.dumps(data))


def _build_http_headers(auth_headers: list[tuple[str, str]]) -> dict[str, str]:
    """Build HTTP headers dict from auth_headers with a Content-Type default."""
    headers = dict(auth_headers)
    headers.setdefault("Content-Type", "application/json")
    return headers


def _handle_http_error(resp) -> None:
    """Print error and exit(1) on non-2xx HTTP response."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


def list_openapi_commands(commands: list) -> None:
    groups: dict[str, list] = {}
    for cmd in commands:
        prefix = cmd.name.split("-", 1)[0] if "-" in cmd.name else "other"
        groups.setdefault(prefix, []).append(cmd)

    for group in sorted(groups):
        print(f"\n{group}:")
        for cmd in groups[group]:
            method = (cmd.method or "").upper()
            line = f"  {cmd.name:<45} {method:<6}"
            if cmd.description:
                line += f" {cmd.description[:60]}"
            print(line)


def list_mcp_commands(commands: list) -> None:
    for cmd in commands:
        desc = f"  {cmd.description[:70]}" if cmd.description else ""
        print(f"  {cmd.name:<40}{desc}")


def _filter_commands(commands: list, pattern: str) -> list:
    """Filter commands by case-insensitive substring match on name or description."""
    pattern_lower = pattern.lower()
    return [
        cmd
        for cmd in commands
        if pattern_lower in cmd.name.lower()
        or pattern_lower in (cmd.description or "").lower()
    ]


def _extract_content_parts(content_list, *, attrs=("text", "data")) -> str:
    """Extract text/data/blob from MCP content objects, joined by newline."""
    parts = []
    for c in content_list:
        for attr in attrs:
            if hasattr(c, attr):
                parts.append(getattr(c, attr))
                break
    return "\n".join(parts) if parts else ""
