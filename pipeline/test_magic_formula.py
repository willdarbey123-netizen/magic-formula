"""
test_magic_formula.py
=====================

Tests for the Magic Formula pipeline. Two layers:

  1. Unit tests for the pure functions in magic_formula.py (exact hand-computed
     values, filter behaviour, deterministic ranking).
  2. A synthetic end-to-end test that feeds SimFin-shaped DataFrames through
     build_rankings.build_from_frames and checks the JSON contract and the
     maths -- with no API key and no network, so it runs anywhere.

Run directly (no pytest needed):
    python test_magic_formula.py
Or with pytest if available:
    pytest test_magic_formula.py
"""

from __future__ import annotations

import math

import pandas as pd

import magic_formula as mf
import build_rankings as br


TOL = 1e-9


# ---------------------------------------------------------------------------
# 1. Scalar formulas  (worked example, computed by hand)
# ---------------------------------------------------------------------------
# Company "A":
#   price 100, shares 10            -> market_cap 1000
#   ebit 200
#   cur_assets 500, cash 100, cur_liab 200, st_debt 50
#       nwc = (500-100) - (200-50) = 250
#   net_fixed_assets 150            -> tangible_capital = 400
#       roc = 200/400 = 0.5
#   lt_debt 100                     -> total_debt = 150
#       ev = 1000 + 150 - 100 = 1050
#       earnings_yield = 200/1050 = 0.19047619...

def test_net_working_capital():
    assert mf.net_working_capital(500, 100, 200, 50) == 250


def test_tangible_capital():
    assert mf.tangible_capital(250, 150) == 400


def test_return_on_capital():
    assert abs(mf.return_on_capital(200, 400) - 0.5) < TOL


def test_return_on_capital_guards_nonpositive_capital():
    assert mf.return_on_capital(200, 0) is None
    assert mf.return_on_capital(200, -10) is None


def test_total_debt():
    assert mf.total_debt(50, 100) == 150


def test_enterprise_value():
    assert mf.enterprise_value(1000, 150, 100) == 1050


def test_earnings_yield():
    assert abs(mf.earnings_yield(200, 1050) - 200 / 1050) < TOL


def test_earnings_yield_guards_nonpositive_ev():
    assert mf.earnings_yield(200, 0) is None
    assert mf.earnings_yield(200, -5) is None


# ---------------------------------------------------------------------------
# 2. Vectorised metrics agree with scalars
# ---------------------------------------------------------------------------

def _company_a_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            dict(
                ticker="A", name="Alpha", sector="Technology", industry="Software",
                fiscal_year=2025, currency="USD",
                price=100.0, shares=10.0, ebit=200.0,
                cur_assets=500.0, cur_liab=200.0, cash=100.0,
                st_debt=50.0, lt_debt=100.0, net_fixed_assets=150.0,
            )
        ]
    )


def test_add_metric_columns_matches_scalars():
    df = mf.add_metric_columns(_company_a_frame())
    row = df.iloc[0]
    assert row["market_cap"] == 1000
    assert row["nwc"] == 250
    assert row["total_debt"] == 150
    assert row["tangible_capital"] == 400
    assert row["enterprise_value"] == 1050
    assert abs(row["roc"] - 0.5) < TOL
    assert abs(row["earnings_yield"] - 200 / 1050) < TOL


def test_metric_nan_on_bad_denominators():
    df = _company_a_frame()
    # force tangible capital negative and EV negative
    df.loc[0, "net_fixed_assets"] = -1000.0   # tangible cap = 250-1000 < 0
    df.loc[0, "cash"] = 100000.0              # EV = 1000+150-100000 < 0
    out = mf.add_metric_columns(df)
    assert math.isnan(out.iloc[0]["roc"])
    assert math.isnan(out.iloc[0]["earnings_yield"])


# ---------------------------------------------------------------------------
# 3. Filters
# ---------------------------------------------------------------------------

