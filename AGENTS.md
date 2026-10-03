# Institutional Macro Engine ŌĆö Working Notes

XAU/USD macro intelligence platform. Backend owns strategy state; Streamlit cockpit
renders it for human approval.

## Layout
- `backend/main.py` ŌĆö FastAPI API (`:8000`): `/api/state`, `/api/spot`, `/api/history`, `/health`, `/api/refresh`
- `backend/market_data.py` ŌĆö live feeds: gold-api.com (XAU/USD spot), Yahoo `query2` `GC=F` (daily bars)
- `backend/app/engine_macro.py` ŌĆö `InstitutionalDataIngestor`: CFTC COT + FRED ŌåÆ SDI + gate
- `backend/app/main_orchestrator.py` ŌĆö boots API, runs ingestion cron, ordered shutdown
- `data/macro_intelligence_repository.csv` ŌĆö authoritative weekly state; readers select by **max date**
- `frontend/streamlit_app.py` ŌĆö cockpit HUD (`:8501`)
- `backend/tests/` ŌĆö `python -m pytest backend/tests -q`

## Environment gotchas (container)
- **Console scripts are NOT on PATH.** Always `python -m streamlit` / `python -m uvicorn`.
- **Do not send a custom User-Agent to FRED.** A bot-style UA makes
  `fred.stlouisfed.org` hang until timeout (20s+); plain `requests` returns in ~0.1s.
  `_UA = None` in engine_macro is deliberate.
- **Yahoo: use `query2`, not `query1`.** query1 returns 429 from this egress.
- **Stooq serves an anti-bot JS proof-of-work challenge** ŌĆö not usable headless, and
  bypassing it is out of scope.
- **Background processes need `setsid` in non-interactive shells**, otherwise they
  inherit SIGINT-ignored and signal tests give false negatives.

## Data rules
- Repo rows must be ordered by max date, not file position (`df.iloc[-1]` is wrong).
- Every row carries a `source` column (`CFTC_COT` = real; `SEED` = fabricated placeholder).
- **Never seed fabricated rows dated after real data** ŌĆö they win max-date selection
  and the cockpit then presents invented macro values as authoritative.
- SDI = (noncomm_long - noncomm_short) / open_interest (CME gold, contract 088691).
- Gate = OPEN only when SDI > 0.50 AND dovish proxy > 0.50.
- `fedwatch_dovish_prob` is a **logistic proxy** (FRED DGS2 vs DFEDTARU), not true CME
  FedWatch. Must stay labelled as a proxy.
- History chart is COMEX **futures** (`GC=F`), a proxy ŌĆö not XAU/USD spot.

## Spatial boundary engine (Directive 04)
- `backend/app/engine_spatial.py` (`SpatialBoundaryEngine`) ŌåÆ `data/spatial_boundaries_repository.csv`.
- Columns: `Date` (ISO string), `Three_Day_High`, `Three_Day_Low`, `Sweep_Floor` (= Low ŌłÆ 1.50),
  `ATR_14`, `Source`. Swing/ATR windows are shifted 1 session to avoid look-ahead bias.
- **No fabricated values.** ATR is a real 14-period mean; rows lacking full lookback are
  dropped, never `fillna`-ed. The directive draft's `.fillna(10.0)` stamped a fake constant.
- **Fallback serves a real cached payload only** (`data/spatial_raw_cache.csv`, labelled
  `CACHE_GC=F`). With no cache it raises `SpatialDataUnavailable` ŌĆö it never invents prices.
- `yfinance` (>=0.2.40) works from this egress via query2 for `GC=F`; output is MultiIndex,
  so flatten columns before use.
- Tests: `backend/tests/test_engine_spatial.py`. Both regression guards are mutation-verified.

## Orchestrator (Directive 08)
- Canonical lifecycle engine: `backend/orchestrator.py`. `backend/app/main_orchestrator.py`
  is a delegation shim only ŌĆö do not fork a second copy of the logic.
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

## Deployment (Docker)
- Root `requirements.txt` is required: the Dockerfile's `COPY requirements.txt .` fails
  without it (only backend/ and frontend/ had one).
- `API_HOST` must default to `0.0.0.0`. A container binding 127.0.0.1 is unreachable
  through EXPOSE/-p. `HEALTH_URL` still targets loopback.
