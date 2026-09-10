# silpo-agent-core — HTTP and tool contract

The contract between the SilpoFit backend and this service. Both halves are
defined once in Python and must be changed there, never described twice:
`silpofit/request_schema.py` (input) and `silpofit/plan_schema.py` (output).

## Endpoints

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/health` | none | `{"status": "ok"}` |
| POST | `/plan` | service bearer | `Plan` (JSON) |
| POST | `/plan/stream` | service bearer | `text/event-stream` |

`GET /docs` serves the generated OpenAPI UI. The Authorize button there takes a
value from `SILPOFIT_SERVICE_TOKENS` — that is the *backend's* token, not the
user's Silpo token.

### Status codes

| Code | Meaning | Backend should |
|---|---|---|
| 200 | plan produced | store it as the week's plan |
| 401 | invalid or missing service token | fix deployment config; never surface to the user |
| 409 | `{"code": "silpo_token_expired"}` | refresh the user's Silpo OAuth token and retry |
| 422 | request failed validation | a field is mis-wired; the message names the field and the allowed values |
| 500 | `SILPOFIT_SERVICE_TOKENS` not configured | deployment problem |
| 502 | the agent run failed; message carries `run_id=…` | retry once, then report with the run id |

### SSE frames (`POST /plan/stream`)

One JSON object per `data:` line, every frame stamped with `run_id`:

```
data: {"type":"tool_call","tool":"silpo_find_products_batch","args":{…},"run_id":"9f2c1ab4"}
data: {"type":"tool_result","tool":"silpo_find_products_batch","ok":true,"result":"…","run_id":"9f2c1ab4"}
data: {"type":"validation","ok":false,"round":1,"source":"audit","accepted":false,"issues":[{"where":"summary.total_uah","problem":"…","fix":"…","source":"audit"}],"run_id":"9f2c1ab4"}
data: {"type":"plan","plan":{…},"run_id":"9f2c1ab4"}
```

* `tool_call` / `tool_result` — progress only; `result` is truncated to 200 chars.
* `validation` — one frame per review of a finished plan, right after the
  `finalize_plan` tool result. `ok: false` with `accepted: false` means the plan
  went back to the agent to be fixed and the run continues; `accepted: true`
  with `ok: false` means that kind of rejection ran out of rounds and the plan was
  let through with those issues unresolved. `source` is `audit` (deterministic
  check) or `review` (the reviewing model), and the two have separate round
  budgets, so `round` counts every validation while the budget that ran out is the
  one named in `source`. Progress only — the backend never needs to act
  on it, but it is the honest place to show «перевіряю план» in the UI.
* Terminal frame is **always** `plan` or `error`. A stream that ends with neither
  means the connection dropped — that is the one failure the server cannot report.
* `error` may carry `code: "silpo_token_expired"`, plus `finish_reason`, `step`
  and the model's trailing `text` when the run stopped short of `finalize_plan`.
* HTTP status is already 200 by the time a run can fail here, so failures are
  `error` events, not error statuses.

## Request — `PlanRequest`

| Field | Type | Default | Notes |
|---|---|---|---|
| `silpo_access_token` | str | required | the end user's Silpo OAuth token; the backend owns the flow |
| `profile.weight_kg` | float > 0 | required | |
| `profile.target_weight_kg` | float > 0 | required | |
| `profile.height_cm` | float | `null` | missing ⇒ the agent assumes and says so in `summary.notes` |
| `profile.age` | int | `null` | same |
| `profile.sex` | `male` \| `female` | `null` | accepts «чоловіча», «жін.», `m`, `f`… |
| `goal` | `lose` \| `maintain` \| `gain` | derived | derived from current vs target weight when absent; accepts «Схуднення», «набір маси»… |
| `weekly_pace_kg` | float | `0.0` | magnitude only — the sign is ignored, `goal` gives direction. Rejected above `2.0` kg/week as a unit mistake. `0` ⇒ the user chose no pace |
| `budget_uah` | float ≥ 0 | required | weekly grocery budget |
| `promo_priority` | `low` \| `medium` \| `high` | `medium` | how hard to chase promotions; accepts «високий», «тільки акції»… |
| `delivery_included` | bool | `true` | `true` ⇒ `remaining_uah = budget − total_uah`; `false` ⇒ `budget − products_total_uah` |
| `workouts_per_week` | int 0–7 | `0` | filled in from `workout_schedule`'s length when omitted |
| `workout_schedule` | dict day → str | `{}` | e.g. `{"monday": "силові"}`; keys accept «ПН», `mon`, `понеділок` and normalize to `monday`…`sunday` |
| `diet_type` | `none` \| `vegetarian` \| `vegan` \| `keto` \| `paleo` \| `low_fodmap` | `none` | accepts «кето», «веганська»…; each expands into an explicit rule in the prompt |
| `allergens` | list[str] | `[]` | hard ban, including hidden sources in the composition |
| `excluded_products` | list[str] | `[]` | stop-products |
| `fridge_items` | list[str] | `[]` | already at home — subtracted from the shopping list |
| `note` | str | `""` | the user's own free text, passed through verbatim. **This is where end-of-week dish feedback arrives** |
| `previous_plan` | object | `null` | last week's `Plan` JSON. **This is the whole adaptation channel** — the agent weighs what was actually bought |
| `apply` | bool | `false` | `true` writes the real Silpo cart; `false` plans it only |

**Normalization rules.** Enum-like values are matched casefolded with collapsed
whitespace against the alias tables in `request_schema.py`. A value that matches
nothing raises 422 listing the allowed identifiers — never a silent default. Lists
are stripped of blanks. Non-string values pass through untouched so a type error
still reads as a type error.

## Response — `Plan`

```jsonc
{
  "targets": {                       // from calc_targets, per day
    "kcal": 2100, "protein_g": 150, "fat_g": 67, "carbs_g": 210,
    "estimated_weeks_to_goal": 12.5,
    "estimated_goal_date": "2026-12-04"
  },
  "days": [                          // exactly 7, sorted monday…sunday
    {
      "day": "monday",
      "workout": true,
      "kcal": 2100, "protein_g": 150, "fat_g": 67, "carbs_g": 210,   // the day's totals
      "breakfast": { "title": "…", "items": ["вівсянка 60 г"], "kcal": 480, "protein_g": 22, "fat_g": 12, "carbs_g": 68 },
      "lunch":     { … }, "snack": { … }, "dinner": { … }
    }
  ],
  "cart": [
    {
      "name": "Банан", "product_id": "…",
      "slug": "banan-32485",                              // must end in the article number
      "url": "https://silpo.ua/product/banan-32485",      // derived from slug — never model-written
      "image_url": "https://images.silpo.ua/…",           // must start with that host
      "quantity": 1.2, "unit": "кг",
      "price": 48.9,          // per unit, AFTER discounts — cart item `price`
      "old_price": 59.9,      // per unit before discounts — cart item `oldPrice`, or 0
      "total_price": 58.68,   // whole line — cart item `total`
      "discount_uah": 13.2    // cart item `subDiscount`
    }
  ],
  "summary": {
    "total_uah": 1487.3,           // calculation.totalAfterDiscounts — what the user actually pays
    "products_total_uah": 1387.3,  // calculation.productsTotal
    "delivery_uah": 100.0,         // calculation.delivery.total
    "discount_uah": 212.4,         // calculation.subDiscount
    "budget_uah": 1600.0,
    "remaining_uah": 112.7,
    "restrictions": ["кето", "арахіс"],
    "promotions": ["…"],
    "notes": "…"                   // assumptions, adaptation vs last week, trade-offs
  }
}
```

**Validation that will reject a plan** (each raises back to the model as a tool
error, costing one retry rather than the run):

* fewer or more than 7 days, or a missing weekday — `missing days: …`
* `total_price` off `price × quantity` by more than `max(0.05, 2%)`
* a `slug` that does not match `^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$`
* an `image_url` not starting with `https://images.silpo.ua/`

