# Institutional Macro Intelligence — XAU/USD

A macro and positioning **situational-awareness dashboard** for gold, built on real,
keyless public data. Not a trading system.

> **⚠️ RESEARCH NOTICE — NOT A TRADING SIGNAL**
> The macro gate and the spatial mean-reversion sweep were both backtested over
> 2000–2026 against the correct statistical null. **Neither shows a statistically
> significant edge.** The macro gate underperformed simply holding gold by ~400
> percentage points; the sweep entry predicted nothing. This project is an
> observational intelligence monitor. Do not use it to place or size trades.
> Full findings: [`docs/SYSTEMS_AUDIT_LOG.md`](docs/SYSTEMS_AUDIT_LOG.md).

---

## What this is

A dashboard that answers *"what are macro conditions and institutional positioning doing
right now, in historical context?"* — with every number traceable to a real source.

| Layer | What it does |
|---|---|
| **Data** | Weekly CFTC gold positioning (1986→), FRED rate path, daily gold OHLC (2000→) |
| **Indicators** | 52-week rolling positioning percentile (SDI), Fed-path dovish proxy, structural swing levels + ATR |
| **API** | Authenticated FastAPI service — state, history, and chart series endpoints |
| **Dashboard** | Streamlit monitor with trend context, percentile bands, and honest degraded states |
| **Ops** | Docker, docker-compose, Kubernetes (CronJobs + HPA + Ingress), adversarial verification gate |

## Data provenance

Nothing in the shipped data path is fabricated. Every panel degrades to `—` rather than
showing a plausible-looking invention.

| Measure | Source | Method |
|---|---|---|
| Institutional positioning (SDI) | CFTC Commitments of Traders, gold contract `088691` | rolling 52-week percentile of net commercial positioning |
| Macro gravity | FRED `DGS2` vs `DFEDTARU` | logistic proxy on the 2y-minus-policy spread |
| Structural levels | Yahoo `GC=F` daily OHLC via `yfinance` | 3-day swing high/low, ATR(14), reference level at low − $1.50 |
| Live spot | gold-api.com | real-time XAU/USD |

**Proxies, stated plainly:** the dovish figure is a FRED yield-spread proxy, *not* CME
FedWatch (which requires Fed Funds futures). The price chart is COMEX `GC=F` futures,
*not* XAU/USD spot. Both are labelled as such in the UI.

## Quick start

```bash
pip install -r requirements.txt

# Build the multi-decade history store (one-time, ~1 min; needs network)
python -m backend.app.history_store

# Backend
cd backend
SYSTEM_AUTH_TOKEN=change-me python -m uvicorn main:app --host 127.0.0.1 --port 8000

# Dashboard (separate shell)
cd frontend
SYSTEM_AUTH_TOKEN=change-me python -m streamlit run streamlit_app.py
```

Open <http://localhost:8501>. The API docs are at <http://localhost:8000/docs>.

> `SYSTEM_AUTH_TOKEN` must match on the API and the dashboard. If unset, the API
> generates a random per-process token and logs a warning — the dashboard will show an
> authentication banner instead of data.

### One-command platform (orchestrator)

```bash
cd backend
SYSTEM_AUTH_TOKEN=change-me python -m orchestrator
```

This warms the repositories and history, launches the API, and runs the ingestion
workers. It exits non-zero on boot failure so container monitors detect it.

## API surface

All `/api/*` routes require `Authorization: Bearer <SYSTEM_AUTH_TOKEN>`. `/health` is open
for orchestrator polling.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/macro-state` | Latest macro regime + gate state |
| `GET /api/v1/spatial-boundaries` | Latest structural levels |
| `GET /api/v1/macro-history` | Positioning trend, 52w range, 4-week change |
| `GET /api/v1/price-history` | Returns, 52w range, realized volatility |
| `GET /api/v1/series?name=…` | Dense series for charting (`sdi`, `close`, `atr14`, …) |
| `GET /api/spot`, `/api/history` | Live spot and recent bars |
| `GET /health` | Liveness (unauthenticated) |

## Deployment

```bash
# Containers
SYSTEM_AUTH_TOKEN=... docker compose up -d

# Kubernetes
kubectl apply -f k8s/data-layer.yaml -f k8s/cronjobs.yaml -f k8s/web-layer.yaml
kubectl apply -f k8s/metrics-server.yaml   # required for the HPA to function
k8s/verify-pipeline.sh                     # adversarial infrastructure gate
```

The Kubernetes layer splits stateless readers (API + HPA) from single-writer ingestion
(CronJobs), and the verification gate **fails** when infrastructure is wrong rather than
reporting success.

## Verification

```bash
python -m pytest backend/tests frontend/tests -q     # 153 tests
k8s/verify-pipeline.sh                               # live-cluster gate
```

Regression guards are mutation-verified: reintroducing a known defect fails the suite.

## Known limitations

Read these before deploying anywhere real.

1. **No demonstrated trading edge.** See the notice above. Two independent backtests
   (macro gate, spatial sweep) and a 150-cell parameter tensor all returned nothing.
2. **Single-node validation only.** Kubernetes was validated on a single-node `kind`
   cluster. Multi-node scheduling, real CSI drivers, and ingress TLS are untested.
3. **`ReadWriteOnce` readers** mean the HPA cannot fan out across nodes until the
   readers move to shared read-only storage or an object store.
4. **No token rotation**, no rate limiting on repeated 401s. The token is a static
   environment value; use a secret store in production.
5. **TLS configured but unproven** — the `ClusterIssuer` needs a publicly resolvable
   domain; a `.local` host can never satisfy an ACME challenge.
6. **FRED proxy ≠ CME FedWatch**; **futures ≠ spot**. Both measured, both labelled.

## Repository layout

```
backend/
  main.py                  FastAPI service (auth middleware, state + history routes)
  orchestrator.py          Lifecycle: pre-flight, health-gated launch, workers
  market_data.py           Keyless market feeds with TTL caching
  app/
    engine_macro.py         CFTC + FRED ingestion, SDI, gate
    engine_spatial.py       Structural boundary mapping
    history_store.py        Multi-decade reference history builder
    backtest_engine.py      Macro gate backtest (correct null, benchmarks, costs)
    backtest_spatial.py     Sweep backtest (look-ahead-free, foresight-off by default)
    strategy_optimizer.py   Parameter tensor + interaction test
  tests/                    153 tests
frontend/
  streamlit_app.py          Situational-awareness dashboard
k8s/                        Kubernetes manifests + verify-pipeline.sh
docs/SYSTEMS_AUDIT_LOG.md   Full audit: directive lineage, 34-item defect ledger, findings
```

## Measurement notes for future researchers

Two traps were found and are now guarded by tests. Both produced *spectacular-looking*
false positives:

- **Null-hypothesis inflation.** Testing a long-only rule's returns against **zero**
  rather than the unconditional hold. Gold's secular drift makes any long strategy look
  significant. One draft reported `t = +0.73` against zero and `t = −1.30` against the
  baseline — opposite signs from the same data.
- **Entry-bar foresight.** Crediting an exit on the bar you entered assumes the bar's
  high printed *after* the low that filled you. On the best grid cell this alone flipped
  `t` from **+6.16 to −6.64**.

If you extend this work, keep the null honest and keep exits off the entry bar.

---

*Built as an AI-agent-assisted engineering project. The most valuable output was not the
code — it was reporting negative results before capital was risked.*
