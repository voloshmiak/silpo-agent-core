from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Sex = Literal["male", "female"]
Goal = Literal["lose", "maintain", "gain"]
DietType = Literal["none", "vegetarian", "vegan", "keto", "paleo", "low_fodmap"]
PromoPriority = Literal["low", "medium", "high"]
Verdict = Literal["liked", "disliked", "leftover", "missing"]

SEX_ALIASES: dict[str, str] = {
    "male": "male",
    "m": "male",
    "чоловіча": "male",
    "чоловік": "male",
    "чол": "male",
    "чол.": "male",
    "ч": "male",
    "female": "female",
    "f": "female",
    "жіноча": "female",
    "жінка": "female",
    "жін": "female",
    "жін.": "female",
    "ж": "female",
}

GOAL_ALIASES: dict[str, str] = {
    "lose": "lose",
    "схуднення": "lose",
    "схуднути": "lose",
    "втрата ваги": "lose",
    "зниження ваги": "lose",
    "maintain": "maintain",
    "підтримка": "maintain",
    "підтримка ваги": "maintain",
    "підтримання ваги": "maintain",
    "утримання ваги": "maintain",
    "gain": "gain",
    "набір": "gain",
    "набір ваги": "gain",
    "набір маси": "gain",
    "набір м'язової маси": "gain",
    "набір мʼязової маси": "gain",
}

DIET_ALIASES: dict[str, str] = {
    "none": "none",
    "без обмежень": "none",
    "немає": "none",
    "звичайна": "none",
    "звичайне харчування": "none",
    "vegetarian": "vegetarian",
    "вегетаріанська": "vegetarian",
    "вегетаріанство": "vegetarian",
    "vegan": "vegan",
    "веганська": "vegan",
    "веганство": "vegan",
    "keto": "keto",
    "кето": "keto",
    "кетогенна": "keto",
    "paleo": "paleo",
    "палео": "paleo",
    "палеодієта": "paleo",
    "low_fodmap": "low_fodmap",
    "low fodmap": "low_fodmap",
    "lowfodmap": "low_fodmap",
    "низький fodmap": "low_fodmap",
    "низькофодмап": "low_fodmap",
}

PROMO_ALIASES: dict[str, str] = {
    "low": "low",
    "низький": "low",
    "мінімум": "low",
    "мінімальний": "low",
    "ігнорувати": "low",
    "medium": "medium",
    "середній": "medium",
    "помірний": "medium",
    "звичайний": "medium",
    "high": "high",
    "високий": "high",
    "максимум": "high",
    "максимальний": "high",
    "тільки акції": "high",
}

DAY_ALIASES: dict[str, str] = {
    alias: day
    for day, aliases in {
        "monday": ("monday", "mon", "пн", "пон", "понеділок"),
        "tuesday": ("tuesday", "tue", "вт", "вів", "вівторок"),
        "wednesday": ("wednesday", "wed", "ср", "сер", "середа"),
        "thursday": ("thursday", "thu", "чт", "чет", "четвер"),
        "friday": ("friday", "fri", "пт", "пʼт", "пятниця", "п'ятниця", "пʼятниця"),
        "saturday": ("saturday", "sat", "сб", "суб", "субота"),
        "sunday": ("sunday", "sun", "нд", "нед", "неділя"),
    }.items()
    for alias in aliases
}

VERDICT_ALIASES: dict[str, str] = {
    "liked": "liked",
    "сподобалось": "liked",
    "сподобався": "liked",
    "смакувало": "liked",
    "подобається": "liked",
    "disliked": "disliked",
    "не сподобалось": "disliked",
    "не сподобався": "disliked",
    "не смакувало": "disliked",
    "leftover": "leftover",
    "залишилось": "leftover",
    "лишилось": "leftover",
    "не зʼїли": "leftover",
    "не з'їли": "leftover",
    "missing": "missing",
    "не вистачило": "missing",
    "забракло": "missing",
    "мало": "missing",
}

MAX_WEEKLY_PACE_KG = 2.0


def _key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _normalize(value: Any, aliases: dict[str, str], field: str) -> Any:
    if not isinstance(value, str):
        return value
    key = _key(value)
    if not key:
        return None
    normalized = aliases.get(key)
    if normalized is None:
        allowed = ", ".join(sorted(set(aliases.values())))
        raise ValueError(f"unknown {field} {value!r}; expected one of: {allowed}")
    return normalized


class Profile(BaseModel):
    weight_kg: float = Field(gt=0, description="Current weight, kg")
    target_weight_kg: float = Field(gt=0, description="Goal weight, kg")
    height_cm: float | None = None
    age: int | None = None
    sex: Sex | None = None

    @field_validator("sex", mode="before")
    @classmethod
    def _normalize_sex(cls, value: Any) -> Any:
        return _normalize(value, SEX_ALIASES, "sex")


