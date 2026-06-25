from __future__ import annotations

import json
from pathlib import Path

import scripts.merge_vrp_free_history_caches as merger


def _write_chunk(root: Path, scenario_id: str, rows: list[dict], date_range: dict) -> None:
    chunk_dir = root / scenario_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / "trades.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    (chunk_dir / "manifest.json").write_text(
        json.dumps(
            {
                "date_range": date_range,
                "source_ids": ["deribit-history:ETH:option-trades"],
                "source_url_ids": ["https://history.deribit.com/api/v2/public/x"],
                "source_quality": {"option_trades": "venue", "underlying_index": "venue"},
                "row_counts": {"trades": len(rows)},
                "cache_fabricated": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_merge_concatenates_rows_and_derives_window(tmp_path: Path, monkeypatch) -> None:
    scenario = merger.DEFAULT_SCENARIO_ID
    c0 = tmp_path / "c0"
    c1 = tmp_path / "c1"
    _write_chunk(
        c0,
        scenario,
        [{"trade_id": "ETH-1"}, {"trade_id": "ETH-2"}],
        {"start_ts_ms": 100, "coverage_start_ts_ms": 150, "end_ts_ms": 400},
    )
    _write_chunk(
        c1,
        scenario,
        [{"trade_id": "ETH-2"}, {"trade_id": "ETH-3"}],
        {"start_ts_ms": 390, "coverage_start_ts_ms": 390, "end_ts_ms": 900},
    )

    captured: dict = {}

    def fake_write(root, scenario_id, **kwargs):
        captured["root"] = root
        captured["scenario_id"] = scenario_id
        captured["kwargs"] = kwargs
        out = Path(root) / scenario_id
        out.mkdir(parents=True, exist_ok=True)
        # Emulate dedupe-by-trade_id done inside the real writer.
        deduped = {row["trade_id"]: row for row in kwargs["raw_rows"]}
        (out / "manifest.json").write_text(
            json.dumps(
                {
                    "row_counts": {"trades": len(deduped)},
                    "date_range": {
                        "start_ts_ms": kwargs["start_ts_ms"],
                        "coverage_start_ts_ms": kwargs["coverage_start_ts_ms"],
                        "end_ts_ms": kwargs["end_ts_ms"],
                    },
                    "cache_fabricated": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return out

    monkeypatch.setattr(merger, "write_vrp_free_history_cache", fake_write)

    report = merger.merge_caches(
        chunk_roots=[c0, c1],
        out_root=tmp_path / "out",
        scenario_id=scenario,
        coverage_start_ts_ms=150,
        download_timestamp_ms=12345,
    )

    kwargs = captured["kwargs"]
    # All four raw rows passed through (writer dedupes; merge does not drop pre-emptively).
    assert [r["trade_id"] for r in kwargs["raw_rows"]] == ["ETH-1", "ETH-2", "ETH-2", "ETH-3"]
    # Overall window = min start / max end across chunks; coverage from the explicit arg.
    assert kwargs["start_ts_ms"] == 100
    assert kwargs["end_ts_ms"] == 900
    assert kwargs["coverage_start_ts_ms"] == 150
    assert kwargs["currency"] == "ETH"
    assert kwargs["source_quality"] == {"option_trades": "venue", "underlying_index": "venue"}
    assert report["input_rows_total"] == 4
    assert report["deduped_trade_rows"] == 3
    assert report["cache_fabricated"] is False
