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

# The pace assumed when the user never chose one, kg per week.
DEFAULT_PACE_KG = {
    "lose": 0.6,
    "gain": 0.3,
}

# Kilocalories in a kilogram of body mass — what turns a chosen pace
# (kg per week) into a daily calorie shift.
KCAL_PER_KG = 7700

# How far under TDEE a day may be planned. A pace is a wish, and past this one
# it stops describing a week of food; the tool honours it up to here and says
# what pace that actually buys.
MIN_TDEE_FACTOR = 0.7

# A day at roughly seven times its target is not a badly planned day — it is
# the week's totals pasted into one row. A check that only says "off target"
# sends the model back to rewrite a ration that was never wrong, and that loop
# is what burns a run's whole step budget. So when a number is off by an order
# of magnitude, the tools name the likely mistake instead.
WEEKLY_TOTALS_BAND = (5.0, 9.0)

# Below the weekly band but still far past any real day: a few days summed
# together, or the cart counted as one day. Both are input mistakes too.
MULTI_DAY_RATIO = 2.0


def calc_targets(
    weight_kg: float,
    height_cm: float,
    age: int,
    sex: str,
    goal: str,
    workouts_per_week: int = 0,
    target_weight_kg: float | None = None,
    weekly_pace_kg: float = 0.0,
) -> dict[str, Any]:
    """Mifflin-St Jeor BMR, activity factor, then a goal-driven calorie shift.

    `weekly_pace_kg` is the pace the user picked in onboarding. Given one, the
    shift is computed from it instead of from a fixed percentage, so the plan
    and the user's own choice cannot say different things. It is capped: a pace
    that would push the day under `MIN_TDEE_FACTOR` of TDEE, or under BMR, is
    followed only as far as the cap, and the pace that cap actually buys comes
    back in `weekly_pace_kg` together with a hint saying so.
    """
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

    pace = abs(float(weekly_pace_kg or 0.0))
    hint = ""
    if pace and goal in ("lose", "gain"):
        shift = pace * KCAL_PER_KG / 7
        calories = tdee - shift if goal == "lose" else tdee + shift
        floor = max(tdee * MIN_TDEE_FACTOR, bmr)
        if calories < floor:
            calories = floor
            capped = (tdee - calories) * 7 / KCAL_PER_KG
            hint = (
                f"{pace} kg/week would mean eating under {round(floor)} kcal a day; the targets "
                f"below hold the safe floor instead, which is about {round(capped, 2)} kg/week. Say so "
                "in summary.notes."
            )
            pace = capped
    else:
        calories = tdee * (1 + GOAL_ADJUSTMENT.get(goal, 0.0))
        pace = DEFAULT_PACE_KG.get(goal, 0.0)

    protein_g = weight_kg * (2.0 if goal == "lose" else 1.8)
    fat_g = weight_kg * 0.9
    carbs_g = max((calories - protein_g * 4 - fat_g * 9) / 4, 0)

    weeks = 0.0
    goal_date = ""
    if target_weight_kg is not None and goal in ("lose", "gain") and pace > 0:
        delta = abs(weight_kg - target_weight_kg)
        weeks = round(delta / pace, 1)
        goal_date = (date.today() + timedelta(days=round(weeks * 7))).isoformat()

    result = {
        "bmr_kcal": round(bmr),
        "tdee_kcal": round(tdee),
        "activity_level": activity,
        "daily_kcal": round(calories),
        "weekly_kcal": round(calories * 7),
        "daily_protein_g": round(protein_g),
        "daily_fat_g": round(fat_g),
        "daily_carbs_g": round(carbs_g),
        "weekly_pace_kg": round(pace, 2),
        "estimated_weeks_to_goal": weeks,
        "estimated_goal_date": goal_date,
    }
    if hint:
        result["hint"] = hint
    return result


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
    result = {
        "weekly_totals": {k: round(v) for k, v in totals.items()},
        "weekly_kcal_target": target,
        "coverage_ratio": round(covered, 2),
        "verdict": _verdict(covered),
        "avg_daily_kcal": round(totals["kcal"] / days) if days else 0,
        "avg_daily_protein_g": round(totals["protein_g"] / days) if days else 0,
    }
    hint = _coverage_hint(covered, days)
    if hint:
        result["hint"] = hint
    return result


def _verdict(covered: float) -> str:
    if covered < 0.85:
        return "not enough food for the week"
    if covered > 1.15:
        return "too much food for the target"
    return "on target"


def _coverage_hint(covered: float, days: int) -> str:
    """Names the input mistake behind a coverage that is off by an order of magnitude."""
    if covered >= MULTI_DAY_RATIO:
        return (
            f"totals are {covered:.1f}x the {days}-day target — this is an input problem, not a "
            "ration problem: kcal_per_100g/protein_per_100g must be per 100 g, and total_grams "
            "the grams of that product for the whole ration. Fix the inputs before changing products."
        )
    if 0 < covered < 0.3:
        return (
            f"totals are only {covered:.2f}x the {days}-day target — check total_grams covers all "
            f"{days} days rather than one portion, before adding more products."
        )
    return ""


