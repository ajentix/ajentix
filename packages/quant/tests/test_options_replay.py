from __future__ import annotations

from pathlib import Path

import pytest

from ajentix_quant.adapters.base import SourceQuality
from ajentix_quant.adapters.deribit_options import DeribitOptionsAdapter
from ajentix_quant.data.options_replay import ReplayOptionChainProvider
from ajentix_quant.options.types import OptionChainSnapshot

SCENARIO = "tiny_eth_options_v1"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "deribit_options"
NORMALIZED_FIXTURE_ROOT = FIXTURE_ROOT / "normalized"
RAW_FIXTURE_ROOT = FIXTURE_ROOT / "raw"
TS_MS = 1717200000000
EXPIRY_MS = 1719532800000


def test_replay_returns_option_chain_snapshots_from_normalized_cache():
    provider = ReplayOptionChainProvider.from_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)

    assert provider.available_expiries("ETH") == (1719532800000, 1721952000000)
    snapshot = provider.chain_snapshot("ETH", TS_MS, EXPIRY_MS)

    assert isinstance(snapshot, OptionChainSnapshot)
    assert snapshot.exchange == "deribit"
    assert snapshot.scenario_id == SCENARIO
    assert snapshot.source_quality_map["option_chain"] is SourceQuality.FIXTURE
    assert [leg.instrument_name for leg in snapshot.legs] == [
        "ETH-28JUN24-3000-P",
        "ETH-28JUN24-3200-P",
        "ETH-28JUN24-3600-C",
    ]


def test_replay_instrument_metadata_is_deterministic_and_no_network():
    provider = ReplayOptionChainProvider.from_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)

    metadata = provider.instrument_metadata("ETH")

    assert metadata["exchange"] == "deribit"
    assert metadata["underlying"] == "ETH"
    assert metadata["scenario_id"] == SCENARIO
    assert metadata["source_ids"] == ("fixture-deribit-options",)
    assert metadata["expiries_ms"] == (1719532800000, 1721952000000)
    assert "ETH-28JUN24-3000-P" in metadata["instrument_names"]


def test_absent_replay_symbol_or_snapshot_raises_without_fallback():
    provider = ReplayOptionChainProvider.from_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)

    with pytest.raises(KeyError):
        provider.available_expiries("BTC")
    with pytest.raises(KeyError):
        provider.chain_snapshot("ETH", TS_MS + 1, EXPIRY_MS)
    with pytest.raises(KeyError):
        provider.instrument_metadata("BTC")


def test_from_cache_never_constructs_live_adapter_or_reads_raw_files(monkeypatch):
    def blocked_exchange(self):
        raise AssertionError("replay must not touch DeribitOptionsAdapter")

    monkeypatch.setattr(DeribitOptionsAdapter, "_exchange", blocked_exchange)
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if str(self).startswith(str(RAW_FIXTURE_ROOT)):
            raise AssertionError("replay must not read raw-source fixture files")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    provider = ReplayOptionChainProvider.from_cache(NORMALIZED_FIXTURE_ROOT, SCENARIO)
    assert provider.chain_snapshot("ETH", TS_MS, EXPIRY_MS).underlying == "ETH"
