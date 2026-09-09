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
    try:
        return Plan.model_validate(arguments).model_dump()
    except ValidationError as exc:
        raise ValueError(
            f"{TOOL_NAME} rejected — fix these fields and call it again: {exc}"
        ) from exc