- `FORWARDED_ALLOW_IPS` defaults to `127.0.0.1` (loopback only). Never `*`: broad trust
  lets a client spoof X-Forwarded-For and poison the auth forensics log.
- Dockerfile: multi-stage, non-root (uid 1000 via a real account), stdlib HEALTHCHECK
  (slim has no curl). CMD `python -m backend.orchestrator`.
- docker-compose: backend publishes **no** host port (internal network only); cockpit
  published as 8501. `SYSTEM_AUTH_TOKEN` uses `${VAR:?}` so the stack refuses to start
  with a missing credential.
- `AppTest` re-executes the script in a fresh namespace: monkeypatching the imported
  module does not affect it. Use a stub HTTP server for render-path tests.

## Kubernetes (Directive 11)
- Manifests in `k8s/`: `data-layer.yaml` (Namespace, PVCs, Secret), `cronjobs.yaml`,
  `web-layer.yaml` (Deployments, Services, HPA, Ingress, ClusterIssuer).
- **CronJob commands are `python -m engine_macro` / `engine_spatial` with
  `workingDir: /workspace/project/backend/app`.** Both engines now expose `main()`;
  before this they had no `__main__` and the jobs exited 0 doing nothing.
- Engines' `main()` honour the schedule guard and exit non-zero on failure
  (`restartPolicy: OnFailure`); `--force` bypasses the guard for manual reruns.
- **Separate ReadWriteOnce PVC per writer.** A single shared PVC gives no single-writer
  isolation; RWX is unsupported by most CSI drivers and is the wrong tool here.
- **Secret key must be `SYSTEM_AUTH_TOKEN`** because `envFrom` maps keys to env vars
  verbatim. The workloads use `envFrom`, not `secretKeyRef`.
- **API command is `python -m uvicorn main:app` with `workingDir: .../backend`** ŌĆö
  `uvicorn backend.main:app` fails (bare uvicorn off PATH + `import market_data`).
- The Ingress needs a Service (`cockpit-ui-service`, `backend-api-service`); the draft
  referenced one that did not exist.

## Backtest (Directive 13)
- `backend/app/backtest_engine.py` evaluates the production gate rule (long gold while
  `MACRO_GATE == OPEN`, i.e. SDI > 0.50 AND dovish > 0.50) over real history.
- **Real data only.** CFTC COT from 1986 (1,935 weekly records), FRED rates, and GC=F
  prices from 2000 are fetched. The engine raises `BacktestDataUnavailable` rather than
  simulating returns. Never reintroduce an `np.random` price fallback ŌĆö a Sharpe computed
  on random numbers is worse than no answer.
- **Look-ahead guards:** execution signal is shifted one bar; the SDI percentile is
  ROLLING (52 weeks), never a full-sample min/max. Both are mutation-verified.
- Benchmarks buy-and-hold and charges 5 bps round-trip, so the Sharpe is interpretable.
- History must be fetched, not read from `data/` ŌĆö those repos hold current state only.

### Measured result (2008-12-19 to 2026-09-25, 928 weeks, 52 trades)
- Strategy total return **+15.3%**, Sharpe **-0.36**, MDD **-27.9%**
- Buy-and-hold benchmark: **+416.6%**, Sharpe **0.40**, MDD **-43.6%**
- Gate-open weeks averaged +0.241%/wk vs +0.196% closed -> edge +0.045%/wk,
  **t = 0.19** (statistically indistinguishable from chance)
- **Conclusion: the gate as specified has no demonstrated edge and materially
  underperforms simply holding gold.** Do not proceed to execution on this rule.

## Spatial sweep backtest (Directive 14)
- `backend/app/backtest_spatial.py` tests the stop-hunt hypothesis: a limit buy inside
  `Sweep_Floor` (3-day low ŌłÆ $1.50) catching a mean-reverting bounce.
- **Real daily OHLC is required.** The directive read `Low`/`High`/`Close` from
  `spatial_boundaries_repository.csv`, which has only structural columns ŌĆö it died with
  `KeyError: ['Low', 'High']`. The engine fetches GC=F daily bars and raises
  `SpatialBacktestDataUnavailable` rather than simulating.
- **The decisive correction: the null must be the unconditional forward hold, not zero.**
  Gold drifts up, so a long-only rule beats zero for free. The draft tested against zero
  and would have reported a misleadingly positive sign.
