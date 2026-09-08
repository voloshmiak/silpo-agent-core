"""Deterministic helpers the model calls instead of doing arithmetic itself."""

from datetime import date, timedelta
from typing import Any

MACRO_KEYS = ("kcal", "protein_g", "fat_g", "carbs_g")

ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
}

GOAL_ADJUSTMENT = {
    "lose": -0.20,
    "maintain": 0.0,
    "gain": 0.15,
}


def calc_targets(
    weight_kg: float,
    height_cm: float,
    age: int,
    sex: str,
    goal: str,
    workouts_per_week: int = 0,
    target_weight_kg: float | None = None,
) -> dict[str, Any]:
    """Mifflin-St Jeor BMR, activity factor, then a goal-driven calorie shift."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    bmr = base + 5 if sex.lower().startswith("m") else base - 161

    if workouts_per_week >= 5:
        activity = "high"
    elif workouts_per_week >= 3:
        activity = "moderate"
    elif workouts_per_week >= 1:
        activity = "light"
    else:
        activity = "sedentary"

    tdee = bmr * ACTIVITY_FACTORS[activity]
    calories = tdee * (1 + GOAL_ADJUSTMENT.get(goal, 0.0))

    protein_g = weight_kg * (2.0 if goal == "lose" else 1.8)
    fat_g = weight_kg * 0.9
    carbs_g = max((calories - protein_g * 4 - fat_g * 9) / 4, 0)

    weeks = 0.0
    goal_date = ""
    if target_weight_kg is not None and goal in ("lose", "gain"):
        delta = abs(weight_kg - target_weight_kg)
        weekly_change = 0.6 if goal == "lose" else 0.3
        weeks = round(delta / weekly_change, 1)
        goal_date = (date.today() + timedelta(days=round(weeks * 7))).isoformat()

    return {
        "bmr_kcal": round(bmr),
        "tdee_kcal": round(tdee),
        "activity_level": activity,
        "daily_kcal": round(calories),
        "weekly_kcal": round(calories * 7),
        "daily_protein_g": round(protein_g),
        "daily_fat_g": round(fat_g),
        "daily_carbs_g": round(carbs_g),
        "estimated_weeks_to_goal": weeks,
        "estimated_goal_date": goal_date,
    }


def check_nutrition(items: list[dict[str, Any]], daily_kcal: int, days: int = 7) -> dict[str, Any]:
    """Sums the nutrition of the picked products against the weekly target."""
    totals = {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    for item in items:
        portions = float(item.get("total_grams", 0)) / 100 or float(item.get("quantity", 1))
        for key, source in (
            ("kcal", "kcal_per_100g"),
            ("protein_g", "protein_per_100g"),
            ("fat_g", "fat_per_100g"),
            ("carbs_g", "carbs_per_100g"),
        ):
            totals[key] += float(item.get(source, 0)) * portions

    target = daily_kcal * days
    covered = totals["kcal"] / target if target else 0
    return {
        "weekly_totals": {k: round(v) for k, v in totals.items()},
        "weekly_kcal_target": target,
        "coverage_ratio": round(covered, 2),
        "verdict": _verdict(covered),
        "avg_daily_kcal": round(totals["kcal"] / days) if days else 0,
        "avg_daily_protein_g": round(totals["protein_g"] / days) if days else 0,
    }


def _verdict(covered: float) -> str:
    if covered < 0.85:
        return "not enough food for the week"
    if covered > 1.15:
        return "too much food for the target"
    return "on target"


def check_budget(items: list[dict[str, Any]], budget_uah: float) -> dict[str, Any]:
    """Totals the cart and points at what to cut when it overruns.

    `line_items` comes back priced per position so the plan's cart can carry
    the same numbers this tool computed, instead of the model re-deriving them.
    """
    priced = []
    for item in items:
        price = float(item.get("price", 0))
        quantity = float(item.get("quantity", 1) or 1)
        priced.append(
            {
                "name": item.get("name", "?"),
                "price": round(price, 2),
                "quantity": quantity,
                "total_price": round(price * quantity, 2),
            }
        )

    total = round(sum(p["total_price"] for p in priced), 2)
    return {
        "total_uah": total,
        "budget_uah": budget_uah,
        "remaining_uah": round(budget_uah - total, 2),
        "over_budget": total > budget_uah,
        "line_items": priced,
        "most_expensive": sorted(priced, key=lambda p: p["total_price"], reverse=True)[:5],
    }


def sum_macros(entries: list[dict[str, Any]], days: int = 1) -> dict[str, Any]:
    """Adds up kcal and macros over meals or whole days.

    Whatever the plan needs summed — the four meals of a day, the seven days of
    a week — goes through here rather than through the model's own arithmetic.
    An entry's `quantity` multiplies it.
    """
    totals = {key: 0.0 for key in MACRO_KEYS}
    for entry in entries:
        multiplier = float(entry.get("quantity", 1) or 1)
        for key in MACRO_KEYS:
            totals[key] += float(entry.get(key, 0) or 0) * multiplier

    days = max(int(days or 1), 1)
    return {
        "totals": {key: round(value) for key, value in totals.items()},
        "per_day": {key: round(value / days) for key, value in totals.items()},
        "days": days,
        "entries": len(entries),
    }


def check_plan_days(
    days: list[dict[str, Any]],
    daily_kcal: int,
    daily_protein_g: int = 0,
    tolerance: float = 0.1,
) -> dict[str, Any]:
    """Compares every planned day against the daily targets.

    Catches days that drifted before the plan is finalized, so the ration and
    the targets it claims to hit cannot disagree.
    """
    rows = []
    off_target = []
    for day in days:
        name = str(day.get("day") or "?")
        kcal = float(day.get("kcal", 0) or 0)
        protein = float(day.get("protein_g", 0) or 0)
        ratio = kcal / daily_kcal if daily_kcal else 0.0
        enough_protein = protein >= daily_protein_g * (1 - tolerance) if daily_protein_g else True
        ok = abs(ratio - 1) <= tolerance and enough_protein
        rows.append(
            {
                "day": name,
                "kcal": round(kcal),
                "kcal_ratio": round(ratio, 2),
                "protein_g": round(protein),
                "ok": ok,
            }
        )
        if not ok:
            off_target.append(name)

    return {
        "daily_kcal_target": daily_kcal,
        "daily_protein_g_target": daily_protein_g,
        "tolerance": tolerance,
        "days": rows,
        "days_off_target": off_target,
        "verdict": "all days on target" if not off_target else f"{len(off_target)} day(s) off target",
    }


DECLARATIONS = [
    {
        "type": "function",
        "name": "calc_targets",
        "description": (
            "Calculates BMR, TDEE, daily calorie/macro targets and the estimated time to "
            "reach the goal weight. Always call this before planning a ration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number", "description": "Current weight in kg"},
                "height_cm": {"type": "number", "description": "Height in cm"},
                "age": {"type": "integer", "description": "Age in years"},
                "sex": {"type": "string", "enum": ["male", "female"]},
                "goal": {"type": "string", "enum": ["lose", "maintain", "gain"]},
                "workouts_per_week": {"type": "integer", "description": "Workouts per week, 0-7"},
                "target_weight_kg": {"type": "number", "description": "Goal weight in kg"},
            },
            "required": ["weight_kg", "height_cm", "age", "sex", "goal"],
        },
    },
    {
        "type": "function",
        "name": "check_nutrition",
        "description": (
            "Sums calories and macros of the chosen products over the week and compares "
            "them with the daily calorie target."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Chosen products with per-100g nutrition and total grams",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "total_grams": {"type": "number"},
                            "quantity": {"type": "number"},
                            "kcal_per_100g": {"type": "number"},
                            "protein_per_100g": {"type": "number"},
                            "fat_per_100g": {"type": "number"},
                            "carbs_per_100g": {"type": "number"},
                        },
                        "required": ["name"],
                    },
                },
                "daily_kcal": {"type": "integer"},
                "days": {"type": "integer", "description": "Days the ration covers, default 7"},
            },
            "required": ["items", "daily_kcal"],
        },
    },
    {
        "type": "function",
        "name": "check_budget",
        "description": (
            "Totals the planned cart cost, flags budget overrun and returns the per-position "
            "prices to put into the plan's cart."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "price": {"type": "number", "description": "Price per unit in UAH"},
                            "quantity": {"type": "number"},
                        },
                        "required": ["name", "price"],
                    },
                },
                "budget_uah": {"type": "number"},
            },
            "required": ["items", "budget_uah"],
        },
    },
    {
        "type": "function",
        "name": "sum_macros",
        "description": (
            "Adds up calories and macros over a list of meals or days. Use it for every total "
            "in the plan — a day's totals from its four meals, the week's from the seven days — "
            "instead of adding the numbers up yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "description": "Meals or days to add up",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "carbs_g": {"type": "number"},
                            "quantity": {"type": "number", "description": "Multiplier, default 1"},
                        },
                    },
                },
                "days": {"type": "integer", "description": "Days to average over, default 1"},
            },
            "required": ["entries"],
        },
    },
    {
        "type": "function",
        "name": "check_plan_days",
        "description": (
            "Checks each planned day's calories and protein against the daily targets and names "
            "the days that are off. Call it once all seven days are drafted, before finalize_plan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "array",
                    "description": "The planned days with their totals",
                    "items": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "string", "description": "monday…sunday"},
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                        },
                    },
                },
                "daily_kcal": {"type": "integer"},
                "daily_protein_g": {"type": "integer"},
                "tolerance": {"type": "number", "description": "Allowed deviation, default 0.1"},
            },
            "required": ["days", "daily_kcal"],
        },
    },
]

HANDLERS = {
    "calc_targets": calc_targets,
    "check_nutrition": check_nutrition,
    "check_budget": check_budget,
    "sum_macros": sum_macros,
    "check_plan_days": check_plan_days,
}
