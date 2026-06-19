from __future__ import annotations

import json
from pathlib import Path

from ajentix_quant.data.cache import SCHEMA_VERSION, load_dataset

COMMITTED_FIXTURES = (
    (Path("tests/fixtures/stage1"), "structural_v1"),
    (Path("tests/fixtures/edge"), "edge_demo_v1"),
)


def test_committed_fixtures_load_validate_and_keep_schema_version() -> None:
    for cache_root, scenario_id in COMMITTED_FIXTURES:
        dataset = load_dataset(cache_root, scenario_id)
        manifest = json.loads(
            (cache_root / scenario_id / "manifest.json").read_text(encoding="utf-8")
        )

        assert dataset.scenario_id == scenario_id
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["scenario_id"] == scenario_id
        assert manifest["sha256_by_file"]