- Also fixed: the draft only inspected the entry bar, so the stated 3-day hold never
  happened; gap-through fills are now modelled (fill at open when it gaps below the limit).

### Measured result (2000-08-30 to 2026-09-25, 6,543 bars, 1,424 sweeps)
- Win rate **46.35%**, profit factor **1.047**, compounded **+29.4%**
- Avg trade **+0.031%** vs baseline hold **+0.095%**
- t **vs zero**: +0.73 (looks positive ŌĆö pure drift) ┬Ę t **vs baseline**: **ŌłÆ1.30**
- **Conclusion: the sweep entry is WORSE than simply holding, and has no statistically
  distinguishable edge.** Neither the macro gate nor the spatial floor carries alpha as
  specified. Do not wire either into execution.

## Parameter tensor + interaction (Directive 15)
- `backend/app/strategy_optimizer.py` sweeps target ├Ś stop ├Ś hold ├Ś macro-gated, reusing
  the verified `SpatialEdgeBacktester` so the two engines cannot diverge.
- **The draft was the most dangerous artefact in this project.** `gold_historical_daily_raw.csv`
  did not exist, so it fell back to SYNTHESISING prices from the indicator being traded
  (`Close = Sweep_Floor + 2*ATR`, `Low = Sweep_Floor - 0.5*ATR`, `High = Close + ATR`) and
  then backtested against them. Its construction made the strategy unlosable ŌĆö a 2.5*ATR
  target is always reached, a 2.0*ATR stop never hit ŌĆö and it reported t-stats of **634**.
  That was algebra, not edge.
- **Second, subtler bias found by our own re-run:** crediting an exit on the ENTRY bar
  assumes the bar's High printed *after* the Low that triggered the fill ŌĆö perfect
  intrabar foresight. `allow_entry_bar_exit` defaults to **False**. Measured impact on the
  best cell: t **+6.16 ŌåÆ ŌłÆ6.64** once the foresight assumption is removed.
- **Entry-convention contamination:** the strategy fills at the FLOOR while the baseline is
  measured from the bar CLOSE, and the floor sits ~0.2% below that close, giving the
  strategy a cheaper start by construction. 104 of 150 cells exceeded |t|>2 where a true
  null yields ~5% ŌĆö the signature of bias, not edge.

### Decisive result ŌĆö convention-free test (both sides buy at close)
| Horizon | Sweep fwd | Unconditional fwd | Edge | t |
|---|---|---|---|---|
| 1 bar | +0.0070% | +0.0486% | ŌłÆ0.042% | ŌłÆ1.16 |
| 2 bars | +0.0284% | +0.0966% | ŌłÆ0.068% | ŌłÆ1.37 |
| 3 bars | +0.0600% | +0.1448% | ŌłÆ0.085% | ŌłÆ1.44 |
| 5 bars | +0.1684% | +0.2420% | ŌłÆ0.074% | ŌłÆ0.97 |

**The sweep event predicts nothing.** Forward returns after a sweep are *below* average at
every horizon ŌĆö there is no post-sweep bounce. The macro gate (Directive 13) and the
spatial mean-reversion floor (Directives 14ŌĆō15) are both closed. Reframe as a
situational-awareness dashboard.

## Intraday microstructure (re-opened research)

Directives 13ŌĆō15 tested the sweep thesis on DAILY bars. That is the wrong resolution: a
daily "sweep" is only "the low went below a level", so the push-down-then-recover
sequence the mechanism depends on is invisible. `backend/app/engine_microstructure.py`
re-tests it on **13,715 hourly GC=F bars (2024-05 ŌåÆ 2026-09)**, where the sequence is
observable.

### What IS real (the premise is partly correct)

**Sweep bars show a genuine volume signature:**

| group | mean volume vs trailing median | n |
|---|---|---|
| sweep (pierce + close back above) | **2.14├Ś** | 870 |
| non-sweep | 1.34├Ś | 12,106 |
| | **t = 13.90** | |

Something structurally real happens at these bars: volume is ~60% higher than normal
activity at the same level. That is consistent with liquidity being taken. **This part of
the thesis holds.**

**Sweeps cluster by session** ŌĆö they are not uniformly distributed:

| session | sweep rate |
|---|---|
| NY afternoon (13ŌĆō16 UTC) | **11.47%** |
| NY open (8ŌĆō11) | 5.81% |
| London open (2ŌĆō5) | 5.11% |
| Asia (19ŌĆō24) | 3.74% |

