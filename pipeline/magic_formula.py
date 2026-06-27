"""
magic_formula.py
================

Pure, side-effect-free business logic for Joel Greenblatt's Magic Formula
(from *The Little Book That Still Beats the Market*).

Nothing in this module touches the network, the filesystem, or SimFin. It
operates on plain numbers and pandas DataFrames that use a small set of tidy
column names (see TIDY COLUMN CONTRACT below). build_rankings.py is responsible
for turning raw SimFin data into that tidy shape and then calling the functions
here. Keeping the maths isolated makes it trivial to unit-test (see
test_magic_formula.py).

--------------------------------------------------------------------------
THE FORMULA (spec section 2)
--------------------------------------------------------------------------
Factor 1, Return on Capital (business quality):
    ROC = EBIT / (Net Working Capital + Net Fixed Assets)

    Net Working Capital (Greenblatt-adjusted)
        = (Current Assets - Cash) - (Current Liabilities - Short-Term Debt)
    Net Fixed Assets = net property, plant & equipment (after depreciation).
    Goodwill / intangibles fall out automatically: the denominator never uses
    total assets.

Factor 2, Earnings Yield (cheapness):
    EY = EBIT / Enterprise Value
    Enterprise Value = Market Cap + Total Debt - Cash
    Market Cap = latest price * shares outstanding
    Total Debt = Short-Term Debt + Long-Term Debt

Ranking:
    rank_roc : rank every eligible company by ROC, highest ROC = rank 1.
    rank_ey  : rank every eligible company by EY,  highest EY  = rank 1.
    combined_rank = rank_roc + rank_ey
    magic_rank    = ordinal position after sorting ascending by combined_rank.
    The best stocks have the best *combination* of both factors.

--------------------------------------------------------------------------
TIDY COLUMN CONTRACT
--------------------------------------------------------------------------
Functions that take a DataFrame expect these columns (all USD, one row per
company, already reduced to the latest annual statement):

    ticker, name, sector, industry   (str)
    fiscal_year                       (int)
    currency                          (str)
    price                             (float)   latest close
    shares                            (float)   shares outstanding
    ebit                              (float)   operating income
    cur_assets                        (float)   total current assets
    cur_liab                          (float)   total current liabilities
    cash                              (float)   cash & equivalents (+ ST inv.)
    st_debt                           (float)   short-term debt
    lt_debt                           (float)   long-term debt
    net_fixed_assets                  (float)   net PP&E

add_metric_columns() appends:
    market_cap, nwc, total_debt, tangible_capital, roc, enterprise_value,
    earnings_yield
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Column groups (referenced by build_rankings.py for missing-data handling)
# ---------------------------------------------------------------------------

# Absence of these makes a company impossible to evaluate reliably -> exclude.
HARD_REQUIRED_COLS: tuple[str, ...] = (
    "price",
    "shares",
    "ebit",
    "cur_assets",
    "cur_liab",
    "cash",
)

# Absence of these is treated as "the company has none of it":
#   - no reported debt  -> debt-free  (st_debt / lt_debt -> 0)
#   - no reported PP&E   -> asset-light (net_fixed_assets -> 0)
# The tangible-capital and EV guards below still protect against nonsense.
SOFT_ZERO_COLS: tuple[str, ...] = (
    "st_debt",
    "lt_debt",
    "net_fixed_assets",
)


# ---------------------------------------------------------------------------
# Scalar formulas (the single source of truth; documented and unit-tested)
# ---------------------------------------------------------------------------

def net_working_capital(
    current_assets: float,
    cash: float,
    current_liabilities: float,
    short_term_debt: float,
) -> float:
    """Greenblatt-adjusted net working capital.

    (Current Assets - Cash) - (Current Liabilities - Short-Term Debt)
    """
    return (current_assets - cash) - (current_liabilities - short_term_debt)


def tangible_capital(nwc: float, net_fixed_assets: float) -> float:
    """Denominator of ROC: net working capital + net fixed assets."""
    return nwc + net_fixed_assets


def return_on_capital(ebit: float, capital: float) -> Optional[float]:
    """EBIT / tangible capital. None when capital <= 0 (cannot rank)."""
    if capital is None or capital <= 0 or pd.isna(capital):
        return None
    return ebit / capital


def total_debt(short_term_debt: float, long_term_debt: float) -> float:
    """Short-term debt + long-term debt."""
    return short_term_debt + long_term_debt


def enterprise_value(market_cap: float, debt: float, cash: float) -> float:
    """Market Cap + Total Debt - Cash."""
    return market_cap + debt - cash


def earnings_yield(ebit: float, ev: float) -> Optional[float]:
    """EBIT / Enterprise Value. None when EV <= 0 (invalid yield)."""
    if ev is None or ev <= 0 or pd.isna(ev):
        return None
    return ebit / ev


# ---------------------------------------------------------------------------
# Vectorised equivalents (used by the pipeline; mirror the scalars exactly)
# ---------------------------------------------------------------------------

def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Element-wise division that yields NaN wherever denominator <= 0,
    without emitting divide-by-zero warnings."""
    result = pd.Series(np.nan, index=numerator.index, dtype="float64")
    mask = denominator > 0
    result[mask] = numerator[mask] / denominator[mask]
    return result


