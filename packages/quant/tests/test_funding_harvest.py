from ajentix_quant.strategies.funding_harvest import FundingHarvest


def test_enters_when_funding_above_threshold():
    s = FundingHarvest(min_funding_rate_8h=0.0001)
    sig = s.signal(symbol="BTC/USDT:USDT", funding_rate_8h=0.0002)
    assert sig.enter is True
    assert sig.target_delta == 0.0  # market-neutral


def test_flat_when_funding_below_threshold():
    s = FundingHarvest(min_funding_rate_8h=0.0001)
    sig = s.signal(symbol="BTC/USDT:USDT", funding_rate_8h=0.00005)
    assert sig.enter is False


def test_flat_on_negative_funding():
    s = FundingHarvest(min_funding_rate_8h=0.0001)
    sig = s.signal(symbol="BTC/USDT:USDT", funding_rate_8h=-0.0002)
    assert sig.enter is False