3├Ś concentration in the NY PM session is a real structural pattern.

### What is NOT real (the tradeable claim fails)

A high "reversion rate" (77ŌĆō92%) is **not** evidence. Gold drifts up, so price regains
almost any level eventually. The question is whether *rejection* carries information, and
that requires a matched contrast:

| horizon | sweep fwd | breakdown fwd | sweep ŌłÆ breakdown | t |
|---|---|---|---|---|
| 1 bar | ŌłÆ0.011% | +0.009% | ŌłÆ0.020% | ŌłÆ1.11 |
| 2 bars | ŌłÆ0.019% | +0.019% | ŌłÆ0.038% | ŌłÆ1.64 |
| 4 bars | ŌłÆ0.016% | +0.018% | ŌłÆ0.034% | ŌłÆ1.14 |
| 8 bars | ŌłÆ0.014% | +0.013% | ŌłÆ0.026% | ŌłÆ0.57 |
| 12 bars | +0.016% | ŌłÆ0.009% | +0.025% | +0.46 |

**A rejected sweep performs no better than an accepted breakdown ŌĆö if anything slightly
worse.** Max |t| across all horizons and both level definitions: **1.96**. The rejection
signature does not predict a reversion.

### Honest verdict

The mechanism is real; the *signal* is not. Liquidity-taking is observable (volume,
session clustering), but knowing that a sweep just happened tells you nothing about the
next 1ŌĆō12 hours that you could trade. **The premise was not wrong ŌĆö it was untradeable
as formulated.**

This is a more precise conclusion than "no edge", and it is the second time the daily-vs-
intraday distinction mattered. It also means the door is not fully closed: a volume-
conditional or session-conditional formulation was not exhausted here.

## Data-integrity impact assessment (post-database)

The database constraints surfaced **441 impossible bars** in the upstream Yahoo GC=F daily
series (`high` below `max(open,low,close)`), which the CSV pipeline had been serving
silently. The obvious question is whether any published conclusion depended on that
corruption. It is now bounded rather than assumed:

| repair effect | measurement |
|---|---|
| bars touched | 441 of 6,543 |
| mean distortion | 0.33% relative |
| max distortion | 7.9% relative |
| **`Close` changed** | **0 bars (max delta 0.0)** |
| intraday impossible bars | **0 of 13,715** |

Because the repair only widens `High`/`Low` to an internally consistent envelope and never
touches `Close`, the impact is **provably nil** for every conclusion that uses returns:

- **Macro gate backtest (D13)** — uses `Close` for returns. Mathematically unaffected.
- **Volatility / conditional / walk-forward studies** — intraday bars had zero impossible
  bars. Untouched.
- **Spatial sweep backtest (D14)** — the one test using `High`/`Low`/`ATR`. Re-run on
  repaired data:

  | data | trades | win | avg | t vs baseline |
  |---|---|---|---|---|
  | original (corrupted) | 1,424 | 45.58% | +0.0043% | −1.873 |
  | **repaired** | 1,431 | 45.14% | −0.0075% | **−2.105** |

  Same sign, slightly stronger. The conclusion ("the sweep entry underperforms holding")
  **holds and sharpens** — repaired data moves it from non-significant to significantly
  negative. Corruption had been *flattering* the strategy, not creating it.

No qualitative conclusion in this project changes. Two quantitative details (win rate
−0.44pp, t −0.23) shift marginally in the direction of the existing verdict.

## Cross-asset replication + specificity (the strongest result)

Two explanations would have invalidated the sweep volatility finding: *it is one series*
and *it is just the leverage effect*. Both were tested directly.

### Replication across four metals — the effect is a market property

`backend/app/microstructure_crossasset.py`. Identical parameters across assets (no
per-asset tuning, which would defeat the purpose). Copper is the strongest control: it
shares no demand or positioning story with gold.

| asset | bars | sweeps | all sweeps | t | high-vol sweeps | t |
|---|---|---|---|---|---|---|
| gold | 13,695 | 870 | 1.185× | 4.34 | **1.336×** | **5.89** |
| silver | 13,694 | 931 | 1.136× | 3.11 | **1.290×** | **4.68** |
| platinum | 13,683 | 951 | 1.095× | 2.32 | **1.184×** | **3.57** |
| copper | 13,687 | 961 | 1.165× | 3.49 | **1.239×** | **4.25** |

