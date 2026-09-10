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
  validator.py       the second agent: deterministic audit + a reviewing model over the plan
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
4. `finalize_plan` hands the plan to the **validator** (below). A rejected plan
   comes back to the planning model as a `finalize_plan` error listing what to
   fix, and the loop continues.
5. The run ends the moment `finalize_plan` validates *and* the validator accepts.
   **There is no final prose turn** — the plan is the entire answer. A turn with no
   tool calls is never an answer, but it is not fatal either: it is retried up to
   `MAX_EMPTY_TURNS` (3) before the run gives up.

`run_stream` is the source of truth; `run()` just drains it, and `/plan/stream`
re-emits its events as SSE (`tool_call`, `tool_result`, then a terminal `plan` or
`error`).

The model's 12-step pipeline lives in `prompts.SYSTEM_INSTRUCTION`: store context →
restrictions → targets → ration → subtract fridge items → find products → nutrition
check → promotions → budget check → per-day check → fill cart and re-read prices →
finalize.

## Non-obvious rules (the ones that break things)

**The plan is reviewed by a second agent, and the review is a tool error.**
`agent._finalize` runs `finalize.validate` (Pydantic), then `validator.check`:
first `audit()` — deterministic checks over money, day totals against `targets`,
workout flags against the request's schedule, empty carts, duplicate slugs — and,
only when the audit is clean, one tool-less Gemini call (`prompts.REVIEW_INSTRUCTION`,
JSON-schema output) for the things arithmetic cannot see: dishes built from
products that are in neither the cart nor the fridge, quantities that do not cover
the week, allergens hiding in a product's composition, re-buying what the user
already has. Issues come back to the planning model as a numbered Ukrainian
`finalize_plan` rejection, which is the same repair path a schema error already
takes — that is why the reviewer needs no tools and no new endpoint.
The split is deliberate: **arithmetic is code, meaning is the model.** Never move
a check that can be computed into the review prompt, and never make the reviewing
model re-add numbers — it will hallucinate a disagreement and the run will loop.

**The review fails open, and it gives up.** A review call that raises is logged
and the plan is accepted — a quality gate must not turn a good run into a 502.
After the round budget is spent the next plan is accepted with its issues
unresolved, logged at ERROR and streamed as `validation` with
`accepted: true, ok: false`. Both are better than a run that never terminates.
Each round costs a model turn from `MAX_STEPS` plus 15–30s of review.

**A plan that no tool call produced is rejected before either check runs.**
`agent._mcp_ok` collects the Silpo tools that came back *successfully*, and
`validator.check_grounding` refuses a `finalize_plan` when the run never got store
context (`silpo_get_shopping_cart_by_id`), never searched products while filling a
cart, or — under `apply=true` — never wrote the cart. This exists because of an
observed run: with a 115k-character `previous_plan` in the prompt, the model called
`finalize_plan` as its **first and only** tool call and copied last week's cart
wholesale. Slugs and image URLs validated (they were real, just stale), every number
was internally consistent, and the reviewer returned `ok: true` — correctly, because
nothing inside the plan was wrong. Neither the audit nor the review can see this
class: they judge the artifact, and the artifact was fine. Only the run's own history
knows the plan was never grounded, so the gate reads that instead.

