import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

from . import prompts
from .config import Settings
from .logs import preview
from .request_schema import PlanRequest

log = logging.getLogger(__name__)

MEALS = ("breakfast", "lunch", "snack", "dinner")

STORE_CONTEXT_TOOLS = ("silpo_get_shopping_cart_by_id",)
PRODUCT_TOOLS = ("silpo_find_products_batch", "silpo_get_products", "silpo_get_product_details")
CART_WRITE_TOOLS = ("silpo_add_or_update_cart_products",)

RATE_LIMIT_RETRIES = 2

MAX_ISSUES = 10

DAY_KCAL_TOLERANCE = 0.12
DAY_PROTEIN_FLOOR = 0.85
MEAL_SUM_TOLERANCE = 0.05
MONEY_TOLERANCE_UAH = 2.0
MONEY_TOLERANCE_RATIO = 0.03


@dataclass
class Issue:
    where: str
    problem: str
    fix: str
    source: str = "audit"

    def as_dict(self) -> dict[str, str]:
        return {"where": self.where, "problem": self.problem, "fix": self.fix, "source": self.source}


@dataclass
class PlanContext:
    budget_uah: float = 0.0
    delivery_included: bool = True
    goal: str = "maintain"
    diet_type: str = "none"
    allergens: list[str] = field(default_factory=list)
    excluded_products: list[str] = field(default_factory=list)
    fridge_items: list[str] = field(default_factory=list)
    workout_schedule: dict[str, str] = field(default_factory=dict)
    workouts_per_week: int = 0

    @property
    def has_restrictions(self) -> bool:
        return self.diet_type != "none" or bool(self.allergens) or bool(self.excluded_products)

    @classmethod
    def from_request(cls, body: PlanRequest) -> "PlanContext":
        return cls(
            budget_uah=body.budget_uah,
            delivery_included=body.delivery_included,
            goal=body.goal
            or prompts.goal_from_weights(body.profile.weight_kg, body.profile.target_weight_kg),
            diet_type=body.diet_type,
            allergens=list(body.allergens),
            excluded_products=list(body.excluded_products),
            fridge_items=list(body.fridge_items),
            workout_schedule=dict(body.workout_schedule),
            workouts_per_week=body.workouts_per_week,
        )


class ReviewIssue(BaseModel):
    where: str = Field(
        description="Місце в плані: days.monday.lunch, cart, summary.restrictions тощо"
    )
    problem: str = Field(description="Що саме порушено")
    fix: str = Field(description="Що зробити, щоб це виправити")


class Review(BaseModel):
    ok: bool = Field(description="true, якщо план можна віддавати користувачеві")
    issues: list[ReviewIssue] = Field(
        default_factory=list, description="Порушення, які треба виправити; порожньо, якщо ok"
    )


def check_grounding(succeeded: set[str], plan: dict[str, Any], apply: bool) -> list[Issue]:
    issues: list[Issue] = []
    cart = plan.get("cart") or []

    if not any(tool in succeeded for tool in STORE_CONTEXT_TOOLS):
        issues.append(
            Issue(
                "plan",
                "план зібраний без контексту магазину: жоден виклик "
                "silpo_get_shopping_cart_by_id за цей ран не пройшов успішно",
                "виконай крок 1: silpo_get_my_shopping_cart, потім "
                "silpo_get_shopping_cart_by_id, і візьми звідти branchId, deliveryType і "
                "таймслот. Без них пошук товарів повертає нуль результатів",
            )
        )

    if cart and not any(tool in succeeded for tool in PRODUCT_TOOLS):
        issues.append(
            Issue(
                "cart",
                "жодного пошуку товарів за цей ран не було, тож товари в кошику взяті не з "
                "«Сільпо» — найімовірніше переписані з минулого плану",
                "знайди кожен товар заново через silpo_find_products_batch і візьми ціну, "
                "slug та image_url з відповіді. Минулий план — це підказка про смаки, а не "
                "джерело товарів і цін",
            )
        )

    if apply and cart and not any(tool in succeeded for tool in CART_WRITE_TOOLS):
        issues.append(
            Issue(
                "cart",
                "кошик користувача не заповнений: жоден silpo_add_or_update_cart_products "
                "не пройшов успішно, а ран іде в режимі реального замовлення",
                "додай товари через silpo_add_or_update_cart_products, перечитай кошик через "
                "silpo_get_shopping_cart_by_id і візьми фінальні числа звідти",
            )
        )
    return issues


