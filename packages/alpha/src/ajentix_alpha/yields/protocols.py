"""Free protocol-metadata client (api.llama.fi/protocols) + a content-hashed snapshot.

The yields model scores market risk (volatility, IL, liquidity) but not *protocol* risk — yet the
largest DeFi losses are smart-contract exploits and rugs, not adverse APY moves. This client pulls
the free, unauthenticated DefiLlama protocols list (audit count, listing age, chains, category) and
snapshots it; `model.py` then attaches UNAUDITED / YOUNG_PROTOCOL / UNKNOWN_PROTOCOL flags offline
and blocks audit/age failures from the capital-preservation CORE tier.

Only a small, stable slice of each protocol record is retained (the fields the model reads), keeping
the snapshot compact and the hash meaningful.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOLS_URL = "https://api.llama.fi/protocols"
_TIMEOUT_S = 30
_KEEP_FIELDS = ("name", "slug", "category", "audits", "listedAt", "chains", "tvl", "url")


@dataclass(frozen=True, kw_only=True)
class ProtocolsSnapshot:
    fetched_at_utc: str
    fetched_at_epoch: float
    source_url: str
    protocol_count: int
    sha256: str
    by_slug: dict[str, dict[str, Any]]  # slug -> trimmed protocol record


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trim(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in _KEEP_FIELDS}


def fetch_protocols(*, url: str = PROTOCOLS_URL) -> dict[str, dict[str, Any]]:
    """Fetch the free protocols list, indexed by slug and trimmed to the fields the model reads."""
    req = urllib.request.Request(url, headers={"User-Agent": "ajentix-alpha/0.1 (research)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed https host
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("protocols endpoint returned no data")
    by_slug: dict[str, dict[str, Any]] = {}
    for rec in payload:
        if isinstance(rec, dict) and rec.get("slug"):
            by_slug[str(rec["slug"])] = _trim(rec)
    return by_slug


def write_snapshot(
    root: str | Path, by_slug: dict[str, dict[str, Any]], *, source_url: str = PROTOCOLS_URL
) -> ProtocolsSnapshot:
    """Write a deterministic, content-hashed snapshot of the slug-indexed protocol metadata."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(by_slug, sort_keys=True, separators=(",", ":"))
    sha = _sha256_text(canonical)
    now = datetime.now(tz=UTC)
    snap = ProtocolsSnapshot(
        fetched_at_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        fetched_at_epoch=now.timestamp(),
        source_url=source_url,
        protocol_count=len(by_slug),
        sha256=sha,
        by_slug=by_slug,
    )
    (out_dir / "protocols.json").write_text(canonical + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fetched_at_utc": snap.fetched_at_utc,
                "fetched_at_epoch": snap.fetched_at_epoch,
                "source_url": snap.source_url,
                "protocol_count": snap.protocol_count,
                "sha256": snap.sha256,
                "fabricated": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return snap


def load_snapshot(root: str | Path) -> ProtocolsSnapshot:
    """Load and verify a written protocols snapshot; fail closed on hash drift."""
    in_dir = Path(root)
    text = (in_dir / "protocols.json").read_text(encoding="utf-8").strip()
    manifest = json.loads((in_dir / "manifest.json").read_text(encoding="utf-8"))
    if _sha256_text(text) != manifest["sha256"]:
        raise ValueError("protocols snapshot sha256 drift; refusing to use a tampered snapshot")
    by_slug = json.loads(text)
    return ProtocolsSnapshot(
        fetched_at_utc=str(manifest["fetched_at_utc"]),
        fetched_at_epoch=float(manifest["fetched_at_epoch"]),
        source_url=str(manifest["source_url"]),
        protocol_count=int(manifest["protocol_count"]),
        sha256=str(manifest["sha256"]),
        by_slug=by_slug,
    )
