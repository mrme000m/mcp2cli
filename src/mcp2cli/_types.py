"""Core data structures and command utilities."""

from __future__ import annotations

import argparse
import fnmatch
from dataclasses import dataclass, field


@dataclass
class ParamDef:
    name: str  # kebab-case CLI flag
    original_name: str  # original name for API/tool call
    python_type: type | None  # None means boolean (store_true)
    required: bool = False
    description: str = ""
    choices: list | None = None
    location: str = "body"  # path|query|header|body|tool_input
    schema: dict = field(default_factory=dict)


@dataclass
class CommandDef:
    name: str
    description: str = ""
    params: list[ParamDef] = field(default_factory=list)
    has_body: bool = False
    # OpenAPI
    method: str | None = None
    path: str | None = None
    # MCP
    tool_name: str | None = None
    # GraphQL
    graphql_operation_type: str | None = None  # "query" or "mutation"
    graphql_field_name: str | None = None      # original field name pre-kebab
    graphql_return_type: dict | None = None    # return type info for selection set


@dataclass
class BakeConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


def filter_commands(
    commands: list[CommandDef],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    methods: list[str] | None = None,
) -> list[CommandDef]:
    """Filter commands by HTTP method, include whitelist, and exclude blacklist.

    Order: methods filter -> include whitelist -> exclude blacklist.
    MCP commands (method is None) pass the methods filter unchanged.
    """
    result = commands
    if methods:
        upper = [m.upper() for m in methods]
        result = [c for c in result if c.method is None or c.method.upper() in upper]
    if include:
        result = [
            c for c in result
            if any(fnmatch.fnmatch(c.name, pat) for pat in include)
        ]
    if exclude:
        result = [
            c for c in result
            if not any(fnmatch.fnmatch(c.name, pat) for pat in exclude)
        ]
    return result


def build_argparse(
    commands: list[CommandDef], pre_parser: argparse.ArgumentParser
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp2cli",
        description="Turn any MCP server or OpenAPI spec into a CLI",
        parents=[pre_parser],
    )
    subparsers = parser.add_subparsers(dest="_command")

    for cmd in commands:
        sub = subparsers.add_parser(cmd.name, help=cmd.description)
        sub.set_defaults(_cmd=cmd)

        if cmd.has_body:
            sub.add_argument(
                "--stdin",
                action="store_true",
                default=False,
                help="Read JSON body/arguments from stdin",
            )

        seen_flags: set[str] = set()
        for p in cmd.params:
            flag = f"--{p.name}"
            if flag in seen_flags:
                continue  # skip duplicate param names (e.g. path + body both have same name)
            seen_flags.add(flag)
            kwargs: dict = {}
            if p.python_type is not None:
                kwargs["type"] = p.python_type
            else:
                kwargs["action"] = "store_true"
            # Body/tool_input params are never argparse-required (--stdin bypasses them)
            if (
                p.required
                and "action" not in kwargs
                and p.location not in ("body", "tool_input", "graphql_arg")
            ):
                kwargs["required"] = True
            else:
                kwargs.setdefault("default", None)
            kwargs["help"] = p.description
            if p.choices:
                kwargs["choices"] = p.choices
            sub.add_argument(flag, **kwargs)

    return parser
