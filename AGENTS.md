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

## Spatial boundary engine (Directive 04)
- `backend/app/engine_spatial.py` (`SpatialBoundaryEngine`) → `data/spatial_boundaries_repository.csv`.
- Columns: `Date` (ISO string), `Three_Day_High`, `Three_Day_Low`, `Sweep_Floor` (= Low − 1.50),
  `ATR_14`, `Source`. Swing/ATR windows are shifted 1 session to avoid look-ahead bias.
- **No fabricated values.** ATR is a real 14-period mean; rows lacking full lookback are
  dropped, never `fillna`-ed. The directive draft's `.fillna(10.0)` stamped a fake constant.
- **Fallback serves a real cached payload only** (`data/spatial_raw_cache.csv`, labelled
  `CACHE_GC=F`). With no cache it raises `SpatialDataUnavailable` — it never invents prices.
- `yfinance` (>=0.2.40) works from this egress via query2 for `GC=F`; output is MultiIndex,
  so flatten columns before use.
- Tests: `backend/tests/test_engine_spatial.py`. Both regression guards are mutation-verified.

## Orchestrator (Directive 08)
- Canonical lifecycle engine: `backend/orchestrator.py`. `backend/app/main_orchestrator.py`
  is a delegation shim only — do not fork a second copy of the logic.
- Pre-flight `warm_data_caches()` seeds any missing/empty repository so the API cannot
  boot into a 503 state.
- Two daemon threads (`run_macro_loop`, `run_spatial_loop`), each with typed exception
  logging and exponential backoff; neither crash kills the app context.
- Health: `HEALTH_URL` defaults to `http://127.0.0.1:<port>/health`. **Never** use the
  directive's literal `http://127.0.0`, which parses as host `127.0` port 80 and always
  refuses; a guard repairs that host automatically.
- **Launch is `main:app` with `cwd=backend/`.** `python -m uvicorn backend.main:app`
  from the project root dies with `ModuleNotFoundError: No module named 'market_data'`.
- Signal handler only flags shutdown; the main thread performs ordered teardown and
  exits 1 when boot failed (so Docker/K8s see the failure).

## Authentication (Directive 10)
- Stateless bearer auth enforced by **HTTP middleware across every `/api/*` path**.
  `_OPEN_PATHS` (allow-list) contains only `/health` and the docs routes. A new API
  route is therefore protected by default, never open by omission.
- **Never protect a subset of routes that share a payload.** `/api/state` returns the
  same macro data as `/api/v1/macro-state`; protecting only the v1 routes would have
  been a cosmetic bypass (caught in review, now guarded by a test).
- `SYSTEM_AUTH_TOKEN` is read from the environment. **There is no hardcoded fallback.**
  If unset, a random per-process ephemeral token is generated and warned once (dev only);
  with a blank env, no caller-supplied value authenticates. Set the same value on the
  API and the cockpit.
- Constant-time compare (`hmac.compare_digest`); 401 responses carry
  `WWW-Authenticate: Bearer` and never leak which part of the token was wrong.
- Cockpit shows a prominent banner and scrubs displayed state on 401 rather than
  silently falling back to local data.