def check_budget(items: list[dict[str, Any]], budget_uah: float) -> dict[str, Any]:
    """Totals the planned cart and points at what to cut when it overruns.

    `line_items` comes back priced per position, and carries the product's slug
    and image straight through, so the model can keep working from one list
    rather than reassembling it by hand while it swaps products around.

    This is an estimate, not the bill. It is fed shelf prices from product
    search, and Silpo applies the user's personal and promo discounts only when
    it calculates the cart — so the real total lands several percent lower. The
    prices that reach the plan are read back off the filled cart; these ones
    exist to keep the model inside the budget while it is still choosing.
    """
    priced = []
    for item in items:
        price = float(item.get("price", 0))
        quantity = float(item.get("quantity", 1) or 1)
        priced.append(
            {
                "name": item.get("name", "?"),
                "slug": item.get("slug", ""),
                "image_url": item.get("image_url", ""),
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
    hints: list[str] = []
    for day in days:
        name = str(day.get("day") or "?")
        kcal = float(day.get("kcal", 0) or 0)
        protein = float(day.get("protein_g", 0) or 0)
        ratio = kcal / daily_kcal if daily_kcal else 0.0
        enough_protein = protein >= daily_protein_g * (1 - tolerance) if daily_protein_g else True
        ok = abs(ratio - 1) <= tolerance and enough_protein
        row = {
            "day": name,
            "kcal": round(kcal),
            "kcal_ratio": round(ratio, 2),
            "protein_g": round(protein),
            "ok": ok,
        }
        protein_ratio = protein / daily_protein_g if daily_protein_g else 0.0
        hint = _scale_hint(ratio, "kcal") or _scale_hint(protein_ratio, "protein")
        if hint:
            row["hint"] = hint
            if hint not in hints:
                hints.append(hint)
        rows.append(row)
        if not ok:
            off_target.append(name)

    if off_target and hints:
        verdict = f"{len(off_target)} day(s) off target — read the hints first, the numbers look wrong"
    elif off_target:
        verdict = f"{len(off_target)} day(s) off target"
    else:
        verdict = "all days on target"

    result = {
        "daily_kcal_target": daily_kcal,
        "daily_protein_g_target": daily_protein_g,
        "tolerance": tolerance,
        "days": rows,
        "days_off_target": off_target,
        "verdict": verdict,
    }
    if hints:
        result["hints"] = hints
    return result


def _scale_hint(ratio: float, subject: str) -> str:
    """Explains a day that misses its target by an order of magnitude."""
    low, high = WEEKLY_TOTALS_BAND
    if low <= ratio <= high:
        return (
            f"{subject} is {ratio:.1f}x the daily target — these look like the whole week's totals "
            "in one row. Send each day's own totals (divide by 7); the ration itself is probably fine."
        )
    if ratio > MULTI_DAY_RATIO:
        return (
            f"{subject} is {ratio:.1f}x the daily target — that is several days' food in one row, "
            "not a mis-planned day. Send one day's own totals; do not rewrite the ration for this."
        )
    if 0 < ratio < 0.4:
        return (
            f"{subject} is {ratio:.2f}x the daily target — check this is the day's total across all "
            "four meals, not a single meal."
        )
    return ""


DECLARATIONS = [
    {
        "type": "function",
        "name": "calc_targets",
        "description": (
            "Calculates BMR, TDEE, daily calorie/macro targets and the estimated time to "
            "reach the goal weight. Always call this before planning a ration. Pass the "
            "user's chosen weekly pace when the request carries one — the calorie deficit "
            "and the estimated time to goal are both built from it."
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
                "weekly_pace_kg": {
                    "type": "number",
                    "description": (
                        "The pace of weight change the user chose, kg per week, as given in "
                        "the user message. Omit it only when the message has none."
                    ),
                },
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
                            "total_grams": {
                                "type": "number",
                                "description": "Grams of this product across the whole ration (all `days` days)",
                            },
                            "quantity": {
                                "type": "number",
                                "description": "Packs, used only when total_grams is unknown",
                            },
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
            "Estimates the planned cart cost from search prices, flags budget overrun and "
            "returns per-position totals, slugs and images. An upper bound only: the user's "
            "personal and promo discounts are applied by Silpo when it calculates the cart, "
            "so the plan's final prices must be read back from silpo_get_shopping_cart_by_id."
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
                            "slug": {"type": "string", "description": "Product slug from MCP"},
                            "image_url": {"type": "string", "description": "Product image URL from MCP"},
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
            "the days that are off. Call it once all seven days are drafted, before finalize_plan. "
            "Every entry is ONE day's own totals across its four meals — never the week's totals "
            "and never the whole cart."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "array",
                    "description": "The seven planned days, each with that day's own totals",
                    "items": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "string", "description": "monday…sunday"},
                            "kcal": {"type": "number", "description": "That day's kcal, not the week's"},
                            "protein_g": {"type": "number", "description": "That day's protein, g"},
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
