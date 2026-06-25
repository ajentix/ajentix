"""Free DefiLlama yields client (stdlib-only) with a hashed on-disk snapshot.

The endpoint https://yields.llama.fi/pools is free and unauthenticated. We fetch once, write a
content-hashed snapshot to disk, and do all ranking offline against that snapshot so a report is
fully reproducible from a recorded hash (the no-fabrication / manifest-hash discipline carried over
from ajentix-quant). Network is the only online step; nothing is synthesized.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

YIELDS_URL = "https://yields.llama.fi/pools"
_TIMEOUT_S = 30


@dataclass(frozen=True, kw_only=True)
class YieldsSnapshot:
    fetched_at_utc: str
    source_url: str
    pool_count: int
    sha256: str
    pools: tuple[dict[str, Any], ...]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_pools(*, url: str = YIELDS_URL) -> list[dict[str, Any]]:
    """Fetch the live free yields list. Raises on non-success payloads (fail closed)."""
    req = urllib.request.Request(url, headers={"User-Agent": "ajentix-alpha/0.1 (research)"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed https host
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("yields endpoint did not return status=success")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("yields endpoint returned no pool data")
    return [row for row in data if isinstance(row, dict)]


def write_snapshot(
    root: str | Path, pools: list[dict[str, Any]], *, source_url: str = YIELDS_URL
) -> YieldsSnapshot:
    """Write a deterministic, content-hashed snapshot of the fetched pools."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(pools, sort_keys=True, separators=(",", ":"))
    sha = _sha256_text(canonical)
    snapshot = YieldsSnapshot(
        fetched_at_utc=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_url=source_url,
        pool_count=len(pools),
        sha256=sha,
        pools=tuple(pools),
    )
    (out_dir / "pools.json").write_text(canonical + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fetched_at_utc": snapshot.fetched_at_utc,
                "source_url": snapshot.source_url,
                "pool_count": snapshot.pool_count,
                "sha256": snapshot.sha256,
                "fabricated": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return snapshot


def load_snapshot(root: str | Path) -> YieldsSnapshot:
    """Load and verify a written snapshot; fail closed on hash drift."""
    in_dir = Path(root)
    pools_text = (in_dir / "pools.json").read_text(encoding="utf-8").strip()
    manifest = json.loads((in_dir / "manifest.json").read_text(encoding="utf-8"))
    if _sha256_text(pools_text) != manifest["sha256"]:
        raise ValueError("yields snapshot sha256 drift; refusing to use a tampered snapshot")
    pools = json.loads(pools_text)
    return YieldsSnapshot(
        fetched_at_utc=str(manifest["fetched_at_utc"]),
        source_url=str(manifest["source_url"]),
        pool_count=int(manifest["pool_count"]),
        sha256=str(manifest["sha256"]),
        pools=tuple(pools),
    )


def archive_snapshot(root: str | Path, snapshot: YieldsSnapshot) -> Path:
    """Copy a snapshot into a timestamped, content-hashed history dir for over-time monitoring.

    Each archive dir is self-contained (pools.json + manifest.json) and loadable by load_snapshot,
    so the monitor can diff any two retained points. Re-archiving the same content is idempotent.
    """
    stamp = snapshot.fetched_at_utc.replace(":", "").replace("-", "")
    out_dir = Path(root) / "history" / f"{stamp}-{snapshot.sha256[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(list(snapshot.pools), sort_keys=True, separators=(",", ":"))
    (out_dir / "pools.json").write_text(canonical + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fetched_at_utc": snapshot.fetched_at_utc,
                "source_url": snapshot.source_url,
                "pool_count": snapshot.pool_count,
                "sha256": snapshot.sha256,
                "fabricated": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return out_dir


def list_history(root: str | Path) -> list[Path]:
    """Return archived snapshot dirs (those containing a manifest.json), oldest first."""
    hist = Path(root) / "history"
    if not hist.is_dir():
        return []
    dirs = [d for d in hist.iterdir() if d.is_dir() and (d / "manifest.json").is_file()]
    return sorted(dirs, key=lambda d: d.name)
