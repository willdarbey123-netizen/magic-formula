"""
build_rankings.py
=================

Entry point for the Magic Formula data pipeline (spec Part A, Phases 1-2).

Flow:
    load SimFin bulk US data
        -> reduce to latest annual statement per company
        -> assemble one tidy row per company (USD)
        -> compute ROC / EY / EV / market cap   (magic_formula.add_metric_columns)
        -> apply eligibility filters             (magic_formula.apply_filters)
        -> rank and combine                      (magic_formula.rank_and_combine)
        -> write data/rankings.json + data/prices.json   (spec section 5)

The pure maths lives in magic_formula.py. This file owns everything with side
effects: SimFin/network, column resolution, and file writing. The heavy logic
sits in build_from_frames(), which takes five DataFrames so it can be exercised
in tests with synthetic data (no API key, no network) -- see test_magic_formula.py.

Configuration (all via environment variables, with sensible defaults):
    SIMFIN_API_KEY        SimFin API key. Required for a real run. Falls back to
                          'free' (SimFin's shared key) with a warning.
    MIN_MARKET_CAP_USD    Eligibility floor. Default 50000000 (the book's floor).
    EXCLUDED_SECTORS      Comma-separated sector patterns (case-insensitive
                          substring match). Default "Financial Services,Utilities".
    SIMFIN_DATA_DIR       Local SimFin cache dir. Default ~/simfin_data/.
    SIMFIN_MARKET         Default "us".
    SIMFIN_VARIANT        Statement variant. Default "annual".
    OUTPUT_DIR            Where to write JSON. Default <repo_root>/data.
    TOP_N_LOG             How many ranked names to print to the console. Default 25.

Never hardcode the API key. On GitHub Actions it is injected from the
SIMFIN_API_KEY secret (see .github/workflows/refresh.yml).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

import magic_formula as mf


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------
# The spec flags SimFin column names as the single most likely thing to need a
# fix between package versions. Rather than depend on one exact string, each
# tidy field maps to an ordered list of candidate SimFin column names; the first
# one present in the DataFrame wins. Verified against simfin 1.0.2 / simfin.names
# at build time, with sensible fallbacks ahead and behind the verified name.

CANDIDATES: dict[str, list[str]] = {
    # income statement
    "ebit": ["Operating Income (Loss)", "Operating Income"],
    "shares_basic": ["Shares (Basic)", "Shares (Diluted)"],
    "fiscal_year": ["Fiscal Year"],
    "currency": ["Currency"],
    # balance sheet
    "cur_assets": ["Total Current Assets"],
    "cur_liab": ["Total Current Liabilities"],
    "cash": [
        "Cash, Cash Equivalents & Short Term Investments",
        "Cash & Cash Equivalents",
    ],
    "st_debt": ["Short Term Debt", "Current Portion of Long Term Debt"],
    "lt_debt": ["Long Term Debt"],
    "net_fixed_assets": [
        "Property, Plant & Equipment, Net",
        "Net Fixed Assets",
    ],
    # share prices (latest)
    "price": ["Close", "Adj. Close"],
    "shares_outstanding": ["Shares Outstanding"],
    # companies / industries
    "name": ["Company Name"],
    "industry_id": ["IndustryId"],
    "sector": ["Sector"],
    "industry": ["Industry"],
}


def resolve(df: pd.DataFrame, field: str) -> Optional[str]:
    """Return the first candidate column for `field` that exists in df, else None."""
    for col in CANDIDATES.get(field, []):
        if col in df.columns:
            return col
    return None


def _require(df: pd.DataFrame, field: str, where: str) -> str:
    col = resolve(df, field)
    if col is None:
        raise KeyError(
            f"Could not find a column for '{field}' in the {where} dataset. "
            f"Tried {CANDIDATES.get(field)}. Available columns: "
            f"{sorted(df.columns)}"
        )
    return col


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.api_key: str = os.environ.get("SIMFIN_API_KEY", "").strip() or "free"
        self.min_market_cap: float = float(
            os.environ.get("MIN_MARKET_CAP_USD", 50_000_000)
        )
        raw_sectors = os.environ.get(
            "EXCLUDED_SECTORS", "Financial Services,Utilities"
        )
        self.excluded_sectors: list[str] = [
            s.strip() for s in raw_sectors.split(",") if s.strip()
        ]
        self.data_dir: str = os.environ.get("SIMFIN_DATA_DIR", "~/simfin_data/")
        self.market: str = os.environ.get("SIMFIN_MARKET", "us")
        self.variant: str = os.environ.get("SIMFIN_VARIANT", "annual")
        self.top_n_log: int = int(os.environ.get("TOP_N_LOG", 25))

        repo_root = Path(__file__).resolve().parent.parent
        self.output_dir: Path = Path(
            os.environ.get("OUTPUT_DIR", str(repo_root / "data"))
        )


# ---------------------------------------------------------------------------
# SimFin loading
# ---------------------------------------------------------------------------

def load_simfin_frames(cfg: Config) -> dict[str, pd.DataFrame]:
    """Load the five bulk SimFin datasets as plain (reset-index) DataFrames."""
    import simfin as sf  # imported here so tests need not install/connect

    sf.set_api_key(cfg.api_key)
    sf.set_data_dir(os.path.expanduser(cfg.data_dir))

    print(f"Loading SimFin bulk data (market={cfg.market}, variant={cfg.variant})...")
    income = sf.load_income(variant=cfg.variant, market=cfg.market).reset_index()
    balance = sf.load_balance(variant=cfg.variant, market=cfg.market).reset_index()
    prices = sf.load_shareprices(variant="latest", market=cfg.market).reset_index()
    companies = sf.load_companies(market=cfg.market).reset_index()
    industries = sf.load_industries().reset_index()

    print(
        "  rows -> income: {:,}  balance: {:,}  prices: {:,}  "
        "companies: {:,}  industries: {:,}".format(
            len(income), len(balance), len(prices), len(companies), len(industries)
        )
    )
    return {
        "income": income,
        "balance": balance,
        "prices": prices,
        "companies": companies,
        "industries": industries,
    }


# ---------------------------------------------------------------------------
# Assembly: raw SimFin frames -> one tidy row per company
# ---------------------------------------------------------------------------

def _one_row_per_ticker(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse to a single row per ticker, keeping the latest dated row when a
    date column is present. Statements date rows under 'Report Date', share
    prices under 'Date'; metadata tables (e.g. companies) are undated and can
    contain duplicate tickers in the SimFin bulk file."""
    for date_col in ("Report Date", "Date"):
        if date_col in df.columns:
            df = df.sort_values(date_col)
            break
    return df.drop_duplicates(subset="Ticker", keep="last")


