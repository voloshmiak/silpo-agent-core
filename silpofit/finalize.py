"""The agent's only 'memory' tool: capturing the finished plan for the caller.

The agent is stateless — it does not persist anything itself. Its last tool
call in every run must be finalize_plan; the agent loop intercepts that call
and hands the structured payload back to the caller (an HTTP backend) as
`plan_to_persist`, instead of writing it anywhere locally.
"""

from typing import Any

TOOL_NAME = "finalize_plan"

DECLARATION = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Records the finished ration and cart. This must be the LAST tool call of the "
        "run — call it once, after the cart (or, for a review, the analysis) is settled, "
        "then give your final text answer with no further tool calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ration_summary": {
                "type": "string",
                "description": "Short description of the week's ration, day by day",
            },
            "cart_items": {
                "type": "array",
                "description": "Final cart, or [] for a review run: product name, id, quantity, price",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "product_id": {"type": "string"},
                        "quantity": {"type": "number"},
                        "price": {"type": "number"},
                    },
                    "required": ["name"],
                },
            },
            "budget_uah": {"type": "number"},
            "targets": {
                "type": "object",
                "description": "Calorie and macro targets used for this plan",
                "properties": {
                    "daily_kcal": {"type": "integer"},
                    "daily_protein_g": {"type": "integer"},
                    "daily_fat_g": {"type": "integer"},
                    "daily_carbs_g": {"type": "integer"},
                },
            },
            "notes": {
                "type": "string",
                "description": "What was adapted compared with the previous plan and why",
            },
        },
        "required": ["ration_summary", "cart_items"],
    },
}


def ack(_: dict[str, Any]) -> str:
    return "recorded"