def audit(plan: dict[str, Any], context: PlanContext) -> list[Issue]:
    issues: list[Issue] = []
    targets = plan.get("targets") or {}
    summary = plan.get("summary") or {}
    cart = plan.get("cart") or []
    days = plan.get("days") or []

    _audit_targets(issues, targets, context)
    _audit_cart(issues, cart)
    _audit_money(issues, summary, cart, context)
    _audit_days(issues, days, targets, context)
    _audit_summary(issues, summary, context)
    return issues


def _audit_targets(issues: list[Issue], targets: dict[str, Any], context: PlanContext) -> None:
    if _num(targets.get("kcal")) <= 0 or _num(targets.get("protein_g")) <= 0:
        issues.append(
            Issue(
                "targets",
                "денна норма ккал або білка порожня",
                "візьми daily_kcal, daily_protein_g, daily_fat_g і daily_carbs_g з calc_targets",
            )
        )
    if context.goal in ("lose", "gain"):
        if _num(targets.get("estimated_weeks_to_goal")) <= 0:
            issues.append(
                Issue(
                    "targets.estimated_weeks_to_goal",
                    "строк досягнення цілі не заповнений, хоча ціль — зміна ваги",
                    "постав estimated_weeks_to_goal і estimated_goal_date з відповіді calc_targets",
                )
            )
        elif not str(targets.get("estimated_goal_date") or "").strip():
            issues.append(
                Issue(
                    "targets.estimated_goal_date",
                    "дата досягнення цілі порожня",
                    "постав estimated_goal_date з відповіді calc_targets",
                )
            )


def _audit_cart(issues: list[Issue], cart: list[dict[str, Any]]) -> None:
    if not cart:
        issues.append(
            Issue(
                "cart",
                "кошик порожній — раціон нема з чого готувати",
                "знайди реальні товари через silpo_find_products_batch і склади з них кошик",
            )
        )
        return

    seen: dict[str, str] = {}
    for item in cart:
        slug = str(item.get("slug") or "")
        name = str(item.get("name") or "?")
        if slug and slug in seen:
            issues.append(
                Issue(
                    "cart",
                    f"товар «{name}» ({slug}) стоїть у кошику двічі",
                    "залиш одну позицію і склади в неї сумарну кількість",
                )
            )
        seen[slug] = name
        if _num(item.get("quantity")) <= 0:
            issues.append(
                Issue(
                    "cart",
                    f"у позиції «{name}» кількість 0",
                    "постав реальну кількість або прибери позицію з кошика",
                )
            )


def _audit_money(
    issues: list[Issue],
    summary: dict[str, Any],
    cart: list[dict[str, Any]],
    context: PlanContext,
) -> None:
    total = _num(summary.get("total_uah"))
    products = _num(summary.get("products_total_uah"))
    delivery = _num(summary.get("delivery_uah"))
    budget = _num(summary.get("budget_uah"))
    remaining = _num(summary.get("remaining_uah"))
    lines = round(sum(_num(item.get("total_price")) for item in cart), 2)

    if context.budget_uah and abs(budget - context.budget_uah) > 0.01:
        issues.append(
            Issue(
                "summary.budget_uah",
                f"у плані бюджет {budget} грн, а в запиті — {context.budget_uah} грн",
                f"постав summary.budget_uah = {context.budget_uah}",
            )
        )

    if cart and products <= 0:
        issues.append(
            Issue(
                "summary.products_total_uah",
                "сума товарів не заповнена, хоча кошик не порожній",
                "візьми calculation.productsTotal з silpo_get_shopping_cart_by_id",
            )
        )
    elif cart and not _close(lines, products):
        issues.append(
            Issue(
                "summary.products_total_uah",
                f"сума позицій кошика {lines} грн не збігається із summary.products_total_uah "
                f"{products} грн",
                "числа в cart і в summary мають бути з одного й того самого перечитаного кошика: "
                "total позиції в total_price, calculation.productsTotal у products_total_uah",
            )
        )

    if total and not _close(total, products + delivery):
        issues.append(
            Issue(
                "summary.total_uah",
                f"сума до сплати {total} грн не дорівнює товарам {products} грн плюс доставка "
                f"{delivery} грн",
                "усі три числа бери дослівно з calculation кошика: totalAfterDiscounts, "
                "productsTotal і delivery.total",
            )
        )

    limit = context.budget_uah or budget
    if not limit:
        return

    spent = total if context.delivery_included else products
    if spent > limit + 0.01:
        issues.append(
            Issue(
                "summary.total_uah",
                f"план коштує {spent} грн при бюджеті {limit} грн — перевищення на "
                f"{round(spent - limit, 2)} грн",
                "заміни найдорожчі позиції на дешевші аналоги через silpo_get_replacements або "
                "silpo_get_similar_products чи зменш кількості, білок ріж останнім, потім "
                "перечитай кошик і онови всі числа",
            )
        )

    expected = round(limit - spent, 2)
    if abs(remaining - expected) > 1.0:
        issues.append(
            Issue(
                "summary.remaining_uah",
                f"залишок бюджету {remaining} грн, а має бути {expected} грн",
                f"remaining_uah = {limit} мінус "
                + ("total_uah (доставка входить у бюджет)" if context.delivery_included
                   else "products_total_uah (доставка поза бюджетом)"),
            )
        )


