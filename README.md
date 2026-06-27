# Magic Formula

A personal-use system that ranks US stocks by Joel Greenblatt's **Magic Formula**
(from *The Little Book That Still Beats the Market*) and feeds a phone app that
displays the ranking, manages watchlists, and tracks a manually-entered portfolio.

The system is split in two with a stable JSON contract between them:

```
[ Python data pipeline ]  ->  data/rankings.json + data/prices.json  ->  [ Android app ]
  (this repo; weekly, off-device)         (the contract)               (Session B; consumes the URLs)
```

- **Part A — the engine (this repo, Sessions A complete):** a Python pipeline
  that pulls SimFin bulk US data, computes the ranking, and writes two JSON files.
  Runs weekly via GitHub Actions.
- **Part B — the client (Session B):** a native Android app (Kotlin / Jetpack
  Compose / Room) that fetches those JSON files. Built later, in a separate session.

This repo is **Part A only**: Phases 0, 1, and 2. The Android app is Session B.

---

## Status of Session A

| Phase | What | State |
|---|---|---|
| 0 | SimFin account, GitHub repo, Actions secret | **Your steps — checklist below** |
| 1 | The engine (`pipeline/`) | **Built + tested** (15/15 tests pass against synthetic data) |
| 2 | Automation (`.github/workflows/refresh.yml`) | **Built** — runs once you push + add the secret |

The engine's maths and the JSON contract are fully verified offline. The only
things that need your machine/account are: your SimFin key (to run against real
data) and your GitHub repo (to push and trigger the Action). Steps below.

---

## Phase 0 — foundations (your steps)

Tick these off. They need your own accounts, so they can't be scripted for you.

- [ ] **SimFin account.** Register free at <https://simfin.com>, open the
      account/API page, copy your API key.
- [ ] **GitHub repo.** Create a new **private** repo (e.g. `magic-formula`). Private
      because SimFin's free tier is personal-use, no redistribution (see *Hosting*
      below for how the app then fetches the data).
- [ ] **Actions secret.** In the repo: *Settings → Secrets and variables → Actions
      → New repository secret*. Name it `SIMFIN_API_KEY`, paste your key.
- [ ] **Local check.** `pip install simfin pandas` works on your machine
      (`--break-system-packages` if your distro requires it).

**Done when:** the repo exists, the secret is set, and `pip install simfin pandas`
succeeds locally.

---

## Phase 1 — run the engine

```bash
git clone <your-repo-url> && cd magic-formula
pip install -r pipeline/requirements.txt
export SIMFIN_API_KEY=your_key_here
python pipeline/build_rankings.py
```

This downloads the SimFin bulk datasets (cached in `~/simfin_data/`), computes the
ranking, and writes `data/rankings.json` + `data/prices.json`, printing a summary
and the top names.

**Done when:** the run produces valid JSON locally; you've spot-checked a few
well-known tickers by hand; and the top names broadly overlap the free list at
<https://www.magicformulainvesting.com> (broad overlap, not identical — different
dataset and timing).

Run the tests anytime:

```bash
python pipeline/test_magic_formula.py
```

See `pipeline/README.md` for the full maths walkthrough, config knobs, and the
missing-data policy. A concrete sample of the output shape lives in
`pipeline/EXAMPLE_rankings.json` / `EXAMPLE_prices.json`.

---

## Phase 2 — automation

`.github/workflows/refresh.yml` runs the pipeline **06:00 UTC every Monday** and
on demand, then commits refreshed `data/*.json`.

To finish Phase 2:

1. Commit and push this repo (with the `SIMFIN_API_KEY` secret already set).
2. In the repo's **Actions** tab, open *Refresh rankings* and click **Run
   workflow** (`workflow_dispatch`).
3. Confirm the run commits updated `data/rankings.json` + `data/prices.json`.
4. Confirm the JSON is reachable at its URL (see *Hosting*).

**Done when:** a triggered Action refreshes `data/*.json` and the URL returns the
current JSON. **Stop here and confirm the URL works before starting Session B.**

---

## Hosting — how the app reaches the JSON (read before Session B)

The spec suggests `raw.githubusercontent.com/<user>/<repo>/main/data/rankings.json`.
That works **only for public repos**. Since this repo is private (SimFin licence),
its `raw.githubusercontent.com` URLs return 404 without authentication, so use one
of these instead:

**Recommended — GitHub Contents API + a read-only token (keeps the repo private):**

```
GET https://api.github.com/repos/<user>/magic-formula/contents/data/rankings.json
Headers:
  Authorization: Bearer <FINE_GRAINED_PAT>
  Accept: application/vnd.github.raw+json
```

Create a **fine-grained personal access token** scoped to just this repo with
**Contents: Read-only**. The Android app sends it as the `Authorization` header.
For a single-user personal app this is fine; store the token in the app's settings
(or `local.properties` at build time), never commit it. Same two URLs for
`prices.json`. This is the base URL / token the Settings screen in Session B will use.

**Simplest — make the repo public and use raw URLs:** only if you're comfortable
that the *computed ranking* (not the raw SimFin dataset, which is never committed)
being public is acceptable for your use. Then the original
`https://raw.githubusercontent.com/<user>/magic-formula/main/data/rankings.json`
works with no token.

---

## Repository structure

```
magic-formula/
├── pipeline/
│   ├── magic_formula.py        # pure maths: ROC, earnings yield, filters, rank_and_combine
│   ├── build_rankings.py       # load SimFin -> assemble -> filter -> rank -> write JSON
│   ├── test_magic_formula.py   # unit + synthetic end-to-end tests (no key/network)
│   ├── requirements.txt
│   ├── EXAMPLE_rankings.json    # sample contract output (synthetic data)
│   ├── EXAMPLE_prices.json
│   └── README.md
├── data/
│   └── .gitkeep                 # rankings.json + prices.json land here on first run
├── .github/workflows/refresh.yml
├── .gitignore
└── README.md
# android/  -> added in Session B
```

---

## The formula (reference)

- **Return on Capital** `ROC = EBIT / (Net Working Capital + Net Fixed Assets)`,
  where `NWC = (Current Assets − Cash) − (Current Liabilities − Short-Term Debt)`.
- **Earnings Yield** `EY = EBIT / Enterprise Value`,
  where `EV = Market Cap + Total Debt − Cash`.
- Rank by ROC and by EY (highest = 1), sum to `combined_rank`, sort ascending →
  `magic_rank`.

These use Greenblatt's specific definitions, **not** the common ROE/ROA/PE ratios.

---

## Licence / data note

SimFin free tier is **personal-use, no redistribution**. The raw dataset is never
committed (it's cached locally in `~/simfin_data/`, git-ignored); only the computed
JSON is stored. Keep the repo private. This app does not republish SimFin data.
