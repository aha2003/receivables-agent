"""Environment settings (SPEC §3.1, §3.2, §5; docs/DECISIONS.md D-029).

Secrets come only from the process environment: no ``.env`` file is read by the application
(``uv run --env-file .env`` loads it into the process). The API key and session secret are
``SecretStr``; they and the model ids are left out of the settings' repr, so printing or
logging the settings never shows them.

``LLM_MODE`` (``fake``, ``replay`` or ``live``; default ``fake``) is the single switch for the
LLM client. ``LLM_REPLAY=true`` is accepted as a synonym for ``replay``; contradicting values
are refused.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DEFAULT_DATABASE_URL", "LlmMode", "Settings"]

DEFAULT_DATABASE_URL = "sqlite:///data/demo.db"
LlmMode = Literal["fake", "replay", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    database_url: str = DEFAULT_DATABASE_URL
    fixtures_dir: str = "fixtures"
    config_path: str = "config.yaml"
    session_secret: SecretStr | None = Field(default=None, repr=False)
    anthropic_api_key: SecretStr | None = Field(default=None, repr=False)
    anthropic_model_planner: str | None = Field(default=None, repr=False)
    anthropic_model_classifier: str | None = Field(default=None, repr=False)
    anthropic_model_drafter: str | None = Field(default=None, repr=False)
    llm_mode: LlmMode = "fake"
    llm_replay: bool | None = None  # LLM_REPLAY: a synonym for LLM_MODE=replay (T-DEMO-01)

    @model_validator(mode="after")
    def _replay_synonym(self) -> Self:
        if self.llm_replay is None:
            return self
        mode_given = "llm_mode" in self.model_fields_set
        if self.llm_replay and mode_given and self.llm_mode != "replay":
            raise ValueError("LLM_REPLAY=true contradicts LLM_MODE; set only one of them")
        if not self.llm_replay and self.llm_mode == "replay":
            raise ValueError("LLM_REPLAY=false contradicts LLM_MODE=replay")
        if self.llm_replay:
            object.__setattr__(self, "llm_mode", "replay")
        return self
