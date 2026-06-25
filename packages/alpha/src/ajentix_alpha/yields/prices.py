"""Free token-price client (coins.llama.fi) + a content-hashed snapshot, for depeg detection.

The yields feed has no price oracle, so a stablecoin pool that has quietly depegged still looks like
a healthy CORE position. This module fetches current token prices for pools' underlying tokens from
the free, unauthenticated coins.llama.fi endpoint, snapshots them (same no-fabrication / hash
discipline as the yields client), and feeds them into the offline peg assessment in `model.py`.

A missing or low-confidence price means "cannot verify" -> the model makes NO adjustment (fail open
to no-false-alarm); the depeg haircut only fires on a price we actually retrieved.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PRICES_URL = "https://coins.llama.fi/prices/current/"
_TIMEOUT_S = 30
_CHUNK = 80  # coins per request (keep URLs well under length limits)

# coins.llama.fi chain keys differ from DefiLlama yields chain names for a few chains.
_CHAIN_ALIASES = {
    "bsc": "bsc",
    "binance": "bsc",
    "avalanche": "avax",
    "avax": "avax",
    "gnosis": "xdai",
    "xdai": "xdai",
    "okexchain": "okexchain",
}


@dataclass(frozen=True, kw_only=True)
class PricesSnapshot:
    fetched_at_utc: str
    source_url: str
    coin_count: int
    sha256: str
    prices: dict[str, dict[str, Any]]  # "chain:address" -> {symbol, price, confidence, ...}


def coin_key(chain: str, address: str) -> str:
    """Build a coins.llama.fi coin key ('chain:address') from a yields chain + token address."""
    c = chain.strip().lower()
    return f"{_CHAIN_ALIASES.get(c, c)}:{address.strip()}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stablecoin_coin_keys(pools: list[dict[str, Any]]) -> list[str]:
    """Distinct coin keys for the underlying tokens of stablecoin pools (what we must peg-check)."""
    keys: dict[str, None] = {}
    for row in pools:
        if not row.get("stablecoin"):
            continue
        chain = str(row.get("chain", ""))
        tokens = row.get("underlyingTokens") or []
        if not isinstance(tokens, list):
            continue
        for tok in tokens:
            addr = str(tok)
            if addr and not addr.startswith("0x0000000000000000000000000000000000000000"):
                keys[coin_key(chain, addr)] = None
    return list(keys)


def _fetch_chunk(url: str, chunk: list[str]) -> dict[str, dict[str, Any]] | None:
    """Fetch one chunk; return its coins map, or None if the request/parse failed."""
    path = urllib.parse.quote(",".join(chunk), safe=":,")
    req = urllib.request.Request(
        url + path, headers={"User-Agent": "ajentix-alpha/0.1 (research)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed host
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    coins = payload.get("coins") if isinstance(payload, dict) else None
    return coins if isinstance(coins, dict) else {}


def fetch_prices(coin_keys: list[str], *, url: str = PRICES_URL) -> dict[str, dict[str, Any]]:
    """Fetch current prices for the given coin keys, tolerant of unsupported tokens.

    Requests are chunked; if a chunk fails (e.g. it contains a token coins.llama.fi cannot
    resolve, such as a Cosmos `ibc/` denom), it is bisected and retried so one bad token never sinks
    the whole fetch. Keys that fail even alone are dropped (cannot verify -> no adjustment).
    """
    out: dict[str, dict[str, Any]] = {}
    pending = [coin_keys[i : i + _CHUNK] for i in range(0, len(coin_keys), _CHUNK)]
    pending = [c for c in reversed(pending) if c]
    while pending:
        chunk = pending.pop()
        coins = _fetch_chunk(url, chunk)
        if coins is None:
            if len(chunk) == 1:
                continue  # unresolvable single token -> drop it
            mid = len(chunk) // 2
            pending.append(chunk[mid:])
            pending.append(chunk[:mid])
            continue
        for key, info in coins.items():
            if isinstance(info, dict) and isinstance(info.get("price"), (int, float)):
                out[str(key)] = info
    return out


def write_snapshot(
    root: str | Path, prices: dict[str, dict[str, Any]], *, source_url: str = PRICES_URL
) -> PricesSnapshot:
    """Write a deterministic, content-hashed snapshot of fetched prices."""
    out_dir = Path(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(prices, sort_keys=True, separators=(",", ":"))
    sha = _sha256_text(canonical)
    snap = PricesSnapshot(
        fetched_at_utc=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source_url=source_url,
        coin_count=len(prices),
        sha256=sha,
        prices=prices,
    )
    (out_dir / "prices.json").write_text(canonical + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fetched_at_utc": snap.fetched_at_utc,
                "source_url": snap.source_url,
                "coin_count": snap.coin_count,
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


def load_snapshot(root: str | Path) -> PricesSnapshot:
    """Load and verify a written price snapshot; fail closed on hash drift."""
    in_dir = Path(root)
    prices_text = (in_dir / "prices.json").read_text(encoding="utf-8").strip()
    manifest = json.loads((in_dir / "manifest.json").read_text(encoding="utf-8"))
    if _sha256_text(prices_text) != manifest["sha256"]:
        raise ValueError("prices snapshot sha256 drift; refusing to use a tampered snapshot")
    prices = json.loads(prices_text)
    return PricesSnapshot(
        fetched_at_utc=str(manifest["fetched_at_utc"]),
        source_url=str(manifest["source_url"]),
        coin_count=int(manifest["coin_count"]),
        sha256=str(manifest["sha256"]),
        prices=prices,
    )
