"""mcp2cli — Turn any MCP server or OpenAPI spec into a CLI."""

from __future__ import annotations

__version__ = "2.2.2"

# --- Constants ---
from mcp2cli._cache import CACHE_DIR, DEFAULT_CACHE_TTL, CONFIG_DIR, BAKED_FILE
from mcp2cli._cache import cache_key_for, load_cached, save_cache
from mcp2cli._oauth import OAUTH_DIR
from mcp2cli._sessions import SESSIONS_DIR
from mcp2cli._graphql import GRAPHQL_INTROSPECTION_QUERY
from mcp2cli._bake import _BAKE_NAME_RE

# --- Types ---
from mcp2cli._types import ParamDef, CommandDef, BakeConfig, filter_commands, build_argparse

# --- Helpers ---
from mcp2cli._helpers import (
    resolve_secret,
    _parse_kv_list,
    read_stdin_json,
    schema_type_to_python,
    _coerce_item,
    coerce_value,
    to_kebab,
    _find_toon_cli,
    _toon_encode,
    output_result,
    _build_http_headers,
    _handle_http_error,
    list_openapi_commands,
    list_mcp_commands,
    _filter_commands,
    _extract_content_parts,
)

# --- OAuth ---
from mcp2cli._oauth import FileTokenStorage, _CallbackHandler, _find_free_port, build_oauth_provider

# --- OpenAPI ---
from mcp2cli._openapi import resolve_refs, load_openapi_spec, extract_openapi_commands
from mcp2cli._openapi import _collect_openapi_params, execute_openapi

# --- GraphQL ---
from mcp2cli._graphql import (
    _unwrap_type,
    _graphql_type_string,
    graphql_type_to_python,
    _build_selection_set,
    load_graphql_schema,
    _detect_field_collisions,
    _build_graphql_param,
    extract_graphql_commands,
    list_graphql_commands,
    _build_graphql_document,
    execute_graphql,
    handle_graphql,
)

# --- MCP ---
from mcp2cli._mcp import (
    extract_mcp_commands,
    run_mcp_http,
    run_mcp_stdio,
    _mcp_session,
    _handle_resources,
    _handle_prompts,
    _fetch_or_cache_mcp_tools,
    _dispatch_mcp_call,
    handle_mcp,
    _fetch_mcp_tools,
)

# --- Sessions ---
from mcp2cli._sessions import (
    session_list,
    session_stop,
    session_start,
    _run_session_daemon,
    _session_request,
)

# --- Bake ---
from mcp2cli._bake import (
    _load_baked_all,
    _load_baked,
    _save_baked_all,
    _baked_to_argv,
    _handle_bake,
    _run_baked,
)

# --- CLI ---
from mcp2cli._cli import (
    _split_at_subcommand,
    main,
    _build_main_parser,
    _validate_source_modes,
    _setup_oauth,
    _handle_session_operations,
    _resolve_resource_prompt_actions,
    _handle_openapi_mode,
    _main_impl,
)


# ---------------------------------------------------------------------------
# Monkeypatch propagation — tests patch attributes on the mcp2cli package,
# but the actual code lives in submodules.  We replace this module's class
# with a custom one whose __setattr__ propagates writes to the canonical
# submodule, so monkeypatch.setattr(mcp2cli, "X", ...) is seen by the
# submodule code that actually uses X.
# ---------------------------------------------------------------------------

import sys as _sys
import types as _types


class _Module(_types.ModuleType):
    _ATTR_PROPAGATION = {
        "CACHE_DIR": "mcp2cli._cache",
        "BAKED_FILE": "mcp2cli._cache",
        "CONFIG_DIR": "mcp2cli._cache",
        "DEFAULT_CACHE_TTL": "mcp2cli._cache",
        "OAUTH_DIR": "mcp2cli._oauth",
        "SESSIONS_DIR": "mcp2cli._sessions",
        "_toon_encode": "mcp2cli._helpers",
    }

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        target = self._ATTR_PROPAGATION.get(name)
        if target:
            mod = _sys.modules.get(target)
            if mod is not None:
                setattr(mod, name, value)


_sys.modules[__name__].__class__ = _Module