def test_sector_excluded_matching():
    pats = ["Financial Services", "Utilities"]
    assert mf.sector_excluded("Financial Services", pats) is True
    assert mf.sector_excluded("utilities", pats) is True          # case-insensitive
    assert mf.sector_excluded("Regulated Utilities", pats) is True  # substring
    assert mf.sector_excluded("Technology", pats) is False
    assert mf.sector_excluded(None, pats) is False
    assert mf.sector_excluded(float("nan"), pats) is False


def test_apply_filters_drops_each_rule():
    rows = [
        dict(ticker="OK", sector="Technology", price=10, shares=1e7, ebit=5e6,
             cur_assets=1e7, cur_liab=1e6, cash=1e6, st_debt=0, lt_debt=0,
             net_fixed_assets=2e6, name="ok", industry="sw", fiscal_year=2025,
             currency="USD"),
        dict(ticker="BANK", sector="Financial Services", price=10, shares=1e7,
             ebit=5e6, cur_assets=1e7, cur_liab=1e6, cash=1e6, st_debt=0, lt_debt=0,
             net_fixed_assets=2e6, name="bank", industry="bank", fiscal_year=2025,
             currency="USD"),
        dict(ticker="TINY", sector="Technology", price=0.01, shares=1000, ebit=5e6,
             cur_assets=1e7, cur_liab=1e6, cash=1e6, st_debt=0, lt_debt=0,
             net_fixed_assets=2e6, name="tiny", industry="sw", fiscal_year=2025,
             currency="USD"),
        dict(ticker="LOSS", sector="Technology", price=10, shares=1e7, ebit=-1,
             cur_assets=1e7, cur_liab=1e6, cash=1e6, st_debt=0, lt_debt=0,
             net_fixed_assets=2e6, name="loss", industry="sw", fiscal_year=2025,
             currency="USD"),
    ]
    df = mf.add_metric_columns(pd.DataFrame(rows))
    eligible, counts = mf.apply_filters(
        df, min_market_cap=50_000_000, excluded_sector_patterns=["Financial Services", "Utilities"]
    )
    tickers = set(eligible["ticker"])
    assert tickers == {"OK"}
    assert counts["sector_excluded"] == 1
    assert counts["ebit_not_positive"] == 1
    assert counts["below_min_market_cap"] == 1


# ---------------------------------------------------------------------------
# 4. Ranking determinism
# ---------------------------------------------------------------------------
# X: roc .50 ey .10  -> rank_roc 1, rank_ey 3, combined 4
# Y: roc .40 ey .25  -> rank_roc 2, rank_ey 1, combined 3
# Z: roc .30 ey .20  -> rank_roc 3, rank_ey 2, combined 5
# expected magic order: Y(1), X(2), Z(3)

def test_rank_and_combine_order():
    df = pd.DataFrame(
        [
            dict(ticker="X", roc=0.50, earnings_yield=0.10),
            dict(ticker="Y", roc=0.40, earnings_yield=0.25),
            dict(ticker="Z", roc=0.30, earnings_yield=0.20),
        ]
    )
    ranked = mf.rank_and_combine(df)
    assert list(ranked["ticker"]) == ["Y", "X", "Z"]
    assert list(ranked["magic_rank"]) == [1, 2, 3]
    y = ranked[ranked["ticker"] == "Y"].iloc[0]
    assert y["rank_roc"] == 2 and y["rank_ey"] == 1 and y["combined_rank"] == 3


# ---------------------------------------------------------------------------
# 5. Synthetic end-to-end pipeline (no SimFin, no network)
# ---------------------------------------------------------------------------