def _audit_days(
    issues: list[Issue],
    days: list[dict[str, Any]],
    targets: dict[str, Any],
    context: PlanContext,
) -> None:
    target_kcal = _num(targets.get("kcal"))
    target_protein = _num(targets.get("protein_g"))
    scheduled = set(context.workout_schedule)
    workout_days = 0

    for day in days:
        key = str(day.get("day") or "?")
        meals = [day.get(meal) or {} for meal in MEALS]

        for name, meal in zip(MEALS, meals):
            if not (meal.get("items") or []):
                issues.append(
                    Issue(
                        f"days.{key}.{name}",
                        "у страви порожній перелік складників",
                        "перелічи складники з вагою, напр. «вівсянка 60 г»",
                    )
                )

        day_kcal = _num(day.get("kcal"))
        meals_kcal = sum(_num(meal.get("kcal")) for meal in meals)
        if meals_kcal and abs(day_kcal - meals_kcal) > max(50.0, meals_kcal * MEAL_SUM_TOLERANCE):
            issues.append(
                Issue(
                    f"days.{key}",
                    f"денний підсумок {round(day_kcal)} ккал не дорівнює сумі чотирьох прийомів "
                    f"їжі ({round(meals_kcal)} ккал)",
                    "порахуй денні підсумки через sum_macros на чотирьох прийомах їжі цього дня "
                    "і постав саме їх",
                )
            )

        day_protein = _num(day.get("protein_g"))
        meals_protein = sum(_num(meal.get("protein_g")) for meal in meals)
        if meals_protein and abs(day_protein - meals_protein) > max(10.0, meals_protein * 0.1):
            issues.append(
                Issue(
                    f"days.{key}",
                    f"білок за день {round(day_protein)} г не дорівнює сумі прийомів їжі "
                    f"({round(meals_protein)} г)",
                    "порахуй денні підсумки через sum_macros і постав саме їх",
                )
            )

        if target_kcal and abs(day_kcal / target_kcal - 1) > DAY_KCAL_TOLERANCE:
            issues.append(
                Issue(
                    f"days.{key}",
                    f"{round(day_kcal)} ккал проти денної норми {round(target_kcal)} ккал",
                    "приведи раціон цього дня до норми — зміни порції, а не саму норму",
                )
            )
        if target_protein and day_protein < target_protein * DAY_PROTEIN_FLOOR:
            issues.append(
                Issue(
                    f"days.{key}",
                    f"білка {round(day_protein)} г при нормі {round(target_protein)} г",
                    "додай білкових продуктів у цей день, не збільшуючи ккал понад норму",
                )
            )

        is_workout = bool(day.get("workout"))
        workout_days += is_workout
        if scheduled and is_workout != (key in scheduled):
            issues.append(
                Issue(
                    f"days.{key}.workout",
                    f"workout: {str(is_workout).lower()}, а за розкладом тренувань цей день "
                    + ("без тренування" if is_workout else "тренувальний"),
                    "постав workout рівно за розкладом тренувань із запиту і підбий під це "
                    "вуглеводи та білок",
                )
            )

    if not scheduled and context.workouts_per_week and workout_days != context.workouts_per_week:
        issues.append(
            Issue(
                "days",
                f"тренувальних днів у плані {workout_days}, а в запиті {context.workouts_per_week}",
                f"постав workout: true рівно {context.workouts_per_week} дням, рівномірно по тижню",
            )
        )


