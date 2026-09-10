"""Deterministic helpers the model calls instead of doing arithmetic itself."""

from typing import Any
import httpx


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

    weeks = None
    if target_weight_kg is not None and goal in ("lose", "gain"):
        delta = abs(weight_kg - target_weight_kg)
        weekly_change = 0.6 if goal == "lose" else 0.3
        weeks = round(delta / weekly_change, 1)

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
    """Totals the cart and points at what to cut when it overruns."""
    priced = []
    for item in items:
        price = float(item.get("price", 0))
        quantity = float(item.get("quantity", 1))
        priced.append({"name": item.get("name", "?"), "cost": round(price * quantity, 2)})

    total = round(sum(p["cost"] for p in priced), 2)
    priced.sort(key=lambda p: p["cost"], reverse=True)
    return {
        "total_uah": total,
        "budget_uah": budget_uah,
        "remaining_uah": round(budget_uah - total, 2),
        "over_budget": total > budget_uah,
        "most_expensive": priced[:5],
    }


def search_silpo_recipes(query: str = "") -> list[dict[str, Any]]:
    """Search recipes from API"""
    url = "https://sf-ecom-api.silpo.ua/v1/recipes?limit=12&offset=0"
    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "origin": "https://silpo.ua"
    }

    try:
        with httpx.Client() as client:
            response = client.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json()

        raw_recipes = data.get("items", [])
        results = []

        for r in raw_recipes:
            title = r.get("title")
            slug = r.get("slug")
            time = r.get("cookingTime", 0)

            # Витягуємо інгредієнти
            ingredients = []
            for ing in r.get("ingredients", []):
                name = ing.get("name", "")
                measure = ing.get("measure", {})
                qty = measure.get("quantity", "")
                unit = measure.get("unit", "")
                ingredients.append(f"{name} ({qty} {unit})".strip())

            if title and slug:
                results.append({
                    "title": title,
                    "time_minutes": time,
                    "link": f"https://silpo.ua/recipes/{slug}",
                    "ingredients": ingredients
                })

        if query:
            q = query.lower()
            results = [r for r in results if q in r["title"].lower()]

        return results[:8]
    except Exception as e:
        print(f"[API Recipe Error] {e}")
        return []

DECLARATIONS = [
    {
        "type": "function",
        "name": "calc_targets",
        "description": (
            "Calculates BMR, TDEE and daily calorie/macro targets from the user's body "
            "metrics and goal. Always call this before planning a ration."
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
        "description": "Totals the planned cart cost and flags budget overrun with the priciest items.",
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
        "name": "search_silpo_recipes",
        "description": "Searches for real recipes in the Silpo database. Returns the title, cooking time, link, and a list of ingredients. Always use this to create the menu and add the correct products to the cart.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword (e.g., 'salad', 'soup', 'dessert'). Leave empty to fetch a general list."
                }
            }
        }
    }

]

HANDLERS = {
    "calc_targets": calc_targets,
    "check_nutrition": check_nutrition,
    "check_budget": check_budget,
    "search_silpo_recipes": search_silpo_recipes,
}