def add_metric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Append all derived Magic Formula columns to a tidy DataFrame.

    Returns a new DataFrame (the input is not mutated). roc / earnings_yield
    are NaN where their denominators are non-positive; the eligibility filters
    drop those rows.
    """
    out = df.copy()

    out["market_cap"] = out["price"] * out["shares"]
    out["nwc"] = (out["cur_assets"] - out["cash"]) - (
        out["cur_liab"] - out["st_debt"]
    )
    out["total_debt"] = out["st_debt"] + out["lt_debt"]
    out["tangible_capital"] = out["nwc"] + out["net_fixed_assets"]
    out["enterprise_value"] = out["market_cap"] + out["total_debt"] - out["cash"]

    out["roc"] = _safe_div(out["ebit"], out["tangible_capital"])
    out["earnings_yield"] = _safe_div(out["ebit"], out["enterprise_value"])
    return out


# ---------------------------------------------------------------------------
# Eligibility filters (spec section 4)
# ---------------------------------------------------------------------------

def drop_missing_required(
    df: pd.DataFrame,
    required_cols: Sequence[str] = HARD_REQUIRED_COLS,
) -> tuple[pd.DataFrame, int]:
    """Drop rows missing any hard-required field. Returns (kept, n_dropped)."""
    before = len(df)
    kept = df.dropna(subset=list(required_cols))
    return kept, before - len(kept)


def sector_excluded(sector: object, patterns: Sequence[str]) -> bool:
    """True if `sector` matches any pattern (case-insensitive substring).

    A NaN/blank sector is NOT excluded on this rule alone (it simply is not a
    known financial/utility); other filters still apply.
    """
    if sector is None or (isinstance(sector, float) and pd.isna(sector)):
        return False
    s = str(sector).casefold()
    return any(p.casefold() in s for p in patterns if p)


def apply_filters(
    df: pd.DataFrame,
    min_market_cap: float,
    excluded_sector_patterns: Sequence[str],
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Apply spec section-4 eligibility filters to a DataFrame that already has
    metric columns (from add_metric_columns).

    Returns (eligible_df, exclusion_counts). Counts are evaluated independently
    per rule for diagnostics, so they may overlap and need not sum to the drop.
    The eligible_df has all rules applied together.
    """
    counts = {
        "sector_excluded": int(
            df["sector"].apply(
                lambda s: sector_excluded(s, excluded_sector_patterns)
            ).sum()
        ),
        "below_min_market_cap": int((df["market_cap"] < min_market_cap).sum()),
        "ebit_not_positive": int((df["ebit"] <= 0).sum()),
        "tangible_capital_not_positive": int(
            (df["tangible_capital"] <= 0).sum()
        ),
        "enterprise_value_not_positive": int(
            (df["enterprise_value"] <= 0).sum()
        ),
    }

    keep = (
        ~df["sector"].apply(
            lambda s: sector_excluded(s, excluded_sector_patterns)
        )
        & (df["market_cap"] >= min_market_cap)
        & (df["ebit"] > 0)
        & (df["tangible_capital"] > 0)
        & (df["enterprise_value"] > 0)
        & df["roc"].notna()
        & df["earnings_yield"].notna()
    )
    eligible = df[keep].copy()
    counts["eligible"] = len(eligible)
    return eligible, counts


# ---------------------------------------------------------------------------
# Ranking (spec section 2, "the ranking algorithm")
# ---------------------------------------------------------------------------

def rank_and_combine(df: pd.DataFrame) -> pd.DataFrame:
    """Rank an eligible DataFrame by ROC and EY, combine, and assign magic_rank.

    Adds integer columns rank_roc, rank_ey, combined_rank, magic_rank and
    returns the rows sorted ascending by magic_rank with a fresh 0..N-1 index.

    Highest ROC -> rank_roc 1; highest EY -> rank_ey 1 (ties share the better
    rank, "min" method). magic_rank breaks ties on combined_rank by the
    better ROC rank, then by ticker, so ordering is fully deterministic.
    """
    out = df.copy()
    out["rank_roc"] = out["roc"].rank(ascending=False, method="min").astype(int)
    out["rank_ey"] = (
        out["earnings_yield"].rank(ascending=False, method="min").astype(int)
    )
    out["combined_rank"] = out["rank_roc"] + out["rank_ey"]

    out = out.sort_values(
        ["combined_rank", "rank_roc", "ticker"], kind="mergesort"
    ).reset_index(drop=True)
    out["magic_rank"] = np.arange(1, len(out) + 1)
    return out