def _audit_summary(issues: list[Issue], summary: dict[str, Any], context: PlanContext) -> None:
    if context.has_restrictions and not (summary.get("restrictions") or []):
        issues.append(
            Issue(
                "summary.restrictions",
                "у запиті є харчові обмеження, а в плані перелік обмежень порожній",
                "перелічи в summary.restrictions обмеження з запиту разом з тими, що повернув "
                "silpo_get_my_food_restrictions",
            )
        )


def format_issues(issues: list[Issue]) -> str:
    lines = [
        "finalize_plan відхилено рецензією плану. Виправ саме ці зауваження і виклич "
        "finalize_plan ще раз:"
    ]
    lines += [
        f"{n}. [{issue.where}] {issue.problem}. Що зробити: {issue.fix}"
        for n, issue in enumerate(issues, 1)
    ]
    lines.append(
        "Не переробляй того, чого немає в переліку. Фінальні гроші й далі бери дослівно з "
        "перечитаного кошика."
    )
    return "\n".join(lines)


class PlanValidator:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def check(
        self, plan: dict[str, Any], context: PlanContext, user_input: str
    ) -> list[Issue]:
        issues = audit(plan, context)
        if issues:
            log.info(
                "plan audit: %d issue(s) — %s",
                len(issues),
                "; ".join(issue.where for issue in issues[:MAX_ISSUES]),
            )
            return issues[:MAX_ISSUES]
        return (await self._review(plan, context, user_input))[:MAX_ISSUES]

    async def _review(
        self, plan: dict[str, Any], context: PlanContext, user_input: str
    ) -> list[Issue]:
        started = time.monotonic()
        config = types.GenerateContentConfig(
            system_instruction=prompts.REVIEW_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=Review,
            thinking_config=types.ThinkingConfig(
                thinking_level=self._settings.review_thinking_level
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = _review_prompt(plan, context, user_input)

        response = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._settings.review_model, contents=contents, config=config
                )
                break
            except errors.ClientError as exc:
                if exc.code != 429 or attempt == RATE_LIMIT_RETRIES:
                    log.exception("plan review call failed — accepting the plan without a review")
                    return []
                delay = 2**attempt
                log.warning(
                    "plan review rate limited, retrying in %.0fs (%d/%d)",
                    delay,
                    attempt + 1,
                    RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(delay)
            except Exception:
                log.exception("plan review call failed — accepting the plan without a review")
                return []
        if response is None:
            return []

        review = _parse_review(response)
        if review is None:
            return []
        issues = [
            Issue(
                where=(reported.where or "plan").strip(),
                problem=reported.problem.strip(),
                fix=reported.fix.strip(),
                source="review",
            )
            for reported in review.issues
            if reported.problem.strip()
        ]
        log.info(
            "plan review in %.1fs: ok=%s, %d issue(s) — %s",
            time.monotonic() - started,
            review.ok,
            len(issues),
            "; ".join(issue.where for issue in issues) or "none",
        )
        return issues


def _review_prompt(plan: dict[str, Any], context: PlanContext, user_input: str) -> str:
    return "\n".join(
        [
            "ЗАПИТ КОРИСТУВАЧА:",
            user_input,
            "",
            "ПЛАН НА ПЕРЕВІРКУ (JSON):",
            json.dumps(plan, ensure_ascii=False),
            "",
            "Перевір план за своїми правилами і поверни рецензію.",
        ]
    )


def _parse_review(response: Any) -> Review | None:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, Review):
        return parsed
    text = getattr(response, "text", None)
    if not text:
        log.warning("plan review came back empty — accepting the plan without it")
        return None
    try:
        return Review.model_validate_json(text)
    except Exception:
        log.warning("plan review is not valid JSON, ignoring it: %s", preview(text, 500))
        return None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _close(left: float, right: float) -> bool:
    tolerance = max(MONEY_TOLERANCE_UAH, max(abs(left), abs(right)) * MONEY_TOLERANCE_RATIO)
    return abs(left - right) <= tolerance