class ProductFeedback(BaseModel):
    name: str = Field(description="Назва товару, як у «Сільпо»")
    slug: str = Field("", description="Slug товару, якщо бекенд його зберіг")
    verdict: Verdict = Field(
        "liked",
        description=(
            "liked — брати знову; disliked — не брати; leftover — лишилось, брати менше; "
            "missing — не вистачило, брати більше. Приймає «сподобалось», «залишилось»…"
        ),
    )
    note: str = ""

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value: Any) -> Any:
        return _normalize(value, VERDICT_ALIASES, "verdict") or "liked"


class DishFeedback(BaseModel):
    title: str = Field(description="Назва страви з минулого плану")
    rating: int = Field(0, ge=0, le=5, description="Оцінка 1-5; 0 — не оцінювали")
    note: str = Field("", description="«занадто складно готувати», «набридло»…")


class WeekFeedback(BaseModel):
    spent_uah: float = Field(0.0, description="Скільки реально витрачено минулого тижня")
    weight_change_kg: float = Field(0.0, description="Зміна ваги за тиждень, + або −")
    products: list[ProductFeedback] = Field(default_factory=list)
    dishes: list[DishFeedback] = Field(default_factory=list)
    note: str = Field("", description="Вільний підсумок тижня від бекенда")


class PlanRequest(BaseModel):
    silpo_access_token: str
    profile: Profile

    goal: Goal | None = Field(
        None,
        description=(
            "The user's own choice of direction. Left empty, it is derived from "
            "current vs target weight."
        ),
    )
    weekly_pace_kg: float = Field(
        0.0,
        description=(
            "Chosen pace of weight change, kg per week. The sign is ignored — "
            "`goal` gives the direction. 0 means the user did not choose one."
        ),
    )

    budget_uah: float = Field(ge=0, description="Weekly grocery budget, UAH")
    promo_priority: PromoPriority = "medium"
    delivery_included: bool = Field(
        True, description="Whether delivery is paid out of `budget_uah`"
    )

    workouts_per_week: int = Field(0, ge=0, le=7)
    workout_schedule: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Which days carry which workout, e.g. {'monday': 'силові'}. Days are "
            "keyed as in the plan (`monday`…`sunday`); «ПН» is accepted too."
        ),
    )
    diet_type: DietType = "none"
    allergens: list[str] = Field(default_factory=list)
    excluded_products: list[str] = Field(default_factory=list)
    fridge_items: list[str] = Field(default_factory=list)

    note: str = Field("", description="The user's own free text, nothing folded in")
    previous_feedback: WeekFeedback | None = Field(
        None,
        description=(
            "Стислий підсумок минулого тижня: що куповано і як зайшло. Замінює "
            "`previous_plan` — надсилай саме його, воно на два порядки менше."
        ),
    )
    previous_plan: dict[str, Any] | None = Field(
        None,
        description=(
            "Минулий план цілком. DEPRECATED: у промпт іде лише стисла вижимка з нього "
            "(товари, страви, витрати), бо повний JSON роздував контекст до 125k токенів "
            "і провокував модель переписати минулий кошик замість пошуку. Ігнорується, "
            "якщо задано `previous_feedback`."
        ),
    )
    apply: bool = False

    @field_validator("goal", mode="before")
    @classmethod
    def _normalize_goal(cls, value: Any) -> Any:
        return _normalize(value, GOAL_ALIASES, "goal")

    @field_validator("diet_type", mode="before")
    @classmethod
    def _normalize_diet(cls, value: Any) -> Any:
        return _normalize(value, DIET_ALIASES, "diet_type") or "none"

    @field_validator("promo_priority", mode="before")
    @classmethod
    def _normalize_promo(cls, value: Any) -> Any:
        return _normalize(value, PROMO_ALIASES, "promo_priority") or "medium"

    @field_validator("weekly_pace_kg", mode="before")
    @classmethod
    def _pace_magnitude(cls, value: Any) -> Any:
        if isinstance(value, (int, float)):
            pace = abs(float(value))
            if pace > MAX_WEEKLY_PACE_KG:
                raise ValueError(
                    f"weekly_pace_kg {value} is past {MAX_WEEKLY_PACE_KG} kg/week — "
                    "check the units, this is kilograms per week"
                )
            return pace
        return value

    @field_validator("workout_schedule", mode="before")
    @classmethod
    def _normalize_schedule(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        schedule: dict[str, str] = {}
        for day, kind in value.items():
            if not isinstance(day, str):
                raise ValueError(f"workout_schedule key {day!r} is not a day name")
            normalized = DAY_ALIASES.get(_key(day))
            if normalized is None:
                raise ValueError(
                    f"unknown workout_schedule day {day!r}; expected monday…sunday "
                    "or ПН…НД"
                )
            schedule[normalized] = str(kind or "").strip()
        return schedule

    @field_validator("allergens", "excluded_products", "fridge_items", mode="before")
    @classmethod
    def _clean_list(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return value

    @model_validator(mode="after")
    def _workouts_from_schedule(self) -> "PlanRequest":
        if self.workout_schedule and not self.workouts_per_week:
            self.workouts_per_week = len(self.workout_schedule)
        return self