def _synthetic_simfin_frames() -> dict[str, pd.DataFrame]:
    """SimFin-shaped frames using the real column names, covering eligible and
    every kind of excluded company, plus a duplicate older statement to verify
    'latest annual' selection."""
    income = pd.DataFrame(
        [
            # ticker, Report Date, Operating Income (Loss), Fiscal Year, Currency, Shares (Basic)
            ("GOODA", "2025-12-31", 200.0, 2025, "USD", 10.0),
            ("GOODA", "2024-12-31", 111.0, 2024, "USD", 10.0),   # older, must be ignored
            ("GOODB", "2025-12-31", 600.0, 2025, "USD", 50.0),
            ("GOODC", "2025-12-31", 90.0, 2025, "USD", 8.0),
            ("BANKX", "2025-12-31", 500.0, 2025, "USD", 40.0),
            ("UTILX", "2025-12-31", 500.0, 2025, "USD", 40.0),
            ("TINYX", "2025-12-31", 5.0, 2025, "USD", 1.0),
            ("LOSSX", "2025-12-31", -50.0, 2025, "USD", 10.0),
            ("NEGCAP", "2025-12-31", 80.0, 2025, "USD", 10.0),
        ],
        columns=["Ticker", "Report Date", "Operating Income (Loss)", "Fiscal Year", "Currency", "Shares (Basic)"],
    )

    balance = pd.DataFrame(
        [
            # CA, CL, Cash(+STI), ST Debt, LT Debt, Net PP&E
            ("GOODA", "2025-12-31", 500.0, 200.0, 100.0, 50.0, 100.0, 150.0),
            ("GOODB", "2025-12-31", 2000.0, 800.0, 300.0, 100.0, 400.0, 500.0),
            ("GOODC", "2025-12-31", 400.0, 150.0, 50.0, 0.0, 0.0, 60.0),
            ("BANKX", "2025-12-31", 5000.0, 4000.0, 1000.0, 200.0, 800.0, 200.0),
            ("UTILX", "2025-12-31", 5000.0, 4000.0, 1000.0, 200.0, 800.0, 200.0),
            ("TINYX", "2025-12-31", 100.0, 40.0, 10.0, 0.0, 0.0, 20.0),
            ("LOSSX", "2025-12-31", 800.0, 300.0, 100.0, 0.0, 0.0, 120.0),
            # NEGCAP: huge current liabilities -> tangible capital negative
            ("NEGCAP", "2025-12-31", 100.0, 100000.0, 50.0, 0.0, 0.0, 10.0),
        ],
        columns=[
            "Ticker", "Report Date", "Total Current Assets", "Total Current Liabilities",
            "Cash, Cash Equivalents & Short Term Investments", "Short Term Debt",
            "Long Term Debt", "Property, Plant & Equipment, Net",
        ],
    )

    prices = pd.DataFrame(
        [
            # Close, Shares Outstanding   (TINYX priced so market cap < 50M)
            ("GOODA", "2026-06-26", 100.0, 1_000_000.0),
            ("GOODB", "2026-06-26", 80.0, 5_000_000.0),
            ("GOODC", "2026-06-26", 60.0, 2_000_000.0),
            ("BANKX", "2026-06-26", 90.0, 4_000_000.0),
            ("UTILX", "2026-06-26", 90.0, 4_000_000.0),
            ("TINYX", "2026-06-26", 0.10, 1000.0),       # market cap = 100 USD
            ("LOSSX", "2026-06-26", 70.0, 3_000_000.0),
            ("NEGCAP", "2026-06-26", 40.0, 3_000_000.0),
        ],
        columns=["Ticker", "Date", "Close", "Shares Outstanding"],
    )

    companies = pd.DataFrame(
        [
            ("GOODA", "Alpha Corp", 101),
            ("GOODB", "Bravo Inc", 101),
            ("GOODC", "Charlie Ltd", 102),
            ("BANKX", "Big Bank", 201),
            ("UTILX", "Power Co", 301),
            ("TINYX", "Tiny Co", 101),
            ("LOSSX", "Loss Co", 101),
            ("NEGCAP", "NegCap Co", 101),
        ],
        columns=["Ticker", "Company Name", "IndustryId"],
    )

    industries = pd.DataFrame(
        [
            (101, "Technology", "Software"),
            (102, "Technology", "Hardware"),
            (201, "Financial Services", "Banks"),
            (301, "Utilities", "Electric Utilities"),
        ],
        columns=["IndustryId", "Sector", "Industry"],
    )

    return dict(income=income, balance=balance, prices=prices,
                companies=companies, industries=industries)


