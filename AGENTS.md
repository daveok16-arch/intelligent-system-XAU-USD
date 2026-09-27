# Institutional Macro Engine — Working Notes

XAU/USD macro intelligence platform. Backend owns strategy state; Streamlit cockpit
renders it for human approval.

## Layout
- `backend/main.py` — FastAPI API (`:8000`): `/api/state`, `/api/spot`, `/api/history`, `/health`, `/api/refresh`
- `backend/market_data.py` — live feeds: gold-api.com (XAU/USD spot), Yahoo `query2` `GC=F` (daily bars)
- `backend/app/engine_macro.py` — `InstitutionalDataIngestor`: CFTC COT + FRED → SDI + gate
- `backend/app/main_orchestrator.py` — boots API, runs ingestion cron, ordered shutdown
- `data/macro_intelligence_repository.csv` — authoritative weekly state; readers select by **max date**
- `frontend/streamlit_app.py` — cockpit HUD (`:8501`)
- `backend/tests/` — `python -m pytest backend/tests -q`

## Environment gotchas (container)
- **Console scripts are NOT on PATH.** Always `python -m streamlit` / `python -m uvicorn`.
- **Do not send a custom User-Agent to FRED.** A bot-style UA makes
  `fred.stlouisfed.org` hang until timeout (20s+); plain `requests` returns in ~0.1s.
  `_UA = None` in engine_macro is deliberate.
- **Yahoo: use `query2`, not `query1`.** query1 returns 429 from this egress.
- **Stooq serves an anti-bot JS proof-of-work challenge** — not usable headless, and
  bypassing it is out of scope.
- **Background processes need `setsid` in non-interactive shells**, otherwise they
  inherit SIGINT-ignored and signal tests give false negatives.

## Data rules
- Repo rows must be ordered by max date, not file position (`df.iloc[-1]` is wrong).
- Every row carries a `source` column (`CFTC_COT` = real; `SEED` = fabricated placeholder).
- **Never seed fabricated rows dated after real data** — they win max-date selection
  and the cockpit then presents invented macro values as authoritative.
- SDI = (noncomm_long - noncomm_short) / open_interest (CME gold, contract 088691).
- Gate = OPEN only when SDI > 0.50 AND dovish proxy > 0.50.
- `fedwatch_dovish_prob` is a **logistic proxy** (FRED DGS2 vs DFEDTARU), not true CME
  FedWatch. Must stay labelled as a proxy.
- History chart is COMEX **futures** (`GC=F`), a proxy — not XAU/USD spot.
