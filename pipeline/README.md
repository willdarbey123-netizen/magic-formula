# Magic Formula pipeline (Part A)

Computes Joel Greenblatt's Magic Formula ranking across the SimFin US universe
and writes two JSON files that the phone app (Part B / Session B) consumes:

- `../data/rankings.json` — the full eligible list, sorted by `magic_rank`.
- `../data/prices.json` — latest close for every eligible ticker.

The maths lives in `magic_formula.py` (pure, no IO). Everything with side effects
(SimFin, files) lives in `build_rankings.py`.

## Files

| File | Purpose |
|---|---|
| `magic_formula.py` | Pure functions: ROC, earnings yield, EV, filters, `rank_and_combine`. The single source of truth for the maths. |
| `build_rankings.py` | Loads SimFin, assembles one tidy row per company, applies filters, ranks, writes JSON. Heavy logic is in `build_from_frames()` so it can be tested without a key. |
| `test_magic_formula.py` | Unit tests + a synthetic end-to-end test. No API key / network needed. |
| `requirements.txt` | `simfin`, `pandas`. |
| `EXAMPLE_rankings.json`, `EXAMPLE_prices.json` | Sample output from the synthetic test data, so you can see the exact contract shape. Not real data. |

## Run locally

```bash
cd pipeline
pip install -r requirements.txt          # add --break-system-packages on some distros
export SIMFIN_API_KEY=your_key_here      # from simfin.com account page
python build_rankings.py
```

Writes `../data/rankings.json` and `../data/prices.json`, and prints a summary
plus the top names. The first run downloads the SimFin bulk datasets into
`~/simfin_data/` (cached for subsequent runs).

## Run the tests

```bash
cd pipeline
python test_magic_formula.py     # self-contained runner, no pytest needed
# or, if you have pytest:
pytest test_magic_formula.py
```

## Configuration (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `SIMFIN_API_KEY` | `free` (with warning) | Your SimFin key. Set it for reliable bulk access. |
| `MIN_MARKET_CAP_USD` | `50000000` | Eligibility floor. `100000000` / `200000000` cut more micro-cap noise. |
| `EXCLUDED_SECTORS` | `Financial Services,Utilities` | Comma-separated, case-insensitive substring match. |
| `SIMFIN_DATA_DIR` | `~/simfin_data/` | Local cache. |
| `SIMFIN_MARKET` | `us` | |
| `SIMFIN_VARIANT` | `annual` | Guaranteed on free tier; faithful to "last year's earnings". |
| `OUTPUT_DIR` | `<repo>/data` | Where the JSON is written. |
| `TOP_N_LOG` | `25` | How many ranked names to print. |

## How a company is evaluated

1. Take each company's **latest annual** statement (income + balance) and its
   latest close price.
2. Compute, in USD:
   - `market_cap = price × shares` (shares from the price file, else basic shares)
   - `nwc = (current_assets − cash) − (current_liabilities − short_term_debt)`
   - `tangible_capital = nwc + net_fixed_assets`
   - `roc = ebit / tangible_capital`
   - `enterprise_value = market_cap + (short_term_debt + long_term_debt) − cash`
   - `earnings_yield = ebit / enterprise_value`
3. Exclude (spec section 4): Financials/Utilities sectors; market cap below the
   floor; `ebit ≤ 0`; `tangible_capital ≤ 0`; `enterprise_value ≤ 0`; missing
   required fields.
4. Rank by ROC and by EY (highest = 1), sum to `combined_rank`, sort ascending,
   assign `magic_rank`.

### Missing-data policy

- **Hard-required** (excluded if missing): price, shares, EBIT, current assets,
  current liabilities, cash.
- **Soft** (missing treated as zero): short-term debt, long-term debt, net PP&E
  — i.e. "debt-free" / "asset-light". The `tangible_capital ≤ 0` and
  `enterprise_value ≤ 0` guards still protect against nonsense.

Adjust the split via `HARD_REQUIRED_COLS` / `SOFT_ZERO_COLS` in `magic_formula.py`.

## Column resolution (the most likely thing to need a tweak)

SimFin column names can drift between package versions. `build_rankings.py` maps
each needed value to an **ordered list of candidate column names** (`CANDIDATES`)
and uses the first one present, so small naming differences self-heal. If a field
genuinely cannot be found it raises a clear error listing the available columns.
Verified against `simfin` 1.0.2 at build time.

## Validation

Spot-check a few well-known tickers by hand, then sanity-check the top names
against the free list at <https://www.magicformulainvesting.com>. Expect broad
overlap, **not** an identical list (different underlying dataset and timing).
