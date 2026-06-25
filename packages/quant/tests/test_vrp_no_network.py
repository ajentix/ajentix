from __future__ import annotations

import builtins
import importlib
import socket
from pathlib import Path

from ajentix_quant.data.options_cache import load_normalized_cache
from ajentix_quant.data.options_replay import ReplayOptionChainProvider

SCENARIO = "tiny_eth_options_v1"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "deribit_options" / "normalized"


def _block_ccxt_import(monkeypatch) -> None:
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "ccxt":
            raise AssertionError("CI fixture path must not import ccxt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)


def _block_network(monkeypatch) -> None:
    def blocked_socket(*args, **kwargs):
        raise AssertionError("CI fixture path must not open network sockets")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    monkeypatch.setattr(socket, "create_connection", blocked_socket)


def test_importing_options_data_modules_performs_no_network(monkeypatch):
    _block_ccxt_import(monkeypatch)
    _block_network(monkeypatch)

    for module_name in (
        "ajentix_quant.adapters.deribit_options",
        "ajentix_quant.data.options_cache",
        "ajentix_quant.data.options_replay",
    ):
        importlib.reload(importlib.import_module(module_name))


def test_ci_style_fixture_loading_is_offline(monkeypatch):
    monkeypatch.setenv("CI", "1")
    _block_ccxt_import(monkeypatch)
    _block_network(monkeypatch)

    snapshots = load_normalized_cache(FIXTURE_ROOT, SCENARIO)
    provider = ReplayOptionChainProvider.from_cache(FIXTURE_ROOT, SCENARIO)

    assert len(snapshots) == 4
    assert provider.available_expiries("ETH") == (1719532800000, 1721952000000)
    assert provider.chain_snapshot("ETH", 1717200000000, 1719532800000).underlying == "ETH"
