"""Typed configuration via pydantic-settings. Env vars use the AQ_ prefix."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AQ_", extra="ignore")

    # --- venue ---
    venue: str = "bybit"
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    # --- capital (range, not fixed) ---
    capital_usd_min: float = 500.0
    capital_usd_max: float = 2000.0

    # --- risk / dynamic leverage ---
    base_leverage: float = 2.0
    max_leverage: float = 5.0
    min_liq_distance_pct: float = 0.15
    reserve_pct: float = 0.25
    max_drawdown_pct: float = 0.05
    funding_reversal_exit_hours: int = 24
    max_position_pct: float = 0.25

    # --- strategy ---
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT"])
    min_funding_rate_8h: float = 0.0001  # 0.01%/8h entry threshold


settings = Settings()