All human-readable text in the plan is Ukrainian. Nulls the model sends are
dropped before validation (`_Model._drop_nulls`), so optional fields default to
`""`, `0` or `[]` rather than `null` — the frontend never has to null-check.

## Tools available to the model

### Silpo MCP (`tool_bridge.ALLOWED_TOOLS`, ~23 of ~45 exposed)

* **identity / restrictions** — `silpo_get_my_profile`,
  `silpo_get_my_food_restrictions`, `silpo_get_my_favorites`,
  `silpo_get_my_premium_subscription`
* **finding products** — `silpo_find_products_batch`, `silpo_get_products`,
  `silpo_get_product_details`, `silpo_get_categories_tree`,
  `silpo_get_similar_products`, `silpo_get_replacements`
* **discounts** — `silpo_get_promotions`, `silpo_get_my_promos`,
  `silpo_get_my_coupons`
* **cart context** — `silpo_get_my_shopping_cart`, `silpo_get_shopping_cart_by_id`,
  `silpo_get_available_delivery_types`, `silpo_get_time_slots`
* **cart writes (gated by `apply`)** — `silpo_add_or_update_cart_products`,
  `silpo_remove_cart_products`, `silpo_clear_shopping_cart`
* **history** — `silpo_get_my_online_orders`, `silpo_get_my_offline_orders`,
  `silpo_list_branches`

`sanitize_schema` strips JSON Schema keywords the Gemini API rejects before the
tool is declared. `select_tools` logs any allowlisted name the server no longer
exposes — watch for that warning after a Silpo MCP update.

### Local deterministic tools (`local_tools.py`)

| Tool | Purpose |
|---|---|
| `calc_targets` | Mifflin-St Jeor BMR → activity factor → goal shift. Uses `weekly_pace_kg` when given (`KCAL_PER_KG = 7700`), capped at `MIN_TDEE_FACTOR` (0.7 × TDEE) or BMR, whichever is higher; returns the pace that cap actually buys plus a `hint`. Also returns `estimated_weeks_to_goal` / `estimated_goal_date` |
| `check_nutrition` | weekly kcal/macro coverage of the chosen products; on target between 0.85 and 1.15 |
| `check_budget` | estimate from **search** prices, per-line totals, the 5 most expensive positions; an upper bound only |
| `sum_macros` | totals and per-day averages over meals or days |
| `check_plan_days` | each day's kcal and protein against the daily target, ±10% by default |

The `hint` fields exist to name *input* mistakes rather than send the model back to
rewrite a correct ration: a value 5–9× the daily target is the week's totals pasted
into one row, > 2× is several days summed, < 0.4× is a single meal sent as a day.

### `finalize_plan`

The terminal tool. Its parameter schema is generated from `plan_schema.Plan`, so the
ask and the response can never drift. The agent intercepts the call, validates, and
ends the run — nothing is written to disk here, the plan goes back to the caller.