**Under `apply=true` the plan is checked against the real cart, not against itself.**
`agent._cart_issues` re-reads `silpo_get_shopping_cart_by_id` (the id is captured from
the model's own successful calls) and `validator.check_cart_match` compares line by
line: quantity, line total, per-position presence in both directions, and
`productsTotal` / `totalAfterDiscounts` / `delivery.total` against `summary`. This is
the only check with access to ground truth, and it is the only one that can settle a
disagreement — a plan can be perfectly self-consistent and still describe a different
cart than the user will pay for. The observed case: the model oscillated between
`quantity: 27` and `quantity: 2.7` for a weighted product across four cart writes, and
the reviewing model "diagnosed" it as 27 × 100 g — **wrongly**, as the cart payload
later proved. That is precisely why the reviewer must never be the authority on
numbers: it argued confidently for the wrong answer, and only the cart could say who
was right. Nothing that reads only the plan can catch this class. It fails open on anything unexpected — non-JSON,
an error result, a payload with no `shipments` and no `calculation` — because a parse
mismatch here would reject every plan forever.

**The meals are the truth, the day total is derived — and the audit must say so in one
breath.** `_audit_days` compares the *sum of the four meals* against `targets`, never the
`kcal` the model wrote on the day, and when both are wrong it emits **one** issue naming
the meal sum and asking for the portions to change. The earlier version emitted two
independent issues ("the total does not equal the meals" and "the day is off target")
and a real run ping-ponged between them for seven rounds: the model copied the meal sum
into the total, which broke the target check, then wrote the target into the total,
which broke the sum check, and shipped 2650 kcal against a 2272 target with the rounds
spent. Two individually correct checks can still spell out contradictory instructions —
whenever two checks constrain the same number, one of them must own the fix.

**The training-day spread has a band, and both checkers must agree on it.** The audit
holds every day within ±12% of `targets.kcal`; the reviewer requires workout days to
carry more carbs and protein than rest days. Stated without a magnitude, those two
sent a run in circles: the model pushed workout days to +14%, the audit rejected them,
the model flattened the week, the reviewer demanded differentiation again. Neither
check was wrong — the model simply had no idea a solution existed inside the band. So
`SYSTEM_INSTRUCTION` now gives the arithmetic (≈+10%/−7.5% at three workouts,
≈+4%/−10% at five, always averaging to the norm over the week) and the reviewer's rule
5 owns only the *direction* of the spread, never its size. When two checkers constrain
the same quantity, one of them must state the feasible range, or the model will
oscillate between them until the rounds run out.

**The two kinds of rejection have separate budgets, on purpose.** `audit` issues
get `SILPOFIT_VALIDATION_ROUNDS + AUDIT_EXTRA_ROUNDS` (3 + 2) attempts, `review`
issues only `SILPOFIT_VALIDATION_ROUNDS`; the counters in `agent._rounds_used` are
independent and the `validation` event carries the `source` that was rejected.
This exists because of a real run: two content rounds burned the whole budget, the
model then fixed the content and broke the money, and a one-line arithmetic error
shipped with no attempts left. A deterministic issue is cheap to detect, always
fixable without re-shopping, and must never be starved by the expensive review.

**History reaches the agent as a digest, never as last week's plan.** `previous_feedback`
(`WeekFeedback` in `request_schema`) is the channel the backend should fill: what was
spent, how the weight moved, a per-product verdict (`liked` / `disliked` / `leftover` /
`missing`) and per-dish ratings. `previous_plan` is still accepted but never rendered
raw — `prompts._previous_plan_digest` reduces it to cart names, unique dish titles and
the spend, which works because it is our own `Plan` shape. The raw field pushed prompts
to 168k–241k characters (≈91k–125k tokens **before the first model turn**) and did
active harm: runs kept opening with last week's cart copied verbatim into
`finalize_plan`, which is why `check_grounding` had to exist at all. Both renderings end
on the same sentence — a hint about taste, not a source of products or prices — and both
stay near 700 characters, because their size follows the number of items, not the
verbosity of the archive.

**`note` carries requests, not decoration.** The user's free text is the only channel
for "I like marshmallows" or "no fish on Fridays", and a run shipped ignoring exactly
such a line. `build_plan_prompt` now marks it as instructions to honour, and the
reviewer's rule 8 requires every explicit wish to be either in the plan or explained in
`summary.notes` — silently dropping one is a violation. `calc_targets` likewise hints
when `goal` contradicts the weights (a request to *lose* toward a heavier target), since
that mis-set field silently inverts every calorie number downstream.

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

**An empty model turn is retried, not fatal.** Gemini can return a candidate with
`finish_reason=STOP`, zero parts and zero output tokens — one observed run died at
step 1 this way, on a 96k-token prompt, after 476 thought tokens and nothing else.
Killing a whole run over one flaky turn is the wrong trade, so `run_stream` retries:
a turn with no parts at all is dropped from the history entirely and the request is
simply reissued (nothing to round-trip, so no `thought_signature` to lose), while a
turn that produced prose instead of a call is kept and answered with
`prompts.NO_TOOL_CALL_NUDGE`. The counter resets after any turn that does call a tool,
so it bounds *consecutive* failures, and each retry spends one of `MAX_STEPS`. The
terminal `error` event carries `empty_turns` so the backend can tell a stuck model
from a genuine dead end.

**A weighted product's `quantity` is always in kilograms** — `weighted: true` decides
it, never the label. Silpo returns `displayRatio`/`ratio` as display context only:
«Куряче філе домашнє» comes back as `ratio: "100г"`, `weighted: true`, `quantity: 2.6`,
`price: 308.58`, `total: 802.31` — that is 2.6 **kg** at 308.58/kg, not 260 g. For
`weighted: false` the quantity counts packages and `displayRatio` («500г», «10шт»,
«5*80г») says what one contains. Both the prompt and any future check must use
`weighted`, not the string, or they will be confidently wrong in units of ten. The
reviewing model got this wrong twice — once advising "raise it to 9 units of 100 g",
which the planner faithfully turned into **9 kilograms** of apples for a week that needed
840 g — so `REVIEW_INSTRUCTION` now states the rule and forbids deriving a quantity from
the unit label.

**Product search is capped to a few results per query.** `silpo_find_products_batch`
defaults to **30 matches per search term** and the model never passes `limit`, so a
nine-term batch came back with 270 products — 105,883 characters, +28k tokens on every
later turn, for a pipeline that needs a handful of candidates.
`tool_bridge.normalize_arguments` forces `limit` to 6 (and caps an explicit request at
12) before the call leaves the agent. Field-trimming the payload was considered and
rejected: at ~392 characters per product almost every field is load-bearing —
`id`/`companyId`/`branchId` for cart writes, `slug`/`image` for schema validation,
`weighted`/`step`/`displayRatio` for quantities, `specialPrices` for promotions. The
count was the problem, not the shape.

**`Plan.cart` has a floor of 3 items**, and the floor is declared in the schema, not
only enforced after the fact. A week's ration cannot come from two products, and an
empty or near-empty cart used to validate cleanly and ship as a 200.
Which is why `sanitize_schema` keeps both the snake_case field names of
`types.JSONSchema` **and their camelCase aliases**: Pydantic emits `minItems`,
`maxItems`, `minLength`, and the old filter — built from `model_fields` alone — threw
every one of them away. The API accepts them; dropping them meant Gemini learned
«exactly 7 days» and «at least 3 cart items» only from rejection messages instead of
from the tool schema.

**No nullable fields in `plan_schema`.** Gemini handles `anyOf` with `type: "null"`
poorly, so every optional field gets an empty default and `_Model._drop_nulls`
discards nulls the model sends anyway. Do not add `X | None` to a plan model.

**Gemini parts must round-trip unchanged.** `_generate` appends the raw `Part`
objects from the API to the history, never rebuilt ones — Gemini 3 attaches a
`thought_signature` to function-call parts and rejects the next request if it is
missing.

**The tool allowlist is deliberate.** Silpo MCP exposes ~40 tools;
`tool_bridge.ALLOWED_TOOLS` hands the model 20 — every one of them named in the
pipeline or needed to create a cart. Five that were not (`silpo_get_categories_tree`,
`silpo_get_products`, `silpo_get_my_online_orders`, `silpo_get_my_offline_orders`,
`silpo_get_my_premium_subscription`) were removed after a run where the model wandered
into them mid-plan: `silpo_get_categories_tree` alone returned 62k characters and added
28k tokens to every subsequent turn for nothing, since the products had already been
found. A tool the pipeline never asks for is not a spare capability, it is a detour. More tools dilute tool choice and
burn context — extend it only when the pipeline actually needs the tool, and add any
cart-writing tool to `MUTATING_TOOLS` in the same edit.
`silpo_create_shopping_cart` is the **one deliberate exception**: it is allowlisted
but *not* gated by `apply`. Without it a user whose cart is missing or stale has no
recovery path, the model starts inventing `shoppingCartId`s (all-zero UUIDs,
`a1b2c3d4-…` placeholders), every cart call fails with «Resource not found», and the
plan ends up priced from search results instead of the cart. It is idempotent per
user — with a cart already there it returns that same id and creates nothing — so in
practice it fires only in the case it exists for. Know what it does cost: creating a
cart also pins a delivery address, delivery type and timeslot on the account, which
is more than "an empty cart". No products, no order, nothing charged, and the user can
change all of it in the app; that is the trade this exception makes, and gating the
tool is a one-line change if the call goes the other way.
It needs a whole chain to be callable at all — `silpo_get_my_delivery_addresses`
(coordinates) → `silpo_get_available_delivery_types` (`deliveryType` + `branchId`,
falling back to `silpo_list_branches`) → `silpo_get_time_slots` → create. All four are
allowlisted for exactly this reason; drop one and the model invents coordinates the
same way it used to invent cart ids. The agent has no address of its own — nothing in
`PlanRequest` carries one — so a user with no saved delivery address genuinely cannot
have a cart created, and the prompt tells the model to fail loudly rather than guess.

**A tool result flagged `isError` is a tool error.** `mcp_client.call_tool` returns
`(text, is_error)` and the agent feeds it back as `{"error": …}`. Do not "simplify"
this into returning the text alone: Silpo answers HTTP 200 with an error payload, and
when that reaches the model labelled as a success it retries the same broken call with
a different invented argument instead of changing course.

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

**Every run opens by logging the request it was handed.** `server._request_summary`
prints one INFO line per `/plan` and `/plan/stream` with every field that shapes the
plan — budget, goal, pace, weights, workouts and their days, diet, allergens, excluded
products, fridge, note, and what kind of history arrived — as the values look **after**
`request_schema` normalisation, so the line shows what the agent understood, not what
was posted. It exists to settle "the backend sent X" arguments in one grep instead of
two repos. `silpo_access_token` is never in it, and never should be.

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
| `SILPOFIT_VALIDATION_ROUNDS` | how many times the reviewing model may send a plan back (default 3; deterministic audit issues get 2 more); `0` turns validation off |
| `SILPOFIT_VALIDATOR_MODEL` | model for the review call; defaults to `SILPOFIT_MODEL` |
| `SILPOFIT_VALIDATOR_THINKING_LEVEL` | thinking level for the review call; defaults to `SILPOFIT_THINKING_LEVEL` |
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
