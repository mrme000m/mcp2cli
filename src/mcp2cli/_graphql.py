"""GraphQL introspection, command extraction, and execution."""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from mcp2cli._types import ParamDef, CommandDef, build_argparse
from mcp2cli._helpers import (
    to_kebab,
    read_stdin_json,
    coerce_value,
    output_result,
    _build_http_headers,
    _handle_http_error,
)
from mcp2cli._cache import cache_key_for, load_cached, save_cache


GRAPHQL_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields(includeDeprecated: false) {
        name
        description
        args {
          name
          description
          type {
            ...TypeRef
          }
          defaultValue
        }
        type {
          ...TypeRef
        }
      }
      inputFields {
        name
        description
        type {
          ...TypeRef
        }
        defaultValue
      }
      enumValues(includeDeprecated: false) {
        name
        description
      }
    }
  }
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
        }
      }
    }
  }
}
"""


def _unwrap_type(type_ref: dict) -> tuple[dict, bool, bool]:
    """Unwrap NON_NULL/LIST wrappers to find the underlying named type.

    Returns (named_type_dict, is_non_null, is_list).
    """
    is_non_null = False
    is_list = False
    t = type_ref
    while t:
        kind = t.get("kind")
        if kind == "NON_NULL":
            is_non_null = True
            t = t.get("ofType", {})
        elif kind == "LIST":
            is_list = True
            t = t.get("ofType", {})
        else:
            return t, is_non_null, is_list
    return type_ref, is_non_null, is_list


def _graphql_type_string(type_ref: dict) -> str:
    """Reconstruct GraphQL type notation from introspection type ref.

    E.g. ``"[String!]!"`` or ``"ID!"`` or ``"Int"``.
    """
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        inner = _graphql_type_string(type_ref.get("ofType", {}))
        return f"{inner}!"
    if kind == "LIST":
        inner = _graphql_type_string(type_ref.get("ofType", {}))
        return f"[{inner}]"
    return type_ref.get("name", "String")


def graphql_type_to_python(
    type_ref: dict, types_by_name: dict
) -> tuple[type | None, bool, list | None]:
    """Map a GraphQL introspection type to (python_type, required, choices).

    - Scalars → str/int/float/None(bool)
    - Enums → str with choices
    - Input objects → str (JSON)
    - Lists → str (JSON array or comma-delimited)
    """
    named, is_non_null, is_list = _unwrap_type(type_ref)
    type_name = named.get("name", "")
    type_kind = named.get("kind", "")

    if is_list:
        return str, is_non_null, None

    if type_kind == "ENUM":
        enum_type = types_by_name.get(type_name, {})
        choices = [ev["name"] for ev in enum_type.get("enumValues", [])]
        return str, is_non_null, choices or None

    if type_kind == "INPUT_OBJECT":
        return str, is_non_null, None

    # Scalars
    scalar_map = {
        "String": str,
        "ID": str,
        "Int": int,
        "Float": float,
        "Boolean": None,  # store_true
    }
    py_type = scalar_map.get(type_name, str)
    return py_type, is_non_null, None


def _build_selection_set(
    type_ref: dict, types_by_name: dict, depth: int = 2, seen: set | None = None
) -> str:
    """Auto-generate a GraphQL selection set from a return type.

    Depth 2 = scalar fields + one level of nested object scalar fields.
    """
    if seen is None:
        seen = set()

    named, _, is_list = _unwrap_type(type_ref)
    type_name = named.get("name", "")
    type_kind = named.get("kind", "")

    # Scalar / enum — no selection needed
    if type_kind in ("SCALAR", "ENUM"):
        return ""

    if type_name in seen or depth <= 0:
        return ""

    type_def = types_by_name.get(type_name, {})
    fields = type_def.get("fields", [])
    if not fields:
        return ""

    seen = seen | {type_name}
    parts = []
    for f in fields:
        f_named, _, _ = _unwrap_type(f["type"])
        f_kind = f_named.get("kind", "")
        if f_kind in ("SCALAR", "ENUM"):
            parts.append(f["name"])
        elif f_kind == "OBJECT" and depth > 1:
            nested = _build_selection_set(f["type"], types_by_name, depth - 1, seen)
            if nested:
                parts.append(f"{f['name']} {nested}")
    if not parts:
        return ""
    return "{ " + " ".join(parts) + " }"


def load_graphql_schema(
    url: str,
    auth_headers: list[tuple[str, str]],
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    oauth_provider: "httpx.Auth | None" = None,
) -> dict:
    """POST introspection query to a GraphQL endpoint, with caching."""
    key = cache_key or cache_key_for(f"graphql:{url}")
    if not refresh:
        cached = load_cached(key, ttl)
        if cached is not None:
            return cached

    headers = dict(auth_headers)
    headers.setdefault("Content-Type", "application/json")
    with httpx.Client(timeout=30, auth=oauth_provider) as client:
        resp = client.post(
            url,
            headers=headers,
            json={"query": GRAPHQL_INTROSPECTION_QUERY},
        )
        resp.raise_for_status()
        result = resp.json()

    if "errors" in result and not result.get("data"):
        msgs = "; ".join(e.get("message", "") for e in result["errors"])
        print(f"Error: GraphQL introspection failed: {msgs}", file=sys.stderr)
        sys.exit(1)

    schema = result.get("data", {}).get("__schema", {})
    if not schema:
        print("Error: introspection returned no schema", file=sys.stderr)
        sys.exit(1)

    save_cache(key, schema)
    return schema


def _detect_field_collisions(
    query_fields: list[dict], mutation_fields: list[dict]
) -> set[str]:
    """Return field names that appear in both query and mutation types."""
    all_names: set[str] = set()
    collisions: set[str] = set()
    for f in query_fields + mutation_fields:
        n = f["name"]
        if n in all_names:
            collisions.add(n)
        all_names.add(n)
    return collisions


def _build_graphql_param(arg: dict, types_by_name: dict) -> ParamDef:
    """Convert a single GraphQL field argument into a ParamDef."""
    py_type, required, choices = graphql_type_to_python(arg["type"], types_by_name)
    gql_type_str = _graphql_type_string(arg["type"])
    named_t, _, is_list = _unwrap_type(arg["type"])

    # Build schema for coerce_value
    param_schema: dict = {"graphql_type": gql_type_str}
    if is_list:
        param_schema["type"] = "array"
        inner_named, _, _ = _unwrap_type(named_t)
        item_type_name = inner_named.get("name", "String")
        item_map = {
            "Int": "integer", "Float": "number", "String": "string",
            "ID": "string", "Boolean": "boolean",
        }
        param_schema["items"] = {"type": item_map.get(item_type_name, "string")}
    elif named_t.get("kind") == "INPUT_OBJECT":
        param_schema["type"] = "object"
    elif named_t.get("kind") == "ENUM":
        param_schema["type"] = "string"

    arg_desc = arg.get("description") or arg["name"]
    if is_list:
        arg_desc += " (JSON array)"
    elif named_t.get("kind") == "INPUT_OBJECT":
        arg_desc += " (JSON object)"

    return ParamDef(
        name=to_kebab(arg["name"]),
        original_name=arg["name"],
        python_type=py_type,
        required=required,
        description=arg_desc,
        choices=choices,
        location="graphql_arg",
        schema=param_schema,
    )


def extract_graphql_commands(schema: dict) -> list[CommandDef]:
    """Convert introspection schema into CommandDef list."""
    types_by_name = {t["name"]: t for t in schema.get("types", []) if t.get("name")}

    query_type_name = (schema.get("queryType") or {}).get("name")
    mutation_type_name = (schema.get("mutationType") or {}).get("name")

    commands: list[CommandDef] = []
    seen_names: set[str] = set()

    query_fields = types_by_name.get(query_type_name, {}).get("fields", []) if query_type_name else []
    mutation_fields = types_by_name.get(mutation_type_name, {}).get("fields", []) if mutation_type_name else []
    collisions = _detect_field_collisions(query_fields, mutation_fields)

    for op_type, type_name, fields in [
        ("query", query_type_name, query_fields),
        ("mutation", mutation_type_name, mutation_fields),
    ]:
        for field_def in fields:
            field_name = field_def["name"]
            if field_name.startswith("__"):
                continue

            cli_name = to_kebab(field_name)
            if field_name in collisions:
                cli_name = f"{op_type}-{cli_name}"

            if cli_name in seen_names:
                cli_name = f"{op_type}-{cli_name}"
            seen_names.add(cli_name)

            desc = field_def.get("description") or f"{op_type} {field_name}"
            params = [
                _build_graphql_param(arg, types_by_name)
                for arg in field_def.get("args", [])
            ]

            commands.append(
                CommandDef(
                    name=cli_name,
                    description=desc,
                    params=params,
                    has_body=bool(params),
                    graphql_operation_type=op_type,
                    graphql_field_name=field_name,
                    graphql_return_type=field_def.get("type"),
                )
            )

    return commands


def list_graphql_commands(commands: list[CommandDef]):
    """Group commands by operation type and print."""
    groups: dict[str, list[CommandDef]] = {}
    for cmd in commands:
        key = cmd.graphql_operation_type or "other"
        groups.setdefault(key, []).append(cmd)

    for group in ["query", "mutation"]:
        cmds = groups.get(group, [])
        if not cmds:
            continue
        label = "queries" if group == "query" else "mutations"
        print(f"\n{label}:")
        for cmd in cmds:
            desc = f"  {cmd.description[:60]}" if cmd.description else ""
            print(f"  {cmd.name:<40}{desc}")


def _build_graphql_document(
    cmd: CommandDef,
    args: argparse.Namespace,
    schema: dict,
    fields_override: str | None = None,
) -> tuple[str, dict, str]:
    """Build a GraphQL document string and variables dict from parsed args.

    Returns (document, variables, field_name).
    """
    types_by_name = {t["name"]: t for t in schema.get("types", []) if t.get("name")}

    # Build variables dict from args
    if getattr(args, "stdin", False):
        variables = read_stdin_json("GraphQL variables")
    else:
        variables = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                variables[p.original_name] = coerce_value(val, p.schema)

    # Build variable declarations for the document
    var_decls = []
    for p in cmd.params:
        if p.original_name in variables:
            gql_type = p.schema.get("graphql_type", "String")
            var_decls.append(f"${p.original_name}: {gql_type}")

    # Build selection set
    if fields_override:
        selection = f"{{ {fields_override} }}"
    elif cmd.graphql_return_type:
        selection = _build_selection_set(cmd.graphql_return_type, types_by_name)
    else:
        selection = ""

    # Build argument list for the field
    field_args = []
    for p in cmd.params:
        if p.original_name in variables:
            field_args.append(f"{p.original_name}: ${p.original_name}")

    field_name = cmd.graphql_field_name or cmd.name
    args_str = f"({', '.join(field_args)})" if field_args else ""
    op_type = cmd.graphql_operation_type or "query"
    var_decls_str = f"({', '.join(var_decls)})" if var_decls else ""

    document = f"{op_type}{var_decls_str} {{ {field_name}{args_str} {selection} }}"
    return document, variables, field_name


def execute_graphql(
    args: argparse.Namespace,
    cmd: CommandDef,
    url: str,
    schema: dict,
    auth_headers: list[tuple[str, str]],
    pretty: bool,
    raw: bool,
    toon: bool = False,
    fields_override: str | None = None,
    oauth_provider: "httpx.Auth | None" = None,
):
    """Build and execute a GraphQL query/mutation."""
    document, variables, field_name = _build_graphql_document(
        cmd, args, schema, fields_override
    )

    headers = _build_http_headers(auth_headers)

    with httpx.Client(timeout=60, auth=oauth_provider) as client:
        resp = client.post(
            url,
            headers=headers,
            json={"query": document, "variables": variables or None},
        )
        _handle_http_error(resp)

    result = resp.json()
    if "errors" in result:
        if not result.get("data"):
            msgs = "; ".join(e.get("message", "") for e in result["errors"])
            print(f"GraphQL error: {msgs}", file=sys.stderr)
            sys.exit(1)
        # Partial errors — include them in output
        output_result(result, pretty=pretty, raw=raw, toon=toon)
        return

    data = result.get("data", {})
    # Extract the specific field's data
    field_data = data.get(field_name, data)
    output_result(field_data, pretty=pretty, raw=raw, toon=toon)


def handle_graphql(
    url: str,
    auth_headers: list[tuple[str, str]],
    remaining: list[str],
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    fields_override: str | None = None,
    oauth_provider: "httpx.Auth | None" = None,
):
    """Top-level handler for --graphql mode."""
    schema = load_graphql_schema(url, auth_headers, cache_key, ttl, refresh, oauth_provider=oauth_provider)
    commands = extract_graphql_commands(schema)

    if list_mode:
        list_graphql_commands(commands)
        return

    if not remaining:
        print("Available operations:")
        list_graphql_commands(commands)
        print("\nUse --list for the same output, or provide a subcommand.")
        return

    pre_for_gql = argparse.ArgumentParser(add_help=False)
    parser = build_argparse(commands, pre_for_gql)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd
    execute_graphql(
        args, cmd, url, schema, auth_headers, pretty, raw, toon=toon,
        fields_override=fields_override, oauth_provider=oauth_provider,
    )
