"""Bake config CRUD and subcommands."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

import mcp2cli._cache as _cache_mod
from mcp2cli._types import BakeConfig
from mcp2cli._helpers import _parse_kv_list


_BAKE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _load_baked_all() -> dict:
    """Load all baked configs from disk."""
    if not _cache_mod.BAKED_FILE.exists():
        return {}
    try:
        return json.loads(_cache_mod.BAKED_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _load_baked(name: str) -> dict | None:
    """Load a single baked config by name."""
    return _load_baked_all().get(name)


def _save_baked_all(data: dict) -> None:
    """Save all baked configs to disk."""
    _cache_mod.BAKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _cache_mod.BAKED_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _baked_to_argv(config: dict) -> list[str]:
    """Reconstruct CLI argv from a baked config."""
    argv: list[str] = []
    st = config.get("source_type", "spec")
    source = config["source"]
    if st == "spec":
        argv += ["--spec", source]
    elif st == "mcp":
        argv += ["--mcp", source]
    elif st == "mcp_stdio":
        argv += ["--mcp-stdio", source]

    if config.get("base_url"):
        argv += ["--base-url", config["base_url"]]
    for name, value in config.get("auth_headers", []):
        argv += ["--auth-header", f"{name}:{value}"]
    for k, v in config.get("env_vars", {}).items():
        argv += ["--env", f"{k}={v}"]
    if config.get("cache_ttl") is not None:
        argv += ["--cache-ttl", str(config["cache_ttl"])]
    transport = config.get("transport", "auto")
    if transport != "auto":
        argv += ["--transport", transport]
    if config.get("oauth"):
        argv.append("--oauth")
    if config.get("oauth_client_id"):
        argv += ["--oauth-client-id", config["oauth_client_id"]]
    if config.get("oauth_client_secret"):
        argv += ["--oauth-client-secret", config["oauth_client_secret"]]
    if config.get("oauth_scope"):
        argv += ["--oauth-scope", config["oauth_scope"]]
    return argv


def _handle_bake(argv: list[str]) -> None:
    """Dispatch bake subcommands."""
    if not argv:
        print("Usage: mcp2cli bake <create|list|show|remove|update|install> ...")
        sys.exit(1)
    sub = argv[0]
    rest = argv[1:]
    dispatch = {
        "create": _bake_create,
        "list": lambda _: _bake_list(),
        "show": _bake_show,
        "remove": _bake_remove,
        "update": _bake_update,
        "install": _bake_install,
    }
    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown bake subcommand: {sub}", file=sys.stderr)
        sys.exit(1)
    handler(rest)


def _bake_create(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake create")
    p.add_argument("name", help="Name for the baked tool")
    p.add_argument("--spec", default=None)
    p.add_argument("--mcp", default=None)
    p.add_argument("--mcp-stdio", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--auth-header", action="append", default=[])
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--cache-ttl", type=int, default=_cache_mod.DEFAULT_CACHE_TTL)
    p.add_argument("--transport", choices=["auto", "sse", "streamable"], default="auto")
    p.add_argument("--oauth", action="store_true")
    p.add_argument("--oauth-client-id", default=None)
    p.add_argument("--oauth-client-secret", default=None)
    p.add_argument("--oauth-scope", default=None)
    p.add_argument("--include", default="", help="Comma-separated include globs")
    p.add_argument("--exclude", default="", help="Comma-separated exclude globs")
    p.add_argument("--methods", default="", help="Comma-separated HTTP methods")
    p.add_argument("--description", default="")
    p.add_argument("--force", action="store_true", help="Overwrite existing")
    args = p.parse_args(argv)

    if not _BAKE_NAME_RE.match(args.name):
        print(
            f"Error: invalid name {args.name!r} — must match [a-z][a-z0-9-]*",
            file=sys.stderr,
        )
        sys.exit(1)

    modes = [args.spec, args.mcp, args.mcp_stdio]
    active = sum(1 for m in modes if m is not None)
    if active == 0:
        print("Error: one of --spec, --mcp, or --mcp-stdio is required.", file=sys.stderr)
        sys.exit(1)
    if active > 1:
        print("Error: --spec, --mcp, and --mcp-stdio are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    all_configs = _load_baked_all()
    if args.name in all_configs and not args.force:
        print(
            f"Error: '{args.name}' already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.spec:
        source_type, source = "spec", args.spec
    elif args.mcp:
        source_type, source = "mcp", args.mcp
    else:
        source_type, source = "mcp_stdio", args.mcp_stdio

    auth_headers = [list(t) for t in _parse_kv_list(args.auth_header, ":", "auth header")]
    env_vars = dict(_parse_kv_list(args.env, "=", "env"))

    config = {
        "source_type": source_type,
        "source": source,
        "base_url": args.base_url,
        "auth_headers": auth_headers,
        "env_vars": env_vars,
        "cache_ttl": args.cache_ttl,
        "transport": args.transport,
        "oauth": args.oauth,
        "oauth_client_id": args.oauth_client_id,
        "oauth_client_secret": args.oauth_client_secret,
        "oauth_scope": args.oauth_scope,
        "include": [x.strip() for x in args.include.split(",") if x.strip()],
        "exclude": [x.strip() for x in args.exclude.split(",") if x.strip()],
        "methods": [x.strip().upper() for x in args.methods.split(",") if x.strip()],
        "description": args.description,
    }

    all_configs[args.name] = config
    _save_baked_all(all_configs)
    print(f"Baked tool '{args.name}' created.")


def _bake_list() -> None:
    configs = _load_baked_all()
    if not configs:
        print("No baked tools.")
        return
    print(f"{'Name':<20} {'Type':<10} {'Source':<50}")
    print("-" * 80)
    for name, cfg in sorted(configs.items()):
        st = cfg.get("source_type", "?")
        src = cfg.get("source", "?")
        if len(src) > 48:
            src = src[:45] + "..."
        print(f"{name:<20} {st:<10} {src:<50}")


def _bake_show(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake show")
    p.add_argument("name")
    args = p.parse_args(argv)
    cfg = _load_baked(args.name)
    if cfg is None:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    # Mask secrets in auth headers for display
    display = dict(cfg)
    if display.get("auth_headers"):
        masked = []
        for name, val in display["auth_headers"]:
            if val.startswith("env:") or val.startswith("file:"):
                masked.append([name, val])
            else:
                masked.append([name, val[:4] + "****" if len(val) > 4 else "****"])
        display["auth_headers"] = masked
    print(json.dumps(display, indent=2))


def _bake_remove(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake remove")
    p.add_argument("name")
    args = p.parse_args(argv)
    all_configs = _load_baked_all()
    if args.name not in all_configs:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    del all_configs[args.name]
    _save_baked_all(all_configs)
    # Clean up any installed wrapper
    wrapper = Path.home() / ".local" / "bin" / args.name
    if wrapper.exists():
        wrapper.unlink()
        print(f"Removed installed wrapper: {wrapper}")
    print(f"Baked tool '{args.name}' removed.")


def _bake_update(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake update")
    p.add_argument("name")
    p.add_argument("--cache-ttl", type=int, default=None)
    p.add_argument("--include", default=None)
    p.add_argument("--exclude", default=None)
    p.add_argument("--methods", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--transport", choices=["auto", "sse", "streamable"], default=None)
    args = p.parse_args(argv)
    all_configs = _load_baked_all()
    if args.name not in all_configs:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    cfg = all_configs[args.name]
    if args.cache_ttl is not None:
        cfg["cache_ttl"] = args.cache_ttl
    if args.include is not None:
        cfg["include"] = [x.strip() for x in args.include.split(",") if x.strip()]
    if args.exclude is not None:
        cfg["exclude"] = [x.strip() for x in args.exclude.split(",") if x.strip()]
    if args.methods is not None:
        cfg["methods"] = [x.strip().upper() for x in args.methods.split(",") if x.strip()]
    if args.description is not None:
        cfg["description"] = args.description
    if args.base_url is not None:
        cfg["base_url"] = args.base_url
    if args.transport is not None:
        cfg["transport"] = args.transport
    _save_baked_all(all_configs)
    print(f"Baked tool '{args.name}' updated.")


def _bake_install(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="mcp2cli bake install")
    p.add_argument("name")
    p.add_argument("--dir", default=None, help="Directory to install wrapper into (default: ~/.local/bin)")
    args = p.parse_args(argv)
    cfg = _load_baked(args.name)
    if cfg is None:
        print(f"Error: no baked tool named '{args.name}'", file=sys.stderr)
        sys.exit(1)
    bin_dir = Path(args.dir) if args.dir else Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / args.name
    # Resolve mcp2cli path
    mcp2cli_bin = shutil.which("mcp2cli") or "mcp2cli"
    wrapper.write_text(
        f"#!/bin/sh\nexec {shlex.quote(mcp2cli_bin)} @{args.name} \"$@\"\n"
    )
    wrapper.chmod(0o755)
    print(f"Installed wrapper: {wrapper}")
    if args.dir is None and str(bin_dir) not in os.environ.get("PATH", ""):
        print(f"  Note: {bin_dir} may not be in your PATH")


def _run_baked(name: str, argv: list[str]) -> None:
    """Load a baked config and run it."""
    cfg = _load_baked(name)
    if cfg is None:
        print(f"Error: no baked tool named '{name}'", file=sys.stderr)
        sys.exit(1)
    synthetic_argv = _baked_to_argv(cfg) + list(argv)
    bake_config = BakeConfig(
        include=cfg.get("include", []),
        exclude=cfg.get("exclude", []),
        methods=cfg.get("methods", []),
    )
    # Lazy import to avoid circular dependency
    from mcp2cli._cli import _main_impl
    _main_impl(synthetic_argv, bake_config=bake_config)
