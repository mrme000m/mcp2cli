"""MCP command extraction, http/stdio transport, and handle_mcp."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

import anyio
import httpx

from mcp2cli._types import ParamDef, CommandDef, BakeConfig, build_argparse, filter_commands
from mcp2cli._helpers import (
    schema_type_to_python,
    to_kebab,
    read_stdin_json,
    coerce_value,
    output_result,
    list_mcp_commands,
    _filter_commands,
    _extract_content_parts,
)
from mcp2cli._cache import cache_key_for, load_cached, save_cache


def extract_mcp_commands(tools: list[dict]) -> list[CommandDef]:
    commands: list[CommandDef] = []
    for tool in tools:
        name = to_kebab(tool.get("name", "unknown"))
        desc = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        required_fields = set(schema.get("required", []))
        params: list[ParamDef] = []

        for prop_name, prop_schema in schema.get("properties", {}).items():
            py_type, suffix = schema_type_to_python(prop_schema)
            params.append(
                ParamDef(
                    name=to_kebab(prop_name),
                    original_name=prop_name,
                    python_type=py_type,
                    required=prop_name in required_fields,
                    description=(prop_schema.get("description") or prop_name) + suffix,
                    choices=prop_schema.get("enum"),
                    location="tool_input",
                    schema=prop_schema,
                )
            )

        commands.append(
            CommandDef(
                name=name,
                description=desc,
                params=params,
                has_body=bool(params),
                tool_name=tool.get("name"),
            )
        )
    return commands


def run_mcp_http(
    url: str,
    auth_headers: list[tuple[str, str]],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
):
    extra = dict(
        resource_action=resource_action,
        resource_uri=resource_uri,
        prompt_action=prompt_action,
        prompt_name=prompt_name,
        prompt_arguments=prompt_arguments,
        search_pattern=search_pattern,
    )

    async def _run():
        from mcp import ClientSession

        headers = dict(auth_headers) if auth_headers else None

        async def _with_streamable():
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(
                url, headers=headers, auth=oauth_provider
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session,
                        tool_name,
                        arguments,
                        list_mode,
                        pretty,
                        raw,
                        cache_key,
                        ttl,
                        refresh,
                        toon=toon,
                        **extra,
                    )

        async def _with_sse():
            from mcp.client.sse import sse_client

            async with sse_client(url, headers=headers, auth=oauth_provider) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _mcp_session(
                        session,
                        tool_name,
                        arguments,
                        list_mode,
                        pretty,
                        raw,
                        cache_key,
                        ttl,
                        refresh,
                        toon=toon,
                        **extra,
                    )

        if transport == "sse":
            return await _with_sse()
        elif transport == "streamable":
            return await _with_streamable()
        else:  # auto
            try:
                return await _with_streamable()
            except Exception:
                return await _with_sse()

    anyio.run(_run)


def run_mcp_stdio(
    command_str: str,
    env_vars: dict[str, str],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
):
    extra = dict(
        resource_action=resource_action,
        resource_uri=resource_uri,
        prompt_action=prompt_action,
        prompt_name=prompt_name,
        prompt_arguments=prompt_arguments,
        search_pattern=search_pattern,
    )

    async def _run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parts = shlex.split(command_str)
        env = {**os.environ, **env_vars}
        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _mcp_session(
                    session,
                    tool_name,
                    arguments,
                    list_mode,
                    pretty,
                    raw,
                    cache_key,
                    ttl,
                    refresh,
                    toon=toon,
                    **extra,
                )

    anyio.run(_run)


async def _mcp_session(
    session,
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
):
    # Handle resource operations
    if resource_action:
        await _handle_resources(
            session, resource_action, resource_uri, pretty, raw, toon
        )
        return

    # Handle prompt operations
    if prompt_action:
        await _handle_prompts(
            session, prompt_action, prompt_name, prompt_arguments, pretty, raw, toon
        )
        return

    if list_mode:
        result = await session.list_tools()
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
            }
            for t in result.tools
        ]
        commands = extract_mcp_commands(tools)
        if search_pattern:
            commands = _filter_commands(commands, search_pattern)
            if not commands:
                print(f"\nNo tools matching '{search_pattern}'.")
                return
            print(f"\nTools matching '{search_pattern}':")
        else:
            print("\nAvailable tools:")
        list_mcp_commands(commands)
        return

    if tool_name is None:
        print(
            "Error: no subcommand specified. Use --list to see available tools.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = await session.call_tool(tool_name, arguments or {})

    text = _extract_content_parts(result.content)
    output_result(text, pretty=pretty, raw=raw, toon=toon)


async def _handle_resources(
    session, action: str, uri: str | None, pretty: bool, raw: bool, toon: bool
):
    if action == "list":
        result = await session.list_resources()
        data = [
            {
                "name": r.name,
                "uri": str(r.uri),
                "description": r.description or "",
                "mimeType": r.mimeType or "",
            }
            for r in result.resources
        ]
        output_result(data, pretty=pretty, raw=raw, toon=toon)
    elif action == "templates":
        result = await session.list_resource_templates()
        data = [
            {
                "name": t.name,
                "uriTemplate": str(t.uriTemplate),
                "description": t.description or "",
                "mimeType": t.mimeType or "",
            }
            for t in result.resourceTemplates
        ]
        output_result(data, pretty=pretty, raw=raw, toon=toon)
    elif action == "read":
        from pydantic import AnyUrl

        result = await session.read_resource(AnyUrl(uri))
        parts = []
        for content in result.contents:
            if hasattr(content, "text"):
                parts.append(content.text)
            elif hasattr(content, "blob"):
                parts.append(content.blob)
        text = "\n".join(parts) if parts else ""
        output_result(text, pretty=pretty, raw=raw, toon=toon)


async def _handle_prompts(
    session,
    action: str,
    name: str | None,
    arguments: dict | None,
    pretty: bool,
    raw: bool,
    toon: bool,
):
    if action == "list":
        result = await session.list_prompts()
        data = [
            {
                "name": p.name,
                "description": p.description or "",
                "arguments": [
                    {
                        "name": a.name,
                        "description": a.description or "",
                        "required": a.required or False,
                    }
                    for a in (p.arguments or [])
                ],
            }
            for p in result.prompts
        ]
        output_result(data, pretty=pretty, raw=raw, toon=toon)
    elif action == "get":
        result = await session.get_prompt(name, arguments or {})
        messages = []
        for msg in result.messages:
            content = msg.content
            if hasattr(content, "text"):
                messages.append({"role": msg.role, "content": content.text})
            else:
                messages.append(
                    {"role": msg.role, "content": json.dumps(content.model_dump())}
                )
        data = {"description": result.description or "", "messages": messages}
        output_result(data, pretty=pretty, raw=raw, toon=toon)


def _fetch_or_cache_mcp_tools(
    key: str,
    ttl: int,
    refresh: bool,
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
) -> list[dict]:
    """Load MCP tools from cache or fetch from server, caching the result."""
    if not refresh:
        cached = load_cached(f"{key}_tools", ttl)
        if cached is not None:
            return cached
    tools = _fetch_mcp_tools(
        source, is_stdio, auth_headers, env_vars,
        transport=transport, oauth_provider=oauth_provider,
    )
    save_cache(f"{key}_tools", tools)
    return tools


def _dispatch_mcp_call(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    tool_name: str | None,
    arguments: dict | None,
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key: str,
    ttl: int,
    refresh: bool,
    *,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    **extra,
) -> None:
    """Route to run_mcp_stdio or run_mcp_http based on is_stdio."""
    if is_stdio:
        run_mcp_stdio(
            source, env_vars, tool_name, arguments, list_mode,
            pretty, raw, cache_key, ttl, refresh, toon=toon, **extra,
        )
    else:
        run_mcp_http(
            source, auth_headers, tool_name, arguments, list_mode,
            pretty, raw, cache_key, ttl, refresh, toon=toon,
            transport=transport, oauth_provider=oauth_provider, **extra,
        )


def handle_mcp(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    remaining: list[str],
    list_mode: bool,
    pretty: bool,
    raw: bool,
    cache_key_override: str | None,
    ttl: int,
    refresh: bool,
    toon: bool = False,
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
    resource_action: str | None = None,
    resource_uri: str | None = None,
    prompt_action: str | None = None,
    prompt_name: str | None = None,
    prompt_arguments: dict | None = None,
    search_pattern: str | None = None,
    bake_config: BakeConfig | None = None,
):
    key = cache_key_override or cache_key_for(source)

    # Resource/prompt operations skip the tool flow entirely
    if resource_action or prompt_action:
        extra = dict(
            resource_action=resource_action,
            resource_uri=resource_uri,
            prompt_action=prompt_action,
            prompt_name=prompt_name,
            prompt_arguments=prompt_arguments,
        )
        _dispatch_mcp_call(
            source, is_stdio, auth_headers, env_vars,
            None, None, False, pretty, raw, key, ttl, refresh,
            toon=toon, transport=transport, oauth_provider=oauth_provider,
            **extra,
        )
        return

    if list_mode:
        if bake_config and (bake_config.include or bake_config.exclude or bake_config.methods):
            # Fetch tools, filter, then list — don't delegate to unfiltered path
            tools = _fetch_or_cache_mcp_tools(
                key, ttl, refresh, source, is_stdio, auth_headers, env_vars,
                transport=transport, oauth_provider=oauth_provider,
            )
            commands = extract_mcp_commands(tools)
            commands = filter_commands(
                commands, bake_config.include, bake_config.exclude, bake_config.methods,
            )
            print("\nAvailable tools:")
            list_mcp_commands(commands)
            return
        _dispatch_mcp_call(
            source, is_stdio, auth_headers, env_vars,
            None, None, True, pretty, raw, key, ttl, refresh,
            toon=toon, transport=transport, oauth_provider=oauth_provider,
            search_pattern=search_pattern,
        )
        return

    # We need tool list to build argparse, try cache first
    tools = _fetch_or_cache_mcp_tools(
        key, ttl, refresh, source, is_stdio, auth_headers, env_vars,
        transport=transport, oauth_provider=oauth_provider,
    )

    commands = extract_mcp_commands(tools)
    if bake_config:
        commands = filter_commands(
            commands, bake_config.include, bake_config.exclude, bake_config.methods,
        )

    if not remaining:
        print("Available tools:")
        list_mcp_commands(commands)
        print("\nUse --list for the same output, or provide a subcommand.")
        return

    pre = argparse.ArgumentParser(add_help=False)
    parser = build_argparse(commands, pre)
    args = parser.parse_args(remaining)

    if not hasattr(args, "_cmd"):
        parser.print_help()
        sys.exit(1)

    cmd: CommandDef = args._cmd

    if getattr(args, "stdin", False):
        arguments = read_stdin_json("MCP tool arguments")
    else:
        arguments = {}
        for p in cmd.params:
            val = getattr(args, p.name.replace("-", "_"), None)
            if val is not None:
                arguments[p.original_name] = coerce_value(val, p.schema)

    _dispatch_mcp_call(
        source, is_stdio, auth_headers, env_vars,
        cmd.tool_name, arguments, False, pretty, raw, key, ttl, refresh,
        toon=toon, transport=transport, oauth_provider=oauth_provider,
    )


def _fetch_mcp_tools(
    source: str,
    is_stdio: bool,
    auth_headers: list[tuple[str, str]],
    env_vars: dict[str, str],
    transport: str = "auto",
    oauth_provider: httpx.Auth | None = None,
) -> list[dict]:
    tools_result: list[dict] = []

    async def _extract_tools(session):
        result = await session.list_tools()
        tools_result.extend(
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
            }
            for t in result.tools
        )

    async def _run():
        nonlocal tools_result

        if is_stdio:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            parts = shlex.split(source)
            env = {**os.environ, **env_vars}
            params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await _extract_tools(session)
        else:
            from mcp import ClientSession

            headers = dict(auth_headers) if auth_headers else None

            async def _via_streamable():
                from mcp.client.streamable_http import streamablehttp_client

                async with streamablehttp_client(
                    source, headers=headers, auth=oauth_provider
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _extract_tools(session)

            async def _via_sse():
                from mcp.client.sse import sse_client

                async with sse_client(source, headers=headers, auth=oauth_provider) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _extract_tools(session)

            if transport == "sse":
                await _via_sse()
            elif transport == "streamable":
                await _via_streamable()
            else:  # auto
                try:
                    await _via_streamable()
                except Exception:
                    await _via_sse()

    anyio.run(_run)
    return tools_result
