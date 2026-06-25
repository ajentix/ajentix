"""No-network option-chain replay provider backed by ``aq-options-cache-v1``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ajentix_quant.options.provider import OptionChainProvider
from ajentix_quant.options.types import OptionChainSnapshot

from .options_cache import load_normalized_cache


class ReplayOptionChainProvider(OptionChainProvider):
    """Serve cached Deribit option chains through the ``OptionChainProvider`` API."""

    name = "options_replay"

    def __init__(self, snapshots: Sequence[OptionChainSnapshot]) -> None:
        self.snapshots = tuple(snapshots)
        self._by_key: dict[tuple[str, int, int], OptionChainSnapshot] = {}
        self._expiries: dict[str, set[int]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        for snapshot in self.snapshots:
            expiries = {leg.expiry_ms for leg in snapshot.legs}
            if len(expiries) != 1:
                raise ValueError("replay snapshots must contain exactly one expiry")
            expiry_ms = next(iter(expiries))
            key = (snapshot.underlying, snapshot.snapshot_ts_ms, expiry_ms)
            if key in self._by_key:
                raise ValueError(f"duplicate replay snapshot key: {key}")
            self._by_key[key] = snapshot
            self._expiries.setdefault(snapshot.underlying, set()).add(expiry_ms)
            self._metadata.setdefault(
                snapshot.underlying,
                {
                    "exchange": snapshot.exchange,
                    "underlying": snapshot.underlying,
                    "scenario_id": snapshot.scenario_id,
                    "source_ids": set(),
                    "manifest_sha256": snapshot.manifest_sha256,
                    "expiries_ms": set(),
                    "instrument_names": set(),
                    "source_quality": {
                        key: value.value for key, value in snapshot.source_quality_map.items()
                    },
                },
            )
            meta = self._metadata[snapshot.underlying]
            meta["source_ids"].add(snapshot.source_id)
            meta["expiries_ms"].add(expiry_ms)
            for leg in snapshot.legs:
                meta["instrument_names"].add(leg.instrument_name)

        for meta in self._metadata.values():
            meta["source_ids"] = tuple(sorted(meta["source_ids"]))
            meta["expiries_ms"] = tuple(sorted(meta["expiries_ms"]))
            meta["instrument_names"] = tuple(sorted(meta["instrument_names"]))

    @classmethod
    def from_cache(cls, cache_root: str | Path, scenario_id: str) -> ReplayOptionChainProvider:
        return cls(load_normalized_cache(cache_root, scenario_id))

    def available_expiries(self, underlying: str) -> tuple[int, ...]:
        expiries = self._expiries.get(underlying.upper())
        if expiries is None:
            raise KeyError(f"no option-chain expiries cached for {underlying.upper()}")
        return tuple(sorted(expiries))

    def chain_snapshot(
        self,
        underlying: str,
        ts_ms: int,
        expiry_ms: int,
    ) -> OptionChainSnapshot:
        key = (underlying.upper(), ts_ms, expiry_ms)
        snapshot = self._by_key.get(key)
        if snapshot is None:
            raise KeyError(f"no option-chain snapshot cached for {key}")
        return snapshot

    def instrument_metadata(self, underlying: str) -> Mapping[str, Any]:
        metadata = self._metadata.get(underlying.upper())
        if metadata is None:
            raise KeyError(f"no instrument metadata cached for {underlying.upper()}")
        return metadata


__all__ = ["ReplayOptionChainProvider"]
