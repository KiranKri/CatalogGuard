"""Environment configuration. Defaults keep the stub LLM and SQLite."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./catalogguard.db"
    default_low_stock_threshold: int = 5
    low_stock_cooldown_seconds: int = 86400
    email_lookback_hours: int = 168
    email_handoff_batch_size: int = 50
    email_handoff_max_age_hours: int = 72
    llm_provider: str = "stub"
    llm_base_url: str = "https://api.x.ai/v1"
    llm_api_key: str = ""
    llm_model: str = "grok-4.7"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3
    llm_daily_cap_per_sku: int = 24
    handler_max_retries: int = 3
    retry_backoff_seconds: float = 0.2
    outbox_claim_timeout_seconds: int = 300
    outbox_lease_renew_seconds: int = 60
    redrive_claim_timeout_seconds: int = 300
    event_dispatch: str = "inline"
    log_level: str = "INFO"
    seed_on_startup: bool = False
    prompts_dir: str = ""

    def resolved_api_key(self) -> str:
        if self.llm_api_key:
            return self.llm_api_key
        return os.environ.get("XAI_API_KEY", "")

    def prompts_path(self) -> Path:
        if self.prompts_dir:
            return Path(self.prompts_dir)
        here = Path(__file__).resolve()
        candidates = [
            Path.cwd() / "prompts",
            here.parents[3] / "prompts",
            here.parents[2] / "prompts",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[0]
