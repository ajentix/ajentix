"""Read-only Deribit history option-trade provider.

The provider talks only to Deribit's public historical trade endpoint, keeps HTTP
imports inside request methods, and accepts an injected fake client for network-free
tests. It intentionally exposes no order, account, private, deposit, or withdrawal
surface.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_HISTORY_ENDPOINT = (
    "https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time"
)
_DEFAULT_COUNT = 1_000
_DEFAULT_CHUNK_MS = 60 * 60 * 1_000


class DeribitHistoryPaginationError(RuntimeError):
    """Raised when Deribit history pagination cannot advance without skipping data."""


class DeribitHistoryTradeProvider:
    """Read-only public Deribit history provider for option trades."""

    name = "deribit_history"

    def __init__(
        self,
        *,
        client: object | None = None,
        endpoint: str = _HISTORY_ENDPOINT,
        rate_limit_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        sorting: str = "asc",
    ) -> None:
        if sorting not in {"asc", "desc", "default"}:
            raise ValueError("sorting must be one of: asc, desc, default")
        self._client = client
        self._endpoint = endpoint
        self._rate_limit_s = float(rate_limit_s)
        self._sleep = sleep
        self._sorting = sorting

    @property
    def endpoint(self) -> str:
        """The public history endpoint used for manual collection."""

        return self._endpoint

    def fetch_option_trades(
        self,
        *,
        currency: str,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
        count: int = _DEFAULT_COUNT,
        chunk_ms: int = _DEFAULT_CHUNK_MS,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch public option trades over ``[start_timestamp_ms, end_timestamp_ms]``.

        Pagination is time-window based and deterministic. The request asks Deribit
        for ascending sorting; full pages overlap the boundary millisecond (advancing
        the cursor to ``max(timestamp)`` and de-duplicating by ``trade_id``) so trades
        sharing the boundary timestamp are never skipped. When a full page collapses
        onto a single millisecond and cannot be disambiguated, the method raises
        ``DeribitHistoryPaginationError`` instead of silently dropping data.
        """

        start_timestamp_ms = _int_ms(start_timestamp_ms, "start_timestamp_ms")
        end_timestamp_ms = _int_ms(end_timestamp_ms, "end_timestamp_ms")
        if start_timestamp_ms > end_timestamp_ms:
            raise ValueError("start_timestamp_ms must be <= end_timestamp_ms")
        if not 1 <= int(count) <= _DEFAULT_COUNT:
            raise ValueError(f"count must be between 1 and {_DEFAULT_COUNT}")
        if int(chunk_ms) <= 0:
            raise ValueError("chunk_ms must be positive")

        rows_by_id: dict[str, dict[str, Any]] = {}
        chunk_start = start_timestamp_ms
        while chunk_start <= end_timestamp_ms:
            chunk_end = min(end_timestamp_ms, chunk_start + int(chunk_ms) - 1)
            for row in self._fetch_window(
                currency=currency,
                start_timestamp_ms=chunk_start,
                end_timestamp_ms=chunk_end,
                count=int(count),
            ):
                key = str(row.get("trade_id") or "")
                if not key:
                    raise ValueError(f"Deribit trade row missing trade_id: {row!r}")
                previous = rows_by_id.get(key)
                if previous is not None and _canonical_row(previous) != _canonical_row(row):
                    raise ValueError(f"conflicting duplicate Deribit trade_id {key!r}")
                rows_by_id[key] = dict(row)
            chunk_start = chunk_end + 1

        return tuple(sorted(rows_by_id.values(), key=_trade_sort_key))

    def _fetch_window(
        self,
        *,
        currency: str,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
        count: int,
    ) -> tuple[dict[str, Any], ...]:
        cursor = start_timestamp_ms
        rows_by_id: dict[str, dict[str, Any]] = {}
        while cursor <= end_timestamp_ms:
            params: dict[str, Any] = {
                "currency": currency.upper(),
                "kind": "option",
                "start_timestamp": cursor,
                "end_timestamp": end_timestamp_ms,
                "count": count,
                "sorting": self._sorting,
            }
            response = self._request_json(params)
            trades = _extract_trades(response)
            if not trades:
                break
            timestamps = [_timestamp_ms(row) for row in trades]
            min_ts = min(timestamps)
            max_ts = max(timestamps)
            if min_ts < cursor or max_ts > end_timestamp_ms:
                raise ValueError(
                    "Deribit history page returned rows outside requested pagination bounds"
                )
            new_rows = 0
            for row in trades:
                key = str(row.get("trade_id") or "")
                if not key:
                    raise ValueError(f"Deribit trade row missing trade_id: {row!r}")
                previous = rows_by_id.get(key)
                if previous is not None:
                    if _canonical_row(previous) != _canonical_row(row):
                        raise ValueError(f"conflicting duplicate Deribit trade_id {key!r}")
                    continue
                rows_by_id[key] = dict(row)
                new_rows += 1

            result = _result(response)
            if "has_more" in result:
                may_have_more = bool(result["has_more"])
            else:
                may_have_more = len(trades) >= count
            if not may_have_more:
                break
            if max_ts >= end_timestamp_ms:
                raise DeribitHistoryPaginationError(
                    "Deribit history page is full at the window end; pagination "
                    "cannot advance without skipping same-timestamp trades"
                )
            if min_ts == max_ts:
                raise DeribitHistoryPaginationError(
                    "Deribit history page is full at a single timestamp; time-window "
                    "pagination cannot advance without skipping trades"
                )
            if new_rows == 0:
                raise DeribitHistoryPaginationError(
                    "Deribit history pagination stalled at the boundary timestamp"
                )
            # Overlap the boundary millisecond (advance to max_ts, not max_ts + 1) and
            # dedupe by trade_id so same-timestamp trades straddling the page boundary
            # are never silently skipped.
            cursor = max_ts
        return tuple(sorted(rows_by_id.values(), key=_trade_sort_key))

    def _request_json(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._client is not None:
            response = _call_injected_client(self._client, dict(params))
        else:
            response = self._urllib_json(dict(params))
        if self._rate_limit_s > 0.0:
            self._sleep(self._rate_limit_s)
        if not isinstance(response, Mapping):
            raise ValueError("Deribit history response must be a mapping")
        return response

    def _urllib_json(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        url = f"{self._endpoint}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "ajentix-quant-vrp-free-history/1"})
        with urlopen(request, timeout=30) as response:  # noqa: S310 - public market data
            payload = response.read().decode("utf-8")
        parsed = json.loads(payload)
        if not isinstance(parsed, Mapping):
            raise ValueError("Deribit history JSON payload must be an object")
        return parsed


# Backward-compatible descriptive alias for callers that expect an adapter noun.
DeribitHistoryAdapter = DeribitHistoryTradeProvider


def _call_injected_client(client: object, params: dict[str, Any]) -> object:
    method = getattr(client, "get_last_trades_by_currency_and_time", None)
    if callable(method):
        return method(params)
    ccxt_method = getattr(client, "public_get_get_last_trades_by_currency_and_time", None)
    if callable(ccxt_method):
        return ccxt_method(params)
    if callable(client):
        return client(params)
    raise TypeError(
        "Deribit history client must expose get_last_trades_by_currency_and_time, "
        "public_get_get_last_trades_by_currency_and_time, or be callable"
    )


def _result(response: Mapping[str, Any]) -> Mapping[str, Any]:
    result = response.get("result", response)
    if not isinstance(result, Mapping):
        raise ValueError("Deribit history response result must be a mapping")
    return result


def _extract_trades(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    trades = _result(response).get("trades", [])
    if not isinstance(trades, Sequence) or isinstance(trades, (str, bytes)):
        raise ValueError("Deribit history result.trades must be a sequence")
    out: list[Mapping[str, Any]] = []
    for row in trades:
        if not isinstance(row, Mapping):
            raise ValueError(f"Deribit history trade row must be a mapping: {row!r}")
        out.append(row)
    return tuple(out)


def _int_ms(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be an integer millisecond timestamp")
    out = int(value)
    if out < 0:
        raise ValueError(f"{label} must be non-negative")
    return out


def _timestamp_ms(row: Mapping[str, Any]) -> int:
    return _int_ms(row.get("timestamp"), "trade timestamp")


def _trade_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
    trade_seq = int(row.get("trade_seq", -1)) if not isinstance(row.get("trade_seq"), bool) else -1
    return (
        _timestamp_ms(row),
        trade_seq,
        str(row.get("instrument_name", "")),
        str(row.get("trade_id", "")),
    )


def _canonical_row(row: Mapping[str, Any]) -> str:
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":"))


__all__ = [
    "DeribitHistoryAdapter",
    "DeribitHistoryPaginationError",
    "DeribitHistoryTradeProvider",
]
