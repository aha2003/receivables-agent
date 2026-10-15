"""``config.yaml`` loader (SPEC §5, §18.10; docs/DECISIONS.md D-011, D-014, D-017, D-018).

The file is parsed with PyYAML's ``BaseLoader``: every scalar stays a string and no tags are
honoured, so neither floats nor arbitrary objects can appear. Pydantic validates the strings
strictly (unknown keys are errors) and engine weights and rates become exact ``Fraction``s.
A missing file means the §5 and §18.10 defaults.
"""

import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from receivables.core.config import EngineConfig
from receivables.core.draft_validator import mentions
from receivables.core.money import to_fils

__all__ = [
    "AgentSection",
    "AppSection",
    "Config",
    "ConfigError",
    "DraftsSection",
    "L2Section",
    "LlmSection",
    "MailSection",
    "load_config",
]


class ConfigError(ValueError):
    """config.yaml is malformed or violates the §5 schema."""


def _fraction(value: str) -> Fraction:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"not a decimal number: {value!r}") from None
    if not number.is_finite() or "e" in value.lower():
        raise ValueError(f"write a plain decimal number, not {value!r}")
    return Fraction(number)


def _plain_integer(value: Any) -> Any:
    if isinstance(value, str) and not value.strip().isdigit():
        raise ValueError(f"write a whole number, not {value!r}")
    return value


PlainInt = Annotated[int, BeforeValidator(_plain_integer)]


