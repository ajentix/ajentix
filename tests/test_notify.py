from __future__ import annotations

from ajentix_alpha.yields import notify as n
from ajentix_alpha.yields.monitor import Alert, MonitorReport


def _alert(kind: str, severity: str, pool_id: str) -> Alert:
    return Alert(
        pool_id=pool_id,
        project="demo",
        symbol="USDC",
        chain="Ethereum",
        kind=kind,
        severity=severity,
        detail=f"{kind} detail",
    )


def _report(alerts: list[Alert], *, watched: int = 5) -> MonitorReport:
    return MonitorReport(
        alerts=tuple(alerts),
        critical=sum(1 for a in alerts if a.severity == "critical"),
        warning=sum(1 for a in alerts if a.severity == "warning"),
        info=sum(1 for a in alerts if a.severity == "info"),
        watched=watched,
    )


def test_payload_structure_and_counts() -> None:
    rep = _report([_alert("POOL_GONE", "critical", "a"), _alert("TVL_DRAIN", "warning", "b")])
    p = n.alert_payload(rep, baseline="t0", current="t1")
    assert p["counts"] == {"critical": 1, "warning": 1, "info": 0, "watched": 5}
    assert isinstance(p["text"], str) and "1 critical" in p["text"]
    assert isinstance(p["alerts"], list) and len(p["alerts"]) == 2
    assert p["truncated"] is False


def test_payload_truncates_to_max_items() -> None:
    alerts = [_alert("TVL_DRAIN", "warning", f"p{i}") for i in range(15)]
    p = n.alert_payload(_report(alerts), baseline="t0", current="t1", max_items=10)
    assert isinstance(p["alerts"], list) and len(p["alerts"]) == 10
    assert p["truncated"] is True


def test_summary_text_mentions_window_and_watched() -> None:
    rep = _report([_alert("POOL_GONE", "critical", "a")], watched=7)
    text = n.summary_text(rep, baseline="2026-01-01", current="2026-01-02")
    assert "7 watched" in text
    assert "2026-01-01 -> 2026-01-02" in text


def test_try_post_never_raises_on_bad_url() -> None:
    ok, detail = n.try_post("not-a-valid-url", {"text": "x"})
    assert ok is False
    assert isinstance(detail, str) and detail
