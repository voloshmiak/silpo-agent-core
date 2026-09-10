import logging
from typing import Any

from google.genai import types
from mcp.types import Tool

log = logging.getLogger(__name__)

ALLOWED_TOOLS = (
    "silpo_get_my_profile",
    "silpo_get_my_food_restrictions",
    "silpo_get_my_favorites",
    "silpo_find_products_batch",
    "silpo_get_product_details",
    "silpo_get_similar_products",
    "silpo_get_replacements",
    "silpo_get_promotions",
    "silpo_get_my_promos",
    "silpo_get_my_coupons",
    "silpo_get_my_shopping_cart",
    "silpo_get_shopping_cart_by_id",
    "silpo_create_shopping_cart",
    "silpo_update_shopping_cart",
    "silpo_get_my_delivery_addresses",
    "silpo_get_available_delivery_types",
    "silpo_get_time_slots",
    "silpo_add_or_update_cart_products",
    "silpo_remove_cart_products",
    "silpo_list_branches",
)

MUTATING_TOOLS = frozenset(
    {
        "silpo_add_or_update_cart_products",
        "silpo_remove_cart_products",
        "silpo_clear_shopping_cart",
        "silpo_update_shopping_cart",
    }
)

SEARCH_TOOL = "silpo_find_products_batch"
SEARCH_LIMIT_DEFAULT = 6
SEARCH_LIMIT_MAX = 8


def normalize_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != SEARCH_TOOL:
        return arguments
    requested = arguments.get("limit")
    limit = (
        min(int(requested), SEARCH_LIMIT_MAX)
        if isinstance(requested, (int, float)) and requested > 0
        else SEARCH_LIMIT_DEFAULT
    )
    if limit == requested:
        return arguments
    log.info(
        "%s: limit %s -> %d over %d queries",
        name,
        requested if requested is not None else "unset (Silpo defaults to 30)",
        limit,
        len(arguments.get("products") or []),
    )
    return {**arguments, "limit": limit}


_SCHEMA_FIELDS = ("items", "additionalProperties")
_SCHEMA_LIST_FIELDS = ("anyOf", "oneOf")
_SCHEMA_DICT_FIELDS = ("properties", "$defs")


def _supported_keys() -> set[str]:
    keys: set[str] = set()
    for name, info in types.JSONSchema.model_fields.items():
        keys.add(name)
        if info.alias:
            keys.add(info.alias)
    return keys


SUPPORTED_KEYS = _supported_keys()


def sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    supported = SUPPORTED_KEYS
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
    by_name = {tool.name: tool for tool in tools}
    declarations = [mcp_tool_to_declaration(by_name[n]) for n in ALLOWED_TOOLS if n in by_name]
    missing = [n for n in ALLOWED_TOOLS if n not in by_name]
    return declarations, missing
