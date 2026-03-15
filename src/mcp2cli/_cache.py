"""Caching functions and directory constants."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(
    os.environ.get("MCP2CLI_CACHE_DIR", Path.home() / ".cache" / "mcp2cli")
)
DEFAULT_CACHE_TTL = 3600
CONFIG_DIR = Path(
    os.environ.get("MCP2CLI_CONFIG_DIR", Path.home() / ".config" / "mcp2cli")
)
BAKED_FILE = CONFIG_DIR / "baked.json"


def cache_key_for(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def load_cached(key: str, ttl: int) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age >= ttl:
        return None
    return json.loads(path.read_text())


def save_cache(key: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data))