**4/4 assets positive, 4/4 significant, 0/4 negative, median 1.264×.** Including copper.
The one-series explanation is dead.

### Specificity — it beats the leverage effect too

`backend/app/microstructure_specificity.py`. Volatility is higher after declines (the
leverage effect), so "sweep" could just mean "price fell recently". Four increasing controls:

| control | beats it? |
|---|---|
| C1 unconditional baseline | 4/4 |
| C2 any ordinary down bar | 2/4 |
| C3 **size-matched** dip (decile-matched on prior move) | **3/4** |
| C4 pierced-but-not-rejected | see below |

**C4 is the decisive control and its sign is the opposite of what volatility clustering
predicts:**

| asset | rejected sweep | pierced, not rejected | t |
|---|---|---|---|
| gold | 0.185% | 0.256% | **−2.81** |
| silver | 0.366% | 0.528% | **−3.94** |
| platinum | 0.352% | 0.425% | −1.85 |
| copper | 0.242% | 0.297% | −0.97 |

If the effect were merely "dips beget volatility", a pierced bar that *kept falling* would
show equal or higher volatility — and it does. But a bar that pierced and was **rejected**
shows *lower* subsequent movement than one that failed to recover. **The recovery is the
informative part**, not the dip. That is a structural distinction, and it is the first
evidence in this project that the rejection signature carries information.

### Sub-period stability (addressing the shared-window concern)

All four assets live in the same two years, so a common volatility regime could drive all of
them. Checked per asset across 4 folds:

| asset | folds > 1.0 | ratios |
|---|---|---|
| gold | 4/4 | 1.255, 1.259, 1.085, 1.137 |
| silver | 4/4 | 1.205, 1.273, 1.028, 1.084 |
| platinum | 3/4 | 1.267, 1.134, 1.128, 0.997 |
| copper | 4/4 | 1.220, 1.108, 1.193, 1.156 |

15 of 16 folds above 1.0. The shared-window explanation is weakened but **not eliminated** —
a 2-year sample remains a 2-year sample, and this is the honest limit of the evidence.

### What this is and is not

**Is:** a replicated, specificity-tested, sub-period-stable statistical structure. A
rejected sweep of a rolling swing low is followed by a larger absolute move than a matched
ordinary dip, in four metals including an industrial one, with the opposite sign for
non-rejected pierces.

**Is not:** a directional signal (three studies, zero directional edge) and not yet a
working volatility strategy. The economic magnitude on a 1-hour horizon is ~0.18–0.37% of
price against ~0.16–0.39% unconditional — a 20–34% relative expansion. Useful for position
sizing, stop placement and volatility targeting; not a standalone edge, and nothing here is
wired to execution.

**Honest residual limit:** 2 years of hourly data, all assets in the same window. A
multi-year replication across a different regime would raise confidence materially and has
not been done.

## Volatility forecasting — does it beat EWMA? (the usable answer)

`backend/app/volatility_forecast.py`. The sweep studies showed a larger absolute move
follows a rejected sweep. But every desk already forecasts volatility with **EWMA**
(RiskMetrics λ=0.94), and "volatility rises after a dip" is the leverage effect. So the
only question that matters is:

> Does sweep state add forecasting power **beyond EWMA**, out of sample?

Tested as `log(RV_{t+1..t+h}) ~ a + b·log(EWMA_t) + c·sweep_t`, fit on the first 60%,
evaluated on the last 40%, across four metals. `c` is the answer.

### Result: it improves forecasts, but EWMA already has most of it

| check | result |
|---|---|
| specifications tested | 12 (4 assets × 3 horizons) |
| out-of-sample improvement over EWMA | **10/12** |
| out-of-sample Diebold-Mariano significant | **3/12** |
| median RMSE improvement | **0.051%** |
| in-sample coefficient t-stats | 3.7–5.0 |

The in-sample t-stats look impressive and mean nothing — that is the same trap this
project has hit three times. The honest statistic is the **out-of-sample** DM test, and
only 3 of 12 specifications clear it. **EWMA already captures the bulk of the effect.**

### What the state actually predicts (practical multipliers)

`exp(coef)` = multiplicative volatility multiplier relative to the EWMA prediction:

