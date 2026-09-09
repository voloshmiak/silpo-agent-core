import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Day = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

PRODUCT_URL = "https://silpo.ua/product/{slug}"

PRODUCT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$")
IMAGE_HOST = "https://images.silpo.ua/"

DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class _Model(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class Macros(_Model):
    kcal: int = Field(description="Калорійність, ккал")
    protein_g: int = Field(description="Білки, г")
    fat_g: int = Field(description="Жири, г")
    carbs_g: int = Field(description="Вуглеводи, г")


class Targets(Macros):
    estimated_weeks_to_goal: float = Field(
        0.0, description="Орієнтовний строк досягнення цільової ваги, тижнів (з calc_targets)"
    )
    estimated_goal_date: str = Field(
        "", description="Орієнтовна дата досягнення цілі, YYYY-MM-DD (з calc_targets)"
    )


class Meal(Macros):
    title: str = Field(description="Назва страви українською")
    items: list[str] = Field(
        default_factory=list, description="Складники з вагою, напр. «вівсянка 60 г»"
    )


class DayPlan(Macros):
    day: Day = Field(description="Ключ дня: monday…sunday")
    workout: bool = Field(False, description="Чи є цього дня тренування")
    breakfast: Meal
    lunch: Meal
    snack: Meal = Field(description="Перекус")
    dinner: Meal


class CartItem(_Model):
    name: str = Field(description="Назва товару як у «Сільпо»")
    product_id: str = Field("", description="ID товару з MCP")
    slug: str = Field("", description="Slug товару з MCP — з нього будується посилання")
    url: str = Field("", description="Посилання на сторінку товару (будується зі slug)")
    image_url: str = Field(
        "", description="Пряме посилання на зображення з MCP: поле image або images[0]"
    )
    quantity: float = Field(1, description="Кількість одиниць")
    unit: str = Field("", description="Одиниця, напр. «шт», «кг»")
    price: float = Field(
        description="Ціна за одиницю ПІСЛЯ знижок, грн — поле price позиції кошика"
    )
    old_price: float = Field(
        0.0, description="Ціна за одиницю до знижок, грн — поле oldPrice позиції кошика, або 0"
    )
    total_price: float = Field(
        description="Сума позиції після знижок, грн — поле total позиції кошика"
    )
    discount_uah: float = Field(
        0.0, description="Знижка на позицію, грн — поле subDiscount позиції кошика"
    )

    @model_validator(mode="after")
    def _line_total_matches(self) -> "CartItem":
        expected = round(self.price * self.quantity, 2)
        if abs(expected - self.total_price) > max(0.05, expected * 0.02):
            raise ValueError(
                f"«{self.name}»: total_price {self.total_price} не збігається з "
                f"price × quantity ({self.price} × {self.quantity} = {expected}). "
                "price — це ціна за одиницю (поле price позиції кошика), "
                "total_price — сума всієї позиції (поле total). Для вагового товару "
                "price за кілограм, а quantity в кілограмах."
            )
        return self

    @model_validator(mode="after")
    def _link_from_slug(self) -> "CartItem":
        if not PRODUCT_SLUG.match(self.slug):
            raise ValueError(
                f"«{self.name}»: slug {self.slug!r} не з «Сільпо». Справжній slug "
                "закінчується артикулом товару (напр. «banan-32485») і береться "
                "дослівно з silpo_find_products_batch чи silpo_get_product_details. "
                "Знайди реальний товар і візьми slug звідти — не складай його сам."
            )
        if not self.image_url.startswith(IMAGE_HOST):
            raise ValueError(
                f"«{self.name}»: image_url {self.image_url!r} не з «Сільпо». Візьми "
                f"поле image з пошуку або images[0] з деталей товару — воно завжди "
                f"починається з {IMAGE_HOST}."
            )
        self.url = PRODUCT_URL.format(slug=self.slug)
        return self


class Summary(_Model):
    total_uah: float = Field(
        description=(
            "Сума до сплати, грн — calculation.totalAfterDiscounts кошика "
            "(товари зі знижками + доставка)"
        )
    )
    products_total_uah: float = Field(
        0.0, description="Сума товарів після знижок, грн — calculation.productsTotal"
    )
    delivery_uah: float = Field(
        0.0, description="Вартість доставки, грн — calculation.delivery.total"
    )
    discount_uah: float = Field(
        0.0, description="Загальна економія на знижках, грн — calculation.subDiscount"
    )
    budget_uah: float = Field(description="Бюджет із запиту, грн")
    remaining_uah: float = Field(description="Залишок бюджету, грн")
    restrictions: list[str] = Field(
        default_factory=list, description="Враховані харчові обмеження користувача"
    )
    promotions: list[str] = Field(
        default_factory=list, description="Використані акції, промо та купони"
    )
    notes: str = Field(
        "", description="Нотатки: припущення, адаптація проти минулого тижня, компроміси"
    )


class Plan(_Model):
    targets: Targets
    days: list[DayPlan] = Field(
        description="Рівно 7 днів, від monday до sunday, кожен день без пропусків",
        min_length=7,
        max_length=7,
    )
    cart: list[CartItem] = Field(description="Фінальний кошик товарів")
    summary: Summary

    @field_validator("days", mode="after")
    @classmethod
    def _all_seven_days(cls, days: list[DayPlan]) -> list[DayPlan]:
        seen = {day.day for day in days}
        missing = [key for key in DAYS if key not in seen]
        if missing:
            raise ValueError(f"missing days: {', '.join(missing)}")
        return sorted(days, key=lambda day: DAYS.index(day.day))
