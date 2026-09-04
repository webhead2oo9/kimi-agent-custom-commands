"""Module declaration and load-time LLM-tool registration."""

from __future__ import annotations

from kimi_agent_module_api import AppModule, ModuleLoadContext, ModulePermissions, ModuleSpec
from kimi_agent_module_api.trust import TrustTier

from kimi_agent_custom_commands.module import MODULE_NAME, CustomCommandsModule

VERSION = "0.2.0"

_BLOCK_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "type": {"const": "heading"},
                "text": {"type": "string", "minLength": 1, "maxLength": 256},
                "url": {"type": ["string", "null"]},
            },
            "required": ["type", "text"],
            "additionalProperties": False,
        },
        *(
            {
                "type": "object",
                "properties": {
                    "type": {"const": kind},
                    "text": {"type": "string", "minLength": 1, "maxLength": maximum},
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            }
            for kind, maximum in (("text", 4_000), ("small", 2_000))
        ),
        {
            "type": "object",
            "properties": {
                "type": {"const": "field"},
                "name": {"type": "string", "minLength": 1, "maxLength": 256},
                "value": {"type": "string", "minLength": 1, "maxLength": 1_024},
            },
            "required": ["type", "name", "value"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"type": {"const": "divider"}},
            "required": ["type"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"const": "images"},
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                },
            },
            "required": ["type", "urls"],
            "additionalProperties": False,
        },
    ]
}

_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"const": 1},
        "pages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 25,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "minLength": 1, "maxLength": 32},
                    "label": {"type": "string", "minLength": 1, "maxLength": 100},
                    "description": {"type": "string", "maxLength": 100},
                    "accent_color": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 16_777_215,
                    },
                    "thumbnail_url": {"type": ["string", "null"]},
                    "blocks": {
                        "type": "array",
                        "items": _BLOCK_SCHEMA,
                        "minItems": 1,
                        "maxItems": 25,
                    },
                },
                "required": ["key", "label", "blocks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["version", "pages"],
    "additionalProperties": False,
}


def create(ctx: ModuleLoadContext) -> AppModule:
    module = CustomCommandsModule()
    ctx.registry.register(
        "custom_commands_read",
        "List, search, or inspect this server's custom slash commands.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional exact name or search text."},
            },
            "additionalProperties": False,
        },
        module.tool_read,
        min_tier=TrustTier.STAFF,
        searchable=True,
        guild_only=True,
    )
    ctx.registry.register(
        "custom_commands_propose",
        "Propose a complete custom-command create, replacement/rename, or deletion for staff approval.",
        {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "replace", "delete"]},
                "name": {
                    "type": "string",
                    "description": "Current command name, or new name for create.",
                },
                "new_name": {"type": "string", "description": "Replacement name when renaming."},
                "description": {"type": "string"},
                "content": _CONTENT_SCHEMA,
                "summary": {"type": "string", "description": "Short explanation for reviewers."},
            },
            "required": ["operation", "name", "summary"],
            "allOf": [
                {
                    "if": {"properties": {"operation": {"enum": ["create", "replace"]}}},
                    "then": {"required": ["description", "content"]},
                }
            ],
            "additionalProperties": False,
        },
        module.tool_propose,
        min_tier=TrustTier.STAFF,
        searchable=True,
        guild_only=True,
    )
    ctx.register_tool_labels(
        {
            "custom_commands_read": "Reading custom commands",
            "custom_commands_propose": "Preparing a custom command proposal",
        }
    )
    return module


SPEC = ModuleSpec(
    name=MODULE_NAME,
    version=VERSION,
    create=create,
    api_version=2,
    requires_capabilities=(
        "discord.guild_commands.v1",
        "discord.modals.v1",
        "discord.components_v2.v1",
    ),
    permissions=ModulePermissions(discord_actions=frozenset({"send_message"})),
)

__all__ = ["SPEC", "VERSION", "create"]
