"""Environment settings (SPEC §3.1, §5). Secrets come only from the process environment.

No ``.env`` file is read by the application; secrets are wrapped in ``SecretStr`` so they
never appear in reprs or logs.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DEFAULT_DATABASE_URL", "Settings"]

DEFAULT_DATABASE_URL = "sqlite:///data/demo.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    database_url: str = DEFAULT_DATABASE_URL
    fixtures_dir: str = "fixtures"
    session_secret: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_model_planner: str | None = None
    anthropic_model_classifier: str | None = None
    anthropic_model_drafter: str | None = None