| asset | state | h=1 | h=4 | h=12 |
|---|---|---|---|---|
| gold | rejected sweep | 1.30× | 1.17× | 1.09× |
| gold | failed pierce | 1.44× | 1.26× | 1.12× |
| silver | rejected sweep | 1.29× | 1.13× | 1.04× |
| silver | failed pierce | 1.52× | 1.29× | 1.13× |
| copper | rejected sweep | 1.22× | 1.14× | 1.02× |
| copper | failed pierce | 1.33× | 1.20× | 1.05× |

**Practical reading: after a sweep or a failed pierce, raise the volatility estimate
~20–50% for the next 1–4 bars, decaying to near-nothing by 12.** That is usable for
position sizing and stop placement, and it is a rule a practitioner can apply.

### A correction to my own earlier claim

The specificity study reported that rejected sweeps show *lower* forward movement than
failed pierces (C4, t = −2.8 to −3.9 on raw absolute returns). Under this better-specified
model the asymmetry **reverses**: `asymmetry_diff` is negative in **21 of 24**
specifications, meaning a rejected sweep carries a *larger* coefficient than a failed
pierce once EWMA is controlled for. My earlier raw-return comparison did not control for
EWMA and misread the direction. The corrected statement:

- **Both** resolutions raise volatility.
- **Neither** is distinguishable from the other once EWMA is controlled (3/24 significant,
  no asset consistent across horizons).
- So the *resolution* is not informative for volatility — only *that a pierce happened*.

That weakens the C4 interpretation but does not remove the effect: the pierce itself
carries incremental information over EWMA, at short horizons, in all four metals.

### Honest verdict

**A real but modest forecast improvement.** 10/12 specifications improve, ~0.05% RMSE,
significant in 3/12, with a clear horizon decay (short-lived information). EWMA with a
faster decay (λ=0.70) absorbs much of it, which is the simplest way a practitioner would
capture the same thing.

**Not a deployable edge on its own**, and nothing is wired to execution. It is a defensible
input to position sizing — "volatility is likely 20–50% above the EWMA estimate for the
next few bars after a pierce" — and it is replicated across four metals.

## Robustness controls — two findings retracted

`backend/app/microstructure_robustness.py`. Two results were at risk of being artefacts.
Both were tested with the stricter null. **Both failed.**

### R1 — session clustering: FALSIFIED

The claim was that sweeps cluster in the New York session (earlier reported as 3×). But if
NY simply *moves more*, more bars cross a rolling low there purely because more ground is
covered. The control normalises the pierce rate by the session's mean absolute return —
"pierces per unit of movement".

| asset | raw NY/other | volatility-normalised | survives |
|---|---|---|---|
| gold | 1.309× | **1.015×** | no |
| silver | 1.364× | **0.999×** | no |
| platinum | 1.147× | **0.901×** | no |
| copper | 1.194× | **0.948×** | no |

Median raw **1.252×** → median normalised **0.974×**. **0 of 4 survive.**

**The session clustering was an artefact of volatility, not session structure.** New York
does not produce more sweeps per unit of movement; it produces more movement, and that
crosses more levels. The earlier "3× concentration" figure came from a coarser session
definition and did not apply this control.

### R2 — the volatility forecast: does not survive

The claim was that sweep state forecasts volatility beyond EWMA. But a sweep happens in
conditions that are *already* volatile, so sweep may only proxy today's volatility. The
control adds the current bar's absolute return.

| asset | RMSE EWMA | +curvol | +curvol+sweep | sweep t | DM vs +curvol | survives |
|---|---|---|---|---|---|---|
| gold | 0.64845 | 0.63707 | 0.63662 | 4.550 | 1.022 | no |
| silver | 0.78829 | 0.75473 | 0.75377 | 3.886 | **2.368** | yes |
| platinum | 0.70077 | 0.68045 | 0.67982 | 4.800 | 1.339 | no |
| copper | 0.62097 | 0.61629 | 0.61633 | 4.602 | −0.076 | no |

**1 of 4 survives.** The sweep coefficient *stays* significant in-sample (t = 3.9–4.8)
while the out-of-sample forecast barely improves — median **+0.08% RMSE**. That is the
same in-sample/out-of-sample trap this project has now hit four times.

**With current volatility controlled, sweep state adds little. The "forecast" was largely
volatility persistence**, which every existing model already captures.

### What actually survives every control

One thing, and it is descriptive rather than predictive:

