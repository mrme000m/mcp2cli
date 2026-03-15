"""OpenAPI spec loading, command extraction, and execution."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import httpx

from mcp2cli._types import ParamDef, CommandDef
from mcp2cli._helpers import (
    schema_type_to_python,
    to_kebab,
    read_stdin_json,
    output_result,
    _build_http_headers,
    _handle_http_error,
)
from mcp2cli._cache import cache_key_for, load_cached, save_cache


def resolve_refs(spec: dict) -> dict:
    spec = copy.deepcopy(spec)

    def _resolve(node, root, seen):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen:
                    return node
                seen = seen | {ref}
                if ref.startswith("#/"):
                    parts = ref[2:].split("/")
                    target = root
                    for p in parts:
                        target = target[p]
                    return _resolve(copy.deepcopy(target), root, seen)
                return node
            return {k: _resolve(v, root, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item, root, seen) for item in node]
        return node

    return _resolve(spec, spec, set())


def load_openapi_spec(
    source: str,
    auth_headers: list[tuple[str, str]],
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    oauth_provider: "httpx.Auth | None" = None,
) -> dict:
    is_url = source.startswith("http://") or source.startswith("https://")

    if is_url:
        key = cache_key or cache_key_for(source)
        if not refresh:
            cached = load_cached(key, ttl)
            if cached is not None:
                return cached

        headers = dict(auth_headers)
        with httpx.Client(timeout=30, auth=oauth_provider) as client:
            resp = client.get(source, headers=headers)
            resp.raise_for_status()
            raw = resp.text
    else:
        raw = Path(source).read_text()

    # Parse JSON or YAML
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        spec = yaml.safe_load(raw)

    if not isinstance(spec, dict) or "paths" not in spec:
        print("Error: spec must contain 'paths'", file=sys.stderr)
        sys.exit(1)

    spec = resolve_refs(spec)

    if is_url:
        save_cache(key, spec)

    return spec


def extract_openapi_commands(spec: dict) -> list[CommandDef]:
    commands: list[CommandDef] = []
    seen_names: dict[str, int] = {}

    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if not isinstance(details, dict):
                continue

            op_id = details.get("operationId")
            if op_id:
                name = to_kebab(op_id)
            else:
                slug = (
                    path.strip("/").replace("/", "-").replace("{", "").replace("}", "")
                )
                name = f"{method}-{slug}" if slug else method

            if name in seen_names:
                seen_names[name] += 1
                name = f"{name}-{method}"
            seen_names[name] = 1

            desc = (
                details.get("summary")
                or details.get("description")
                or f"{method.upper()} {path}"
            )
            params: list[ParamDef] = []

            # Parameters (path, query, header)
            for param in details.get("parameters", []):
                schema = param.get("schema", {})
                py_type, suffix = schema_type_to_python(schema)
                p = ParamDef(
                    name=to_kebab(param["name"]),
                    original_name=param["name"],
                    python_type=py_type,
                    required=param.get("required", False),
                    description=(param.get("description") or param["name"]) + suffix,
                    choices=schema.get("enum"),
                    location=param.get("in", "query"),
                )
                params.append(p)

            # Request body
            rb_schema = (
                details.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            required_fields = set(rb_schema.get("required", []))
            properties = rb_schema.get("properties", {})
            has_body = bool(properties)

            for prop_name, prop_schema in properties.items():
                py_type, suffix = schema_type_to_python(prop_schema)
                p = ParamDef(
                    name=to_kebab(prop_name),
                    original_name=prop_name,
                    python_type=py_type,
                    required=prop_name in required_fields,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    choices=prop_schema.get("enum"),
                    location="body",
                )
                params.append(p)

            commands.append(
                CommandDef(
                    name=name,
                    description=desc,
                    params=params,
                    has_body=has_body,
                    method=method,
                    path=path,
                )
            )

    return commands


def _collect_openapi_params(
    cmd: CommandDef,
    args,
) -> tuple[str, dict[str, str], dict[str, str], dict | None]:
    """Collect OpenAPI params from parsed args, separated by location.

    Returns (path, query_params, extra_headers, body_or_none) where *path*
    has ``{param}`` placeholders substituted with actual values.
    """
    path = cmd.path or ""
    query_params: dict[str, str] = {}
    extra_headers: dict[str, str] = {}
    body: dict | None = None

    for p in cmd.params:
        if p.location == "path":
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                path = path.replace(f"{{{p.original_name}}}", str(val))

    if cmd.method == "get":
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is None:
                continue
            if p.location == "query":
                query_params[p.original_name] = val
            elif p.location == "header":
                extra_headers[p.original_name] = str(val)
    else:
        if getattr(args, "stdin", False):
            body = read_stdin_json("OpenAPI request body")
        else:
            body = {}
            for p in cmd.params:
                val = getattr(args, p.name.replace("-", "_"), None)
                if p.location == "header":
                    if val is not None:
                        extra_headers[p.original_name] = str(val)
                    continue
                if p.location == "path":
                    continue
                if val is not None:
                    body[p.original_name] = val
            if not body:
                body = None
        # Also collect query params for non-GET
        for p in cmd.params:
            if p.location == "query":
                val = getattr(args, p.name.replace("-", "_"), None)
                if val is not None:
                    query_params[p.original_name] = val

    return path, query_params, extra_headers, body


def execute_openapi(
    args,
    cmd: CommandDef,
    base_url: str,
    auth_headers: list[tuple[str, str]],
    pretty: bool,
    raw: bool,
    toon: bool = False,
    oauth_provider: "httpx.Auth | None" = None,
):
    path, query_params, extra_headers, body = _collect_openapi_params(cmd, args)
    url = base_url.rstrip("/") + path

    headers = _build_http_headers(auth_headers)
    headers.update(extra_headers)

    with httpx.Client(timeout=60, auth=oauth_provider) as client:
        resp = client.request(
            (cmd.method or "get").upper(),
            url,
            headers=headers,
            params=query_params or None,
            json=body,
        )
        _handle_http_error(resp)

    if raw:
        sys.stdout.buffer.write(resp.content)
        return

    try:
        data = resp.json()
    except Exception:
        print(resp.text)
        return

    output_result(data, pretty=pretty, toon=toon)
