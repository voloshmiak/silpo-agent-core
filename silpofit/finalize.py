"""The agent's only 'memory' tool: capturing the finished plan for the caller.

The agent is stateless — it does not persist anything itself. Its last tool
call in every run must be finalize_plan; the agent loop intercepts that call,
validates the payload against `plan_schema.Plan` and hands it back to the
caller (an HTTP backend) as the run's result, instead of writing it anywhere
locally. The whole answer lives in this call's arguments — there is no final
text turn after it.

The tool's parameter schema is generated from the Pydantic model rather than
written out by hand, so what the model is asked for and what the API returns
can never drift apart.
"""

from typing import Any

from pydantic import ValidationError

from .plan_schema import Plan
from .tool_bridge import sanitize_schema

TOOL_NAME = "finalize_plan"

DECLARATION = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Records the finished weekly plan: daily calorie and macro targets, the estimated "
        "time to reach the goal, all 7 days of the ration with breakfast/lunch/snack/dinner, "
        "the final cart and the summary. This must be the LAST tool call of the run — the "
        "run ends here, so everything the user should see must be inside these arguments. "
        "Ukrainian for every human-readable text."
    ),
    "parameters": sanitize_schema(Plan.model_json_schema()),
}


def validate(arguments: dict[str, Any]) -> dict[str, Any]:
    """Returns the normalized plan, or raises with a message the model can act on."""
    try:
        return Plan.model_validate(arguments).model_dump()
    except ValidationError as exc:
        raise ValueError(
            f"{TOOL_NAME} rejected — fix these fields and call it again: {exc}"
        ) from exc