| finding | status |
|---|---|
| Directional edge | **no** — three studies, all negative |
| Session clustering | **falsified** (R1) |
| Volatility forecast beyond EWMA | **fails** with current-vol control (R2) |
| Sweep volume signature | **survives** — 2.14× trailing-median volume, t = 13.9, 4/4 assets |

**Sweep bars carry ~2.1× normal volume, replicated across four metals.** That is a
*detection* fact: something unusual is happening now. It has never been shown to forecast
direction, magnitude beyond existing models, or timing.

### Why this is the right outcome

Two claims were retracted by my own controls. That is the process working — the same
discipline that caught the fabricated t=634, the null-inflation trap, the entry-bar
foresight, and the loose EWMA guard. A research programme that only ever confirms its own
hypotheses is not doing research.

**The honest summary of this project's market-intelligence finding: institutional
liquidity-taking is detectable in volume, and nothing more has survived testing.**

## Conditional studies + walk-forward (the earlier finding)

Following the volume and session leads, three further pre-registered studies were run.

### Directional conditioning is empty
`backend/app/microstructure_conditions.py` pre-registered four directional hypotheses
(volume, session, pierce-depth, combined), split 70/30 discovery/validation, and required
|t|>=2 **with the pre-registered sign**. Result:

- **Zero** core hypotheses (those predicting positive edge) passed in discovery OR
  validation, on either level definition.
- The only cells that passed were the negative arms (low-volume / shallow), and the
  discovery hits **failed out-of-sample** (0/2 confirmed).
- 3 cells appeared in validation-only with no corresponding discovery — noise by
  definition, within the 1.4 expected false positives.

**Conditioning sweeps on volume, session, or depth does not produce directional edge.**

### Magnitude is a different story
`backend/app/microstructure_volatility.py` tested whether a sweep predicts the *size*
of the subsequent move rather than its direction. Discovery showed strong effects
(V3 sweep→range t=+6.98 on rolling levels, +7.46 on prior-day levels).

### Walk-forward settles it — and it partly survives
`backend/app/microstructure_walkforward.py` splits into **5 independent folds** rather
than one, to distinguish a robust effect from a single period's luck:

| fold | period | high-vol sweep / unconditional | t |
|---|---|---|---|
| 1 | 2024-05 → 2024-10 | **1.495×** | 4.08 |
| 2 | 2024-10 → 2025-04 | **1.634×** | 4.08 |
| 3 | 2025-04 → 2025-10 | 1.232× | 1.75 |
| 4 | 2025-10 → 2026-04 | 1.084× | 0.83 |
| 5 | 2026-04 → 2026-09 | **1.431×** | 3.54 |

**Median ratio 1.431×, 3/5 folds significant, ZERO folds negative, ratio never below
1.08.** For all sweeps (not volume-conditioned) the rolling-level series gives
median 1.186× with 3/5 significant and no negatives.

**What this means, precisely:**

1. A sweep is followed by a **larger absolute move** than normal. The direction is not
   predictable; the magnitude is.
2. This is **level-dependent**: rolling swing lows show it consistently (never negative
   across 5 folds); prior-day lows show nothing (median 1.011×). That specificity is
   reassuring — a pure artefact would not respect the choice of level.
3. The effect is real but **decays in the middle of the sample** (folds 3–4), so it is
   regime-sensitive rather than constant.

### Economic magnitude — the honest caveat

| measure | value |
|---|---|
| median sweep absolute move (1h) | **0.204%** of price |
| median unconditional 1h move | 0.160% of price |
| ratio | **1.186×** |

A ~19–43% relative volatility expansion is metabolically useful for **position sizing,
stop placement, and volatility targeting** — not as a standalone directional signal. At
~$4,300 gold, the effect on a 1-hour horizon is on the order of $2 of extra expected
absolute movement. That is a real but modest edge in volatility terms.

### Status: a live research lead, not a deployable signal

This is the first positive result in the project that survived out-of-sample. It is
**not** a tradeable directional strategy and nothing here should be wired into execution.
It is a genuine, replicated statistical structure worth further work:

- test on finer bars (5-minute) where the event is sharper
- test whether the effect survives across other metals (silver, platinum) — cross-asset
  replication would substantially raise confidence
- investigate why folds 3–4 decay (feed into a regime filter)
- the level-dependence suggests the *quality* of the swept level matters; unify the
  level definition around swing structure rather than a fixed lookback
