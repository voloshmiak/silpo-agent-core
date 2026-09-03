"""Turns MCP tool definitions into Gemini function declarations."""

from typing import Any

from google.genai import types
from mcp.types import Tool

# The Silpo server exposes ~45 tools. Handing all of them to the model dilutes
# tool choice and burns context, so the agent only gets what the ration -> cart
# pipeline actually needs.
ALLOWED_TOOLS = (
    # who the user is and what they may not eat
    "silpo_get_my_profile",
    "silpo_get_my_food_restrictions",
    "silpo_get_my_favorites",
    "silpo_get_my_premium_subscription",
    # finding real products
    "silpo_find_products_batch",
    "silpo_get_products",
    "silpo_get_product_details",
    "silpo_get_categories_tree",
    "silpo_get_similar_products",
    "silpo_get_replacements",
    # discounts and personal offers
    "silpo_get_promotions",
    "silpo_get_my_promos",
    "silpo_get_my_coupons",
    # the cart itself
    "silpo_get_my_shopping_cart",
    "silpo_add_or_update_cart_products",
    "silpo_remove_cart_products",
    "silpo_clear_shopping_cart",
    # behaviour history for the weekly adaptation
    "silpo_get_my_online_orders",
    "silpo_get_my_offline_orders",
    "silpo_list_branches",
)

# Tools that change the user's real cart — gated behind --apply.
MUTATING_TOOLS = frozenset(
    {
        "silpo_add_or_update_cart_products",
        "silpo_remove_cart_products",
        "silpo_clear_shopping_cart",
        "silpo_update_shopping_cart",
    }
)

_SCHEMA_FIELDS = ("items", "additionalProperties")
_SCHEMA_LIST_FIELDS = ("anyOf", "oneOf")
_SCHEMA_DICT_FIELDS = ("properties", "$defs")


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drops JSON Schema keywords the Gemini API does not accept."""
    supported = set(types.JSONSchema.model_fields) | {
        "additionalProperties",
        "anyOf",
        "oneOf",
        "$defs",
        "$ref",
    }
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _SCHEMA_FIELDS and isinstance(value, dict):
            out[key] = sanitize_schema(value)
        elif key in _SCHEMA_LIST_FIELDS and isinstance(value, list):
            out[key] = [sanitize_schema(v) for v in value if isinstance(v, dict)]
        elif key in _SCHEMA_DICT_FIELDS and isinstance(value, dict):
            out[key] = {k: sanitize_schema(v) for k, v in value.items()}
        elif key in supported:
            out[key] = value
    return out


def mcp_tool_to_declaration(tool: Tool) -> dict[str, Any]:
    parameters = sanitize_schema(tool.input_schema or {"type": "object", "properties": {}})
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})
    return {
        "type": "function",
        "name": tool.name,
        "description": (tool.description or tool.title or tool.name)[:1024],
        "parameters": parameters,
    }


def select_tools(tools: list[Tool]) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns declarations for the allowlisted tools plus any names not found."""
    by_name = {tool.name: tool for tool in tools}
    declarations = [mcp_tool_to_declaration(by_name[n]) for n in ALLOWED_TOOLS if n in by_name]
    missing = [n for n in ALLOWED_TOOLS if n not in by_name]
    return declarations, missing