def test_end_to_end_contract_and_maths():
    frames = _synthetic_simfin_frames()

    cfg = br.Config()
    cfg.min_market_cap = 50_000_000
    cfg.excluded_sectors = ["Financial Services", "Utilities"]

    rankings, prices, diag = br.build_from_frames(frames, cfg, as_of="2026-06-27")

    # --- top-level contract keys ---
    for key in ("as_of", "universe_size", "eligible_count", "params", "stocks"):
        assert key in rankings, f"missing top-level key {key}"
    assert rankings["as_of"] == "2026-06-27"
    assert rankings["params"]["min_market_cap_usd"] == 50_000_000
    assert rankings["params"]["excluded_sectors"] == ["Financial Services", "Utilities"]

    # 8 unique tickers assembled (the older GOODA statement is collapsed)
    assert rankings["universe_size"] == 8
    assert diag["dropped_missing_required"] == 0

    tickers = {s["ticker"] for s in rankings["stocks"]}
    # eligible = the three GOOD names only
    assert tickers == {"GOODA", "GOODB", "GOODC"}, tickers
    assert rankings["eligible_count"] == 3

    # excluded for the right reasons
    assert diag["sector_excluded"] == 2          # BANKX + UTILX
    assert diag["below_min_market_cap"] >= 1     # TINYX
    assert diag["ebit_not_positive"] == 1        # LOSSX
    assert diag["tangible_capital_not_positive"] == 1  # NEGCAP

    # --- per-stock contract keys ---
    needed = {
        "ticker", "name", "sector", "industry", "magic_rank", "roc",
        "earnings_yield", "rank_roc", "rank_ey", "combined_rank", "price",
        "currency", "market_cap", "ebit", "enterprise_value", "fiscal_year",
    }
    for s in rankings["stocks"]:
        assert needed.issubset(s.keys())

    # --- maths for GOODA (latest statement, NOT the 2024 one) ---
    a = next(s for s in rankings["stocks"] if s["ticker"] == "GOODA")
    assert a["fiscal_year"] == 2025
    assert a["ebit"] == 200
    # market cap = 100 * 1,000,000 = 100,000,000
    assert a["market_cap"] == 100_000_000
    # nwc = (500-100)-(200-50)=250 ; tangible cap = 250+150 = 400 ; roc = 200/400
    assert abs(a["roc"] - 0.5) < 1e-6
    # ev = 100,000,000 + 150 - 100 = 100,000,050 ; ey = 200 / 100,000,050 (~tiny)
    assert a["enterprise_value"] == 100_000_050
    assert abs(a["earnings_yield"] - 200 / 100_000_050) < 1e-12

    # --- magic_rank contiguous 1..N ---
    ranks = sorted(s["magic_rank"] for s in rankings["stocks"])
    assert ranks == list(range(1, len(ranks) + 1))

    # --- prices.json ---
    assert prices["as_of"] == "2026-06-27"
    assert set(prices["prices"].keys()) == {"GOODA", "GOODB", "GOODC"}
    assert prices["prices"]["GOODA"] == 100.0


def test_soft_field_absence_is_zero():
    """A company with no debt / no PP&E columns at all should still be evaluated
    (debt and PP&E coerced to 0), not dropped."""
    frames = _synthetic_simfin_frames()
    # drop the debt + PP&E columns entirely from the balance sheet
    frames["balance"] = frames["balance"].drop(
        columns=["Short Term Debt", "Long Term Debt", "Property, Plant & Equipment, Net"]
    )
    cfg = br.Config()
    cfg.min_market_cap = 50_000_000
    rankings, _, _ = br.build_from_frames(frames, cfg, as_of="2026-06-27")
    a = next(s for s in rankings["stocks"] if s["ticker"] == "GOODA")
    # st_debt now 0, so nwc = (500-100)-(200-0) = 200 ; net_fixed_assets 0 ->
    # tangible cap = 200 ; roc = 200/200 = 1.0
    assert abs(a["roc"] - 1.0) < 1e-6
    # with debt=0: ev = market_cap - cash = 100,000,000 - 100 = 99,999,900
    assert a["enterprise_value"] == 99_999_900


# ---------------------------------------------------------------------------
# Self-runner (so the suite works without pytest installed)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
