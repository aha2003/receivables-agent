"""The simulator's scenario config (SPEC §19.2; values marked [A] there are assumptions).

``scenarios/demo_company.json`` holds the §19.2 demo company. The file is read strictly:
unknown fields, wrong types (no coercion) and inconsistent values are refused.
"""

from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["DEFAULT_SCENARIO", "Archetype", "Scenario", "load_scenario"]

DEFAULT_SCENARIO = Path(__file__).resolve().parent / "scenarios" / "demo_company.json"

Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Archetype(StrEnum):
    """The four §19.2 customer behaviours; the behaviour itself is S4.02."""

    RESPONSIVE = "responsive"
    DISPUTER = "disputer"
    SILENT = "silent"
    CLAIMS_PAID = "claims_paid"


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=64)
    seed: int = Field(ge=0)
    start_date: date
    days: int = Field(ge=1, le=366)
    tick_time: time
    timezone: Literal["Asia/Dubai"]
    working_days: tuple[Weekday, ...] = Field(min_length=1, max_length=7)
    customers: int = Field(ge=1, le=99)
    customers_per_archetype: int = Field(ge=1)
    archetypes: tuple[Archetype, ...] = Field(min_length=1)
    invoices_per_customer: int = Field(ge=1, le=99)
    payment_terms_days: int = Field(ge=1, le=365)
    currency: Literal["AED"]
    amount_step_aed: int = Field(ge=1)
    amount_units_min: int = Field(ge=1)
    amount_units_max: int = Field(ge=1)
    due_offset_min_days: int = Field(ge=0)
    due_offset_max_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.working_days)) != len(self.working_days):
            raise ValueError("working_days repeats a day")
        if len(set(self.archetypes)) != len(self.archetypes):
            raise ValueError("archetypes repeats an archetype")
        if self.customers != len(self.archetypes) * self.customers_per_archetype:
            raise ValueError("customers must equal archetypes x customers_per_archetype")
        if self.amount_units_min > self.amount_units_max:
            raise ValueError("amount_units_min is above amount_units_max")
        if self.due_offset_min_days > self.due_offset_max_days:
            raise ValueError("due_offset_min_days is above due_offset_max_days")
        if self.due_offset_max_days > self.payment_terms_days:
            # An invoice must already exist on day 0: it is dated terms days before it falls due.
            raise ValueError("due_offset_max_days is above payment_terms_days")
        return self


def load_scenario(path: Path = DEFAULT_SCENARIO, *, seed: int | None = None) -> Scenario:
    """Read a scenario file; ``seed`` overrides its seed (checked like the file's)."""
    scenario = Scenario.model_validate_json(path.read_bytes())
    if seed is None:
        return scenario
    return Scenario.model_validate(scenario.model_dump() | {"seed": seed})