class _UniqueKeyLoader(yaml.BaseLoader):
    """BaseLoader (strings only, no tags) that refuses duplicate keys (no silent last-wins)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)  # pyright: ignore[reportArgumentType]
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Weights(_Strict):
    overdue: str = "0.40"
    history: str = "0.25"
    size: str = "0.15"
    dispute: str = "0.10"
    promise: str = "0.10"


class _Engine(_Strict):
    min_history_invoices: PlainInt = Field(default=5, ge=1)
    history_window_days: PlainInt = Field(default=365, ge=1)
    grace_days: PlainInt = Field(default=0, ge=0)
    weights: _Weights = _Weights()
    overdue_cap_days: PlainInt = Field(default=90, ge=1)
    band_high: str = "60.0"
    band_medium: str = "30.0"
    queue_size: PlainInt = Field(default=20, ge=1)
    # §2/§6.6 define the KPI as the *90-day* sales proxy; §5 makes the key configurable.
    # Only 90 is accepted so the label stays true (docs/DECISIONS.md D-017).
    dso_window_days: PlainInt = Field(default=90, ge=90, le=90)
    no_history_fallback_rate: str = "0.5"

    def to_engine_config(self) -> EngineConfig:
        w = self.weights
        weights = [_fraction(x) for x in (w.overdue, w.history, w.size, w.dispute, w.promise)]
        if any(not 0 <= x <= 1 for x in weights) or sum(weights) != 1:
            raise ValueError("weights must each be in [0, 1] and sum to exactly 1")
        band_high, band_medium = _fraction(self.band_high), _fraction(self.band_medium)
        if not 0 < band_medium < band_high <= 100:
            raise ValueError("bands need 0 < band_medium < band_high <= 100")
        fallback = _fraction(self.no_history_fallback_rate)
        if not 0 <= fallback <= 1:
            raise ValueError("no_history_fallback_rate must be in [0, 1]")
        return EngineConfig(
            min_history_invoices=self.min_history_invoices,
            history_window_days=self.history_window_days,
            grace_days=self.grace_days,
            weight_overdue=weights[0],
            weight_history=weights[1],
            weight_size=weights[2],
            weight_dispute=weights[3],
            weight_promise=weights[4],
            overdue_cap_days=self.overdue_cap_days,
            band_high=band_high,
            band_medium=band_medium,
            queue_size=self.queue_size,
            dso_window_days=self.dso_window_days,
            no_history_fallback_rate=fallback,
        )


class _ModelEnvs(_Strict):
    planner: str = "ANTHROPIC_MODEL_PLANNER"
    classifier: str = "ANTHROPIC_MODEL_CLASSIFIER"
    drafter: str = "ANTHROPIC_MODEL_DRAFTER"


class _Llm(_Strict):
    provider: Literal["anthropic"] = "anthropic"
    api_key_env: str = "ANTHROPIC_API_KEY"
    model_envs: _ModelEnvs = _ModelEnvs()
    max_retries: PlainInt = Field(default=1, ge=0, le=5)
    timeout_seconds: PlainInt = Field(default=30, ge=1, le=300)
    redact_before_send: bool = True
    pseudonymize_customer_names: bool = True
    review_confidence_threshold: str = "0.6"
    replay: bool = False
    cassette_dir: str = "tests/cassettes"


SENDER_NAME_MAX_WORDS = 8
_DOMAIN = re.compile(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")


class _Drafts(_Strict):
    max_regenerations: PlainInt = Field(default=2, ge=0, le=10)
    max_words: PlainInt = Field(default=140, ge=1)
    invoice_number_regex: str = r"\b[A-Z]{2,6}-\d{2,}\b"
    sender_name: str = Field(default="Accounts Receivable Team", min_length=1, max_length=100)
    # §18.9: the one domain (besides the recipient's address) a draft may link or write to.
    sender_domain: str | None = None

    @field_validator("sender_domain")
    @classmethod
    def _bare_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DOMAIN.fullmatch(value):
            raise ValueError("sender_domain must be a bare domain such as example.com")
        return value.lower()

    @field_validator("sender_name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if value != value.strip() or any(not ch.isprintable() for ch in value):
            raise ValueError("sender_name must be printable text without outer whitespace")
        if len(value.split()) > SENDER_NAME_MAX_WORDS:
            raise ValueError(f"sender_name must be at most {SENDER_NAME_MAX_WORDS} words")
        return value

    @model_validator(mode="after")
    def _sender_name_mentions_nothing(self) -> "_Drafts":
        # The sign-off goes into every template; it must not trip the draft validator (§8.2).
        found = mentions(self.sender_name, self.invoice_number_regex)
        if found:
            raise ValueError(f"sender_name may not contain {', '.join(found)} material")
        return self

    @field_validator("invoice_number_regex")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as err:
            raise ValueError(f"invoice_number_regex does not compile: {err}") from None
        return value


class _App(_Strict):
    data_dir: str = "data"
    base_currency: Literal["AED"] = "AED"
    require_https: bool = True
    session_ttl_minutes: PlainInt = Field(default=480, ge=1, le=24 * 60)
    login_rate_limit_per_minute: PlainInt = Field(default=5, ge=1, le=100)
    retention_days_after_pilot: PlainInt = Field(default=30, ge=0)


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def _hhmm(value: str) -> time:
    match = _HHMM.fullmatch(value)
    if match is None:
        raise ValueError(f"write a 24-hour time as HH:MM, not {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


class _L2(_Strict):
    allowlisted_customers: list[str] = Field(default_factory=list)
    max_total_open_amount: str = "25000"  # AED; becomes fils
    min_days_between_contacts: PlainInt = Field(default=7, ge=0)
    daily_send_cap: PlainInt = Field(default=20, ge=0)
    business_days: list[str] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    business_hours: list[str] = Field(default_factory=lambda: ["09:00", "17:00"])
    timezone: str = "Asia/Dubai"
    kill_switch: bool = False

    @field_validator("allowlisted_customers")
    @classmethod
    def _customer_ids(cls, value: list[str]) -> list[str]:
        if any(not x.strip() or x != x.strip() for x in value) or len(set(value)) != len(value):
            raise ValueError("allowlisted_customers must be distinct, non-empty customer ids")
        return value

    @field_validator("business_days")
    @classmethod
    def _weekdays(cls, value: list[str]) -> list[str]:
        if not value or any(d not in WEEKDAYS for d in value) or len(set(value)) != len(value):
            raise ValueError(f"business_days must be distinct names from {', '.join(WEEKDAYS)}")
        return value

    @field_validator("business_hours")
    @classmethod
    def _hours(cls, value: list[str]) -> list[str]:
        if len(value) != 2 or _hhmm(value[0]) >= _hhmm(value[1]):
            raise ValueError("business_hours must be [start, end] with start before end")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value

    def max_total_open_fils(self) -> int:
        value = self.max_total_open_amount
        if not re.fullmatch(r"[0-9]+(\.[0-9]{1,2})?", value):  # ASCII digits only
            raise ValueError(f"max_total_open_amount must be a plain AED amount, not {value!r}")
        fils = to_fils(value)
        if fils <= 0:
            raise ValueError("max_total_open_amount must be above zero")
        return fils


class _Agent(_Strict):
    autonomy_level: PlainInt = Field(default=1, ge=0, le=2)  # 0 shadow, 1 assist, 2 policy-gated
    max_cases_per_tick: PlainInt = Field(default=20, ge=1, le=20)  # §18.6: at most 20
    # §18.6 allows at most 5 steps; the rule planner's plans have up to 3, which must fit.
    max_steps_per_case: PlainInt = Field(default=5, ge=3, le=5)
    llm_timeout_seconds: PlainInt = Field(default=30, ge=1, le=300)
    promise_grace_days: PlainInt = Field(default=2, ge=0)
    l2: _L2 = _L2()


class _Mail(_Strict):
    gateway: Literal["sandbox", "smtp"] = "sandbox"
    enabled: bool = False

    @model_validator(mode="after")
    def _no_smtp_yet(self) -> "_Mail":
        # CLAUDE.md: L2 writes only to the sandbox outbox, no real SMTP. The optional Mailpit
        # SMTP sandbox is S5.09, which must lift this together with a localhost-only check.
        if self.gateway == "smtp" and self.enabled:
            raise ValueError("SMTP sending is disabled in this prototype (sandbox outbox only)")
        return self


class _File(_Strict):
    engine: _Engine = _Engine()
    llm: _Llm = _Llm()
    drafts: _Drafts = _Drafts()
    app: _App = _App()
    agent: _Agent = _Agent()
    mail: _Mail = _Mail()


@dataclass(frozen=True, slots=True)
class LlmSection:
    provider: str
    api_key_env: str
    model_envs: dict[str, str]
    max_retries: int
    timeout_seconds: int
    redact_before_send: bool
    pseudonymize_customer_names: bool
    review_confidence_threshold: Fraction
    replay: bool
    cassette_dir: str


@dataclass(frozen=True, slots=True)
class DraftsSection:
    max_regenerations: int
    max_words: int
    invoice_number_regex: str
    sender_name: str
    sender_domain: str | None = None


@dataclass(frozen=True, slots=True)
class AppSection:
    data_dir: str
    base_currency: str
    require_https: bool
    session_ttl_minutes: int
    login_rate_limit_per_minute: int
    retention_days_after_pilot: int


@dataclass(frozen=True, slots=True)
class L2Section:
    """The L2 send policy limits of §18.4 (read by the policy engine)."""

    allowlisted_customers: tuple[str, ...]
    max_total_open_fils: int
    min_days_between_contacts: int
    daily_send_cap: int
    business_days: tuple[str, ...]
    business_hours: tuple[time, time]
    timezone: str
    kill_switch: bool


@dataclass(frozen=True, slots=True)
class AgentSection:
    autonomy_level: int
    max_cases_per_tick: int
    max_steps_per_case: int
    llm_timeout_seconds: int
    promise_grace_days: int
    l2: L2Section


@dataclass(frozen=True, slots=True)
class MailSection:
    gateway: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class Config:
    engine: EngineConfig
    llm: LlmSection
    drafts: DraftsSection
    app: AppSection
    agent: AgentSection
    mail: MailSection


def load_config(path: Path) -> Config:
    """Read and validate ``path``; the §5 defaults when the file does not exist."""
    raw: Any = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            raw = yaml.load(text, Loader=_UniqueKeyLoader) or {}  # noqa: S506 - strings only, no tags
        except yaml.YAMLError as err:
            raise ConfigError(f"{path} is not valid YAML: {err}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping of sections")
    try:
        parsed = _File.model_validate(raw)
        engine = parsed.engine.to_engine_config()
        threshold = _fraction(parsed.llm.review_confidence_threshold)
        if not 0 <= threshold <= 1:
            raise ValueError("review_confidence_threshold must be in [0, 1]")
        l2 = parsed.agent.l2
        max_total_open_fils = l2.max_total_open_fils()
    except (ValidationError, ValueError) as err:
        raise ConfigError(f"{path}: {err}") from None
    llm = parsed.llm
    return Config(
        engine=engine,
        llm=LlmSection(
            provider=llm.provider,
            api_key_env=llm.api_key_env,
            model_envs=llm.model_envs.model_dump(),
            max_retries=llm.max_retries,
            timeout_seconds=llm.timeout_seconds,
            redact_before_send=llm.redact_before_send,
            pseudonymize_customer_names=llm.pseudonymize_customer_names,
            review_confidence_threshold=threshold,
            replay=llm.replay,
            cassette_dir=llm.cassette_dir,
        ),
        drafts=DraftsSection(**parsed.drafts.model_dump()),
        app=AppSection(**parsed.app.model_dump()),
        agent=AgentSection(
            autonomy_level=parsed.agent.autonomy_level,
            max_cases_per_tick=parsed.agent.max_cases_per_tick,
            max_steps_per_case=parsed.agent.max_steps_per_case,
            llm_timeout_seconds=parsed.agent.llm_timeout_seconds,
            promise_grace_days=parsed.agent.promise_grace_days,
            l2=L2Section(
                allowlisted_customers=tuple(l2.allowlisted_customers),
                max_total_open_fils=max_total_open_fils,
                min_days_between_contacts=l2.min_days_between_contacts,
                daily_send_cap=l2.daily_send_cap,
                business_days=tuple(l2.business_days),
                business_hours=(_hhmm(l2.business_hours[0]), _hhmm(l2.business_hours[1])),
                timezone=l2.timezone,
                kill_switch=l2.kill_switch,
            ),
        ),
        mail=MailSection(gateway=parsed.mail.gateway, enabled=parsed.mail.enabled),
    )
