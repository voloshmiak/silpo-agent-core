---
name: silpofit-context
description: >
  Explains what SilpoFit is and how silpo-agent-core fits into it: the stateless
  Gemini + Silpo-MCP agent service that turns a user's body goal, workout schedule,
  dietary restrictions and weekly budget into a 7-day ration plus a real «Сільпо»
  cart, called over HTTP by the SilpoFit backend, which is in turn called by the
  frontend. Read it before changing anything in silpofit/ — the agent loop, prompts,
  plan_schema, request_schema, local_tools, tool_bridge, mcp_client or server — and
  before answering "what does this repo do", "how does the backend call this",
  "what does the frontend show", "why was the plan rejected", "where do prices come
  from", "which MCP tools does the agent get", or when wiring a new profile field,
  a new plan field or a new /plan endpoint. Also covers the codebase-memory MCP
  index for this project.
---

# SilpoFit — repository context

## What the product is

SilpoFit is an AI personal-nutrition agent. It turns a user's physical goal,
activity level and budget into an **adaptive weekly ration and a ready-to-order
«Сільпо» cart** (via the official Silpo MCP server).

It is deliberately *not* another passive calorie tracker. The differentiators,
in priority order:

1. **Adapts to the training week** — workout days get more carbs and protein,
   rest days fewer; a skipped session triggers a recalculation.
2. **Hard exclusions** — allergens and stop-products are never bought, and the
   check is against the composition, not just the product name.
3. **Spends against real prices** — the plan is optimized around live promotions,
   personal offers and coupons, inside a weekly UAH budget.
4. **Learns from real purchase history** — what was actually bought, what stayed
   in the fridge, and end-of-week feedback on the dishes feed the next plan.

All user-facing text in the product is **Ukrainian**. Code and comments in this
repo are English; the system instruction, the plan's human-readable fields and
the request aliases are Ukrainian.

## Three repositories, one product

```
frontend  ──HTTP──▶  backend  ──HTTP──▶  silpo-agent-core   (this repo)
  (UI)               (auth, DB,          (stateless agent) ──MCP──▶ mcp.silpo.ua
                      history)                             ──API──▶ Gemini
```

* **frontend** — the pages described below. Talks only to the backend.
* **backend** — owns the Silpo OAuth flow, the user profile, the plan archive,
  purchase analytics, weight history and the feedback loop. It persists
  everything, then calls this service per plan request.
* **silpo-agent-core (here)** — a stateless agent service. It holds **nothing**
  between requests: no tokens, no profiles, no plans. Everything it needs
  arrives in one POST body; everything it produces comes back in the response
  for the backend to store.

**The single most important invariant of this repo: do not add persistence,
sessions, user lookup or an OAuth flow here.** If a feature needs memory, it
belongs in the backend, and it reaches the agent as a request field.

## What the frontend shows, and where each piece comes from

The agent only ever sees what the backend folds into `PlanRequest`. Check this
table before promising that a UI feature is "supported by the agent".

| Frontend page | Data | Owner |
|---|---|---|
| **Профіль** — weight, target weight, height, age, sex, goal (схуднення / набір маси / баланс) | `profile.*`, `goal`, `weekly_pace_kg` | agent consumes |
| **Спортивний режим** — workouts per week, per-day type (кардіо/силові), quick "skipped a day" toggle | `workouts_per_week`, `workout_schedule` | backend stores; a skipped day is just a re-POST with a changed schedule — the agent has no notion of "today" |
| **Преференції та обмеження** — allergens, stop-products, diet type (веган, кето…) | `allergens`, `excluded_products`, `diet_type` | agent enforces as a hard rule, merged with `silpo_get_my_food_restrictions` |
| **Бюджет** — weekly UAH limit | `budget_uah`, `delivery_included`, `promo_priority` | agent optimizes against it |
| **Тижневий дашборд** — 7 interactive days, workout vs rest days | `Plan.days[]` (`workout: true/false`, 4 meals each) | agent produces |
| **Готовий кошик «Сільпо»** — real products, prices, discounts, one-click order | `Plan.cart[]` + `Plan.summary`; `apply=true` writes the real Silpo cart | agent produces / writes |
| **Швидка корекція** — "I already have these", fridge photo, meal-completion status | only `fridge_items` (a list of strings) and free-text `note` reach the agent | **backend/frontend own this** — photo recognition and per-meal completion do not exist in this repo |
| **Історія та архів планів** — past weeks, dishes cooked, products ordered | `previous_plan` (opaque JSON) is the only way history enters a run | **backend owns the archive** |
| **Аналітика закупівель, динаміка ваги** — bought vs planned, fridge leftovers, weight curve | not in this repo at all | **backend owns it**; it may summarize findings into `note` / `previous_plan` |
| **Фідбек-петля** — end-of-week dish ratings («занадто складно готувати», «не сподобався продукт») | reaches the agent only as text in `note`, or folded into `previous_plan` | **backend owns collection and storage**; the agent adapts because the prompt tells it to weigh `previous_plan` |