def _col(df: pd.DataFrame, field: str) -> "pd.array":
    """Positional values for a resolved field, decoupled from the source index."""
    return df[_require(df, field, "source")].to_numpy()


def assemble_tidy(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Join the SimFin datasets into the tidy one-row-per-company contract that
    magic_formula.py expects.

    Every source is first collapsed to one row per ticker, then the pieces are
    combined with explicit merges on the ticker column (never index alignment),
    so duplicate tickers in any single source cannot break the assembly. Soft
    fields are coerced (NaN -> 0); hard-required fields are left as NaN for
    drop_missing_required to handle. A company must have income, balance, and a
    price to be evaluable (inner joins); company metadata is optional (left join).
    """
    income = _one_row_per_ticker(frames["income"])
    balance = _one_row_per_ticker(frames["balance"])
    prices = _one_row_per_ticker(frames["prices"])
    companies = _one_row_per_ticker(frames["companies"])
    industries = frames["industries"]

    # --- sector / industry: prefer columns already on companies, else join ---
    sector_col = resolve(companies, "sector")
    industry_col = resolve(companies, "industry")
    if sector_col is None or industry_col is None:
        ind_id_c = _require(companies, "industry_id", "companies")
        ind_id_i = _require(industries, "industry_id", "industries")
        keep_cols = [ind_id_i]
        for f in ("sector", "industry"):
            col = resolve(industries, f)
            if col and col not in keep_cols:
                keep_cols.append(col)
        industries_dedup = industries.drop_duplicates(subset=ind_id_i, keep="last")
        companies = companies.merge(
            industries_dedup[keep_cols],
            left_on=ind_id_c, right_on=ind_id_i, how="left",
        )
        companies = companies.drop_duplicates(subset="Ticker", keep="last")
        sector_col = resolve(companies, "sector")
        industry_col = resolve(companies, "industry")

    name_col = resolve(companies, "name")  # display-only: fall back to ticker if absent

    # --- one tidy sub-frame per source, built positionally, merged by ticker ---
    inc = pd.DataFrame({"ticker": income["Ticker"].to_numpy()})
    inc["ebit"] = _col(income, "ebit")
    fy_col = resolve(income, "fiscal_year")
    inc["fiscal_year"] = income[fy_col].to_numpy() if fy_col else pd.NA
    cur_col = resolve(income, "currency")
    inc["currency"] = income[cur_col].to_numpy() if cur_col else "USD"
    sb_col = resolve(income, "shares_basic")
    inc["shares_basic"] = income[sb_col].to_numpy() if sb_col else pd.NA

    bal = pd.DataFrame({"ticker": balance["Ticker"].to_numpy()})
    bal["cur_assets"] = _col(balance, "cur_assets")
    bal["cur_liab"] = _col(balance, "cur_liab")
    bal["cash"] = _col(balance, "cash")
    st_c = resolve(balance, "st_debt")
    lt_c = resolve(balance, "lt_debt")
    nfa_c = resolve(balance, "net_fixed_assets")
    bal["st_debt"] = balance[st_c].to_numpy() if st_c else 0.0
    bal["lt_debt"] = balance[lt_c].to_numpy() if lt_c else 0.0
    bal["net_fixed_assets"] = balance[nfa_c].to_numpy() if nfa_c else 0.0

    pr = pd.DataFrame({"ticker": prices["Ticker"].to_numpy()})
    pr["price"] = _col(prices, "price")
    so_c = resolve(prices, "shares_outstanding")
    pr["shares_outstanding"] = prices[so_c].to_numpy() if so_c else pd.NA

    comp = pd.DataFrame({"ticker": companies["Ticker"].to_numpy()})
    comp["name"] = companies[name_col].to_numpy() if name_col else comp["ticker"]
    comp["sector"] = companies[sector_col].to_numpy() if sector_col else pd.NA
    comp["industry"] = companies[industry_col].to_numpy() if industry_col else pd.NA

    tidy = inc.merge(bal, on="ticker", how="inner")
    tidy = tidy.merge(pr, on="ticker", how="inner")
    tidy = tidy.merge(comp, on="ticker", how="left")

    # shares: prefer price-file shares outstanding, fall back to income basic
    tidy["shares"] = pd.to_numeric(
        tidy["shares_outstanding"], errors="coerce"
    ).fillna(pd.to_numeric(tidy["shares_basic"], errors="coerce"))

    # coerce soft fields (missing debt / PP&E => none of it)
    for col in mf.SOFT_ZERO_COLS:
        tidy[col] = pd.to_numeric(tidy[col], errors="coerce").fillna(0.0)

    # numeric coercion for hard-required fields + fiscal year
    for col in (
        "ebit", "cur_assets", "cur_liab", "cash", "price", "shares", "fiscal_year",
    ):
        tidy[col] = pd.to_numeric(tidy[col], errors="coerce")

    cols = [
        "ticker", "name", "sector", "industry", "fiscal_year", "currency",
        "price", "shares", "ebit", "cur_assets", "cur_liab", "cash",
        "st_debt", "lt_debt", "net_fixed_assets",
    ]
    return tidy[cols]


# ---------------------------------------------------------------------------
# JSON construction (spec section 5)
# ---------------------------------------------------------------------------

def _round(x: object, ndigits: int) -> Optional[float]:
    if x is None or pd.isna(x):
        return None
    return round(float(x), ndigits)


def build_rankings_payload(
    ranked: pd.DataFrame, cfg: Config, universe_size: int, as_of: str
) -> dict:
    stocks = []
    for _, r in ranked.iterrows():
        fy = r["fiscal_year"]
        stocks.append(
            {
                "ticker": r["ticker"],
                "name": None if pd.isna(r["name"]) else str(r["name"]),
                "sector": None if pd.isna(r["sector"]) else str(r["sector"]),
                "industry": None if pd.isna(r["industry"]) else str(r["industry"]),
                "magic_rank": int(r["magic_rank"]),
                "roc": _round(r["roc"], 6),
                "earnings_yield": _round(r["earnings_yield"], 6),
                "rank_roc": int(r["rank_roc"]),
                "rank_ey": int(r["rank_ey"]),
                "combined_rank": int(r["combined_rank"]),
                "price": _round(r["price"], 4),
                "currency": "USD" if pd.isna(r["currency"]) else str(r["currency"]),
                "market_cap": _round(r["market_cap"], 0),
                "ebit": _round(r["ebit"], 0),
                "enterprise_value": _round(r["enterprise_value"], 0),
                "fiscal_year": None if pd.isna(fy) else int(fy),
            }
        )
    return {
        "as_of": as_of,
        "universe_size": int(universe_size),
        "eligible_count": int(len(ranked)),
        "params": {
            "min_market_cap_usd": int(cfg.min_market_cap),
            "excluded_sectors": cfg.excluded_sectors,
        },
        "stocks": stocks,
    }


def build_prices_payload(ranked: pd.DataFrame, as_of: str) -> dict:
    prices = {
        str(r["ticker"]): _round(r["price"], 4)
        for _, r in ranked.iterrows()
        if not pd.isna(r["price"])
    }
    return {"as_of": as_of, "prices": prices}


# ---------------------------------------------------------------------------
# Frame-based core (testable: takes DataFrames, returns the two payloads)
# ---------------------------------------------------------------------------

def build_from_frames(
    frames: dict[str, pd.DataFrame], cfg: Config, as_of: Optional[str] = None
) -> tuple[dict, dict, dict]:
    """Run the full pipeline on already-loaded frames.

    Returns (rankings_payload, prices_payload, diagnostics).
    """
    as_of = as_of or date.today().isoformat()

    tidy = assemble_tidy(frames)
    universe_size = len(tidy)

    tidy, n_missing = mf.drop_missing_required(tidy)
    metrics = mf.add_metric_columns(tidy)
    eligible, counts = mf.apply_filters(
        metrics, cfg.min_market_cap, cfg.excluded_sectors
    )
    ranked = mf.rank_and_combine(eligible)

    diagnostics = {
        "universe_size": universe_size,
        "dropped_missing_required": n_missing,
        **counts,
    }
    rankings = build_rankings_payload(ranked, cfg, universe_size, as_of)
    prices = build_prices_payload(ranked, as_of)
    return rankings, prices, diagnostics


# ---------------------------------------------------------------------------
# Writing + console summary
# ---------------------------------------------------------------------------

def write_outputs(rankings: dict, prices: dict, cfg: Config) -> tuple[Path, Path]:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    rankings_path = cfg.output_dir / "rankings.json"
    prices_path = cfg.output_dir / "prices.json"
    rankings_path.write_text(json.dumps(rankings, indent=2))
    prices_path.write_text(json.dumps(prices, indent=2))
    return rankings_path, prices_path


def print_summary(rankings: dict, diagnostics: dict, cfg: Config) -> None:
    print("\n--- Pipeline summary ---")
    print(f"  as_of                  : {rankings['as_of']}")
    print(f"  universe (assembled)   : {diagnostics['universe_size']:,}")
    print(f"  dropped missing fields : {diagnostics['dropped_missing_required']:,}")
    print(f"  sector-excluded        : {diagnostics['sector_excluded']:,}")
    print(f"  below min market cap   : {diagnostics['below_min_market_cap']:,}")
    print(f"  EBIT <= 0              : {diagnostics['ebit_not_positive']:,}")
    print(f"  tangible cap <= 0      : {diagnostics['tangible_capital_not_positive']:,}")
    print(f"  EV <= 0                : {diagnostics['enterprise_value_not_positive']:,}")
    print(f"  ELIGIBLE (ranked)      : {rankings['eligible_count']:,}")

    n = min(cfg.top_n_log, len(rankings["stocks"]))
    if n:
        print(f"\nTop {n} by Magic Formula rank:")
        print(f"  {'#':>3}  {'TICKER':<8}{'ROC%':>8}{'EY%':>8}  NAME")
        for s in rankings["stocks"][:n]:
            roc = "" if s["roc"] is None else f"{s['roc'] * 100:7.1f}"
            ey = "" if s["earnings_yield"] is None else f"{s['earnings_yield'] * 100:7.1f}"
            print(f"  {s['magic_rank']:>3}  {s['ticker']:<8}{roc:>8}{ey:>8}  {s['name'] or ''}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = Config()
    if cfg.api_key == "free":
        print(
            "WARNING: SIMFIN_API_KEY not set; using SimFin's shared 'free' key. "
            "Register at simfin.com and set SIMFIN_API_KEY for reliable bulk access.",
            file=sys.stderr,
        )

    as_of = datetime.now(timezone.utc).date().isoformat()
    frames = load_simfin_frames(cfg)
    rankings, prices, diagnostics = build_from_frames(frames, cfg, as_of=as_of)
    rankings_path, prices_path = write_outputs(rankings, prices, cfg)

    print_summary(rankings, diagnostics, cfg)
    print(f"\nWrote {rankings_path}")
    print(f"Wrote {prices_path}")

    if rankings["eligible_count"] == 0:
        print(
            "ERROR: no eligible stocks produced. Check column resolution and "
            "filters before committing empty JSON.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
