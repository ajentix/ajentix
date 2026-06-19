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
    health_factor_floor: float = 1.5
    vol_spike_annual: float = 1.0
    funding_compression_8h: float = 0.00005
    funding_reversal_imminent_8h: float = 0.0
    max_net_delta_frac: float = 0.02
    gap_stress_pct: float = 0.20
    adl_rank_threshold: int = 3

    # --- strategy ---
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    )
    min_funding_rate_8h: float = 0.0001  # 0.01%/8h entry threshold

    # --- data / cache (Phase 1) ---
    cache_dir: str = "data/cache"
    timeframe: str = "1h"
    gate_scenario_id: str = "stage1_hybrid_v1"
    edge_verdict_scenario_id: str = "bybit_real_v1"

    # --- costs (deterministic; bps unless noted) ---
    perp_taker_fee_bps: float = 5.5  # Bybit non-VIP linear taker ~0.055%
    perp_maker_fee_bps: float = 2.0  # ~0.02%
    spot_taker_fee_bps: float = 10.0  # ~0.10%
    leverage_cost_apr: float = 0.0  # deterministic borrow/financing drag (per scenario)

    # --- slippage (size-based, deterministic) ---
    slippage_base_bps: float = 1.0
    slippage_impact_bps_per_pct_volume: float = 5.0
    slippage_cap_bps: float = 50.0

    # --- small-capital sizing ---
    default_capital_usd: float = 1000.0  # within [capital_usd_min, capital_usd_max]


settings = Settings()