So: the feedback loop, the archive and the analytics are *product* features
implemented in the backend. This repo's contribution to them is exactly two
fields — `previous_plan` and `note` — plus the fact that every run is
reproducible from its request.

## Layout of this repo

```
silpofit/
  server.py          FastAPI app: /health, POST /plan, POST /plan/stream (SSE)
  config.py          Settings.load() — env + defaults (model, thinking level, MAX_STEPS)
  request_schema.py  PlanRequest/Profile — the input contract + Ukrainian alias normalization
  prompts.py         SYSTEM_INSTRUCTION (the 12-step pipeline) + build_plan_prompt()
  agent.py           the loop: Gemini decides → MCP/local tools act → finalize_plan ends the run
  mcp_client.py      SilpoMCP — one authenticated MCP session per request
  tool_bridge.py     MCP tool allowlist → Gemini function declarations; MUTATING_TOOLS gate
  local_tools.py     deterministic math the model must not do itself (calc_targets, checks)
  finalize.py        the terminal finalize_plan tool: validates and captures the plan
  plan_schema.py     the output contract — one Pydantic model used three ways
  logs.py            run-id-stamped logging
Dockerfile           uv build → python:3.14-slim; CMD must read $PORT at runtime
.github/workflows/deploy.yml   push to main → Artifact Registry → Cloud Run (europe-central2)
```

Python 3.14, `uv` for dependencies (`uv.lock` is committed). There is **no test
suite** — verify changes by running the service and issuing a real `/plan`.

Full field-by-field request and response contract: `references/api-contract.md` —
read it before adding, renaming or removing any field on either side.

## The run, end to end

1. Backend POSTs `/plan` with a service bearer token, and the user's Silpo access
   token in the body.
2. `SilpoMCP` opens one MCP session with that token; `agent.prepare()` fetches the
   Silpo tool list and builds the Gemini tool set: allowlisted MCP tools + 5 local
   tools + `finalize_plan`.
3. `run_stream()` loops up to `MAX_STEPS` (60): one model turn → dispatch every
   function call → feed the results back.
4. The run ends the moment `finalize_plan` validates. **There is no final prose
   turn** — the plan is the entire answer. A turn that comes back with no tool
   calls is a *failed* run, not an answer.

`run_stream` is the source of truth; `run()` just drains it, and `/plan/stream`
re-emits its events as SSE (`tool_call`, `tool_result`, then a terminal `plan` or
`error`).

The model's 12-step pipeline lives in `prompts.SYSTEM_INSTRUCTION`: store context →
restrictions → targets → ration → subtract fridge items → find products → nutrition
check → promotions → budget check → per-day check → fill cart and re-read prices →
finalize.

## Non-obvious rules (the ones that break things)

**Two independent credentials.** The `Authorization: Bearer` header authenticates
the *calling backend* (`SILPOFIT_SERVICE_TOKENS`, comma-separated). The *end user's*
Silpo token is a body field, `silpo_access_token`. Never conflate them. A rejected
Silpo token surfaces as **409 `silpo_token_expired`** (checked explicitly in
`mcp_client._check_token`, because the MCP SDK otherwise buries the 401 in a generic
`-32603`); a bad service token is 401; an agent failure is 502.

**`apply` gates real cart writes.** With `apply=false` (the default) every tool in
`MUTATING_TOOLS` returns `DRY_RUN_REFUSAL` and `DRY_RUN_NOTICE` is appended to the
system instruction. The planned products still land in `Plan.cart` — only the real
Silpo cart is left untouched.

**Search prices are not the bill.** Product search returns shelf prices; personal
and loyalty discounts materialize only when Silpo calculates the cart. So
`check_budget` is an upper-bound *estimate used while choosing*, and every number in
`Plan.cart` / `Plan.summary` must be copied verbatim out of
`silpo_get_shopping_cart_by_id` after the cart is filled. Never compute final money
from search results. The real total lands noticeably below the estimate — that is
expected, and is not a reason to re-shop the budget.

**Every product search needs store context.** `branchId`, `deliveryType` and a valid
timeslot come from `silpo_get_my_shopping_cart` → `silpo_get_shopping_cart_by_id`.
Without them product search silently returns **zero results** — which reads like
"out of stock" and is the most common cause of a plan built on nothing.

**The model may not invent products.** `CartItem` validates `slug` against
`PRODUCT_SLUG` (must end in the article number, e.g. `banan-32485`) and `image_url`
against `IMAGE_HOST`. The product page URL is *derived* from the slug, never written
by the model. `total_price` is cross-checked against `price × quantity` to catch a
unit price used as a line total.

**No nullable fields in `plan_schema`.** Gemini handles `anyOf` with `type: "null"`
poorly, so every optional field gets an empty default and `_Model._drop_nulls`
discards nulls the model sends anyway. Do not add `X | None` to a plan model.

**Gemini parts must round-trip unchanged.** `_generate` appends the raw `Part`
objects from the API to the history, never rebuilt ones — Gemini 3 attaches a
`thought_signature` to function-call parts and rejects the next request if it is
missing.

**The tool allowlist is deliberate.** Silpo MCP exposes ~45 tools;
`tool_bridge.ALLOWED_TOOLS` hands the model ~23. More tools dilute tool choice and
burn context — extend it only when the pipeline actually needs the tool, and add any
cart-writing tool to `MUTATING_TOOLS` in the same edit.

**Arithmetic is a tool, not a model job.** `calc_targets`, `sum_macros`,
`check_nutrition`, `check_budget` and `check_plan_days` exist so the numbers are
deterministic. Their `hint` fields diagnose *input* mistakes (a week's totals pasted
into one day, per-100g values confused with per-portion) so the model fixes the
argument instead of rewriting a perfectly good ration — that rewrite loop is what
burns the step budget.

**Day keys are `monday`…`sunday`** — stable English identifiers in both schemas.
Ukrainian day names are the frontend's business. `request_schema` accepts «ПН» and
friends and normalizes them.

**Unknown enum labels are rejected loudly.** `request_schema` maps Ukrainian
onboarding labels («КЕТО», «Схуднення») onto identifiers and raises a 422 on anything
unmatched, rather than falling back to a default — a silently dropped allergen is far
more expensive than a wiring-time error.

**Every log line carries a run id**, and every SSE frame carries the same id, so a
frontend error report maps onto exact log lines. Logs go to **stdout** (Cloud Run
marks stderr as errors). `SILPOFIT_LOG_LEVEL=DEBUG` adds the full prompt and every
tool result — httpx stays at WARNING on purpose, since its DEBUG output would leak
the user's Silpo token.

## Configuration

| Env var | Effect |
|---|---|
| `GEMINI_API_KEY` | required; the service refuses to start without it |
| `SILPOFIT_SERVICE_TOKENS` | comma-separated backend tokens; empty ⇒ every `/plan` returns 500 |
| `SILPOFIT_MODEL` | overrides `config.MODEL` (currently `gemini-3.5-flash-lite`) |
| `SILPOFIT_THINKING_LEVEL` | `MINIMAL` < `LOW` < `MEDIUM` < `HIGH`; raise if plans degrade |
| `SILPOFIT_LOG_LEVEL` | default `INFO` |
| `PORT` | injected by Cloud Run; the Dockerfile `CMD` must stay in shell form so it expands |

A run costs 30–60 model turns, so the thinking level is a real latency lever.
`MAX_STEPS=60` only bounds a run that is going nowhere; the real ceiling on a healthy
run is the Cloud Run request timeout (`--timeout=3600` in `deploy.yml`).

Run locally:

```bash
uv run uvicorn silpofit.server:app --reload   # OpenAPI docs at /docs
```

`tools/dev_login.py` obtains a Silpo access token for manual testing.

## codebase-memory MCP

This repo is indexed in the codebase-memory MCP server as project
**`D-GoProjects-silpo-agent-core`**. Prefer it over blind grepping for structural
questions:

* `search_graph` — find a symbol; `trace_path` — callers/callees;
  `get_code_snippet` — exact source; `get_architecture` — orientation.
* `search_code` or `Grep` for literal text, Ukrainian prompt strings, and anything
  the graph does not model.
* `check_index_coverage` before making a negative or exhaustive claim about a path —
  coverage is best-effort, and absence from the graph is never proof of absence in
  the code.
* `tools/` and `.venv/` are **excluded** from the index — grep those directly.
* The index auto-refreshes in the background; run `index_repository` only after a
  large external change, or `index_status` to check health.
