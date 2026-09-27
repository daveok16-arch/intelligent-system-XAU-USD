# Institutional Macro Engine (XAU/USD) — Final Systems Audit Log

Traceability record from the original raw directive set to the current multi-pod
Kubernetes architecture. Intended for handover and post-mortem use.

**Repository:** `daveok16-arch/intelligent-system-XAU-USD`
**Head at time of audit:** `5fbe97d`
**Automated tests:** 91 passing (`python -m pytest backend/tests frontend/tests -q`)
**Verification standard:** every claim below was produced by executing code against
live data sources, a live container stack, or a live `kind` cluster — not by inspection.

---

## 1. Directive lineage

Directive numbering was reconciled on discovery of the original ingestion module.
The cockpit was always "02" and the orchestrator "03", so the ingestion engine is the
true **01** — the origin of the system. No renumbering was required.

| # | Module delivered | Commit | Status |
|---|---|---|---|
| **01** | **Macro ingestion engine** (Pillars 1 & 2, SDI, gate) | `bfb3114` / `27e3366` | delivered, rewritten |
| 02 | Frontend cockpit HUD (Streamlit) | `89f2400` | delivered, corrected |
| 03 | Master orchestrator (API + ingestion loop) | `bfb3114` | delivered, corrected |
| 04 | Spatial boundary mapping engine | `240af17` | delivered, corrected |
| 05 | Spatial test suite / runner compatibility | `9bfc175` | delivered, corrected |
| 06 | v1 API endpoints + CORS hardening | `ceef1df` | delivered, corrected |
| 07 | Cockpit wiring to live v1 data | `353e4d9` | delivered, corrected |
| 08 | Consolidated orchestrator + cache warming + dual loops | `d377560` | delivered, corrected |
| 09 | Production scheduling + daily-roll guard + API hardening | `4436575` | delivered, corrected |
| 10 | Bearer authentication across the API surface | `ef57243` | delivered, corrected |
| — | Dockerfile, compose, bind/proxy fixes | `1d206c4` | delivered, corrected |
| 11 | Kubernetes cluster layer | `8b0d2af` | delivered, corrected |
| 12 | Adversarial pipeline gate + metrics-server + storage fix | `5fbe97d` | delivered, corrected |

"Corrected" means the directive did not work as written and was repaired before
delivery. Every correction is in the defect ledger (§4).

---

## 2. Data provenance

Nothing in the shipped data path is fabricated. Each pillar was traced to a live,
keyless source and validated by executing the fetch.

| Pillar | Source | Method | Verification |
|---|---|---|---|
| 1. Macro gravity | FRED `DGS2` vs `DFEDTARU` | logistic spread → dovish-pivot **proxy** | live fetch, 4.87 vs 4.00 |
| 2. Fund flow (SDI) | CFTC COT, gold contract `088691` | net non-commercial / open interest | live: 253,982 / 28,129 / 412,800 → **0.5471** |
| 3. Spatial boundaries | Yahoo `query2` `GC=F` | 3-day swing + ATR(14); floor = low − $1.50 | 49 rows written in-cluster |
| Live spot | gold-api.com | real-time XAU/USD | $4,286.20 |
| Price history | Yahoo `query2` `GC=F` | daily bars, labelled futures proxy | 21 bars |

**Current authoritative state:** `2026-09-22`, SDI `0.5471`, gate `CLOSED`, source `CFTC_COT`.

### Provenance rules enforced in code
- Readers select the latest record **by max date**, never by file position.
- Every repository row carries a `source` column (`CFTC_COT` vs `SEED`).
- An unavailable panel renders `—`; it never substitutes a plausible number.

---

## 3. Architecture as built

```
Platform Engine (backend/orchestrator.py)
├── Backend microservice (FastAPI :8000) — bearer-authenticated, max-date reads
│   ├── /api/v1/macro-state, /api/v1/spatial-boundaries
│   ├── /api/state, /api/spot, /api/history (hardened), /health (open)
│   └── engines: CFTC COT + FRED (macro), Yahoo GC=F (spatial)
├── Ingestion workers (daemon threads locally; CronJobs in Kubernetes)
└── Cockpit HUD (Streamlit :8501) — live v1 data, per-source fallback chain
```

**Security:** bearer middleware over every `/api/*` path (allow-list `/health` + docs);
constant-time comparison; no hardcoded fallback token; 401 leaks no state; backend
container publishes no host port.

**Deployment:** multi-stage non-root Dockerfile; compose stack (backend internal-only);
Kubernetes with namespace, shared repository volume, Secret, CronJobs, HPA, Services,
NGINX Ingress + cert-manager ClusterIssuer, and metrics-server.

---

## 4. Defect ledger

Every defect below was reproduced before being fixed. Grouped by severity class.

### 4a. Silent-failure class (worst: reports success while doing nothing)

| # | Defect | Evidence | Resolution |
|---|---|---|---|
| D1 | Directive 1 `ingest_institutional_fund_flow` had a **hard SyntaxError** — `"large_spec_longs":,` with no value; three of four COT columns were empty | `python -m py_compile` → `SyntaxError: expression expected after dictionary key and ':'` | rewritten against real CFTC data |
| D2 | **Kubernetes CronJob commands were no-ops.** Neither engine had a `__main__` entry point, so jobs exited **0** with no output | ran `python -m backend.app.engine_macro` → exit 0, no output | added guarded `main()` entry points that exit non-zero on failure |
| D3 | `/health` probe race: `time.sleep(2)` instead of polling | socket not yet bound on slow start | active bounded health polling |

### 4b. Fabrication class (invented data presented as real)

| # | Defect | Evidence | Resolution |
|---|---|---|---|
| D4 | Directive 1 was **100% mock data** (`mock_fedwatch_stream`, `mock_cot_tape`, hardcoded retail ratios) written straight to the production repository | code inspection | replaced with live CFTC + FRED ingestion |
| D5 | Directive 1 **overwrote** the repository every run, destroying history | `to_csv(..., index=False)` with no append | idempotent, chronologically sorted append |
| D6 | `ATR_14` fabricated via `.fillna(10.0)` — a constant stamped onto 10 of 18 rows and labelled a true-range mean | ran directive code verbatim | real 14-period mean; short-lookback rows excluded |
| D7 | Spatial fallback **manufactured constant OHLC prices** and wrote them to disk as data | ran directive code verbatim | serves only a real cache, labelled `CACHE_*`; otherwise raises |
| D8 | Seed rows I myself introduced in Directive 2 were dated **after** the real report, so they won max-date selection and the cockpit served invented values (SDI 0.68, gate OPEN) while real data sat unused | smoke test | purged; `source` column added for provenance |
| D9 | Directive 1's gate (`fedwatch >= 65 AND bias DOVISH`) would have returned **CLOSED forever** on real data, because real dovish probability was 14.93 | calculation against live values | gate = SDI > 0.50 **AND** dovish > 0.50 |

### 4c. Would-not-run class

| # | Defect | Evidence | Resolution |
|---|---|---|---|
| D10 | `np.random.randn(20, 2) * 5 +,` — dangling operator, hard SyntaxError | `py_compile` | fixed |
| D11 | `requests.get("http://127.0.0")` — invalid host (host `127.0.0`, port 80) | `ConnectionError` | `127.0.0.1:8000/health` + repair guard |
| D12 | Health poll `http://127.0.0` would have failed **every** boot and exited 1 | live test | corrected host |
| D13 | `uvicorn backend.main:app` from project root | `ModuleNotFoundError: No module named 'market_data'` | `main:app` with `cwd=backend/` |
| D14 | Bare `uvicorn` / `streamlit` not on `PATH` in this container | `exit 127` | `python -m ...` |
| D15 | Target path `/workspaces/gold_intelligence_app` did not exist | `ls` → no such file | real root `/workspace/project` |
| D16 | Orchestrator pre-flight checked a CWD-relative CSV that never matched, so it silently never ran | path resolution | absolute paths from `__file__` |
| D17 | Dockerfile `COPY requirements.txt .` — no root requirements.txt existed | build would fail at that layer | root requirements.txt added |
| D18 | Deployment command `uvicorn backend.main:app` (same as D13) | `ModuleNotFoundError` | corrected |
| D19 | CronJob command used a path the image does not have | `/workspaces/...` absent | `workingDir: /workspace/project/backend/app` |

### 4d. Security class

| # | Defect | Evidence | Resolution |
|---|---|---|---|
| D20 | Directive 10 protected only `/api/v1/*` and `/api/history`, leaving **`/api/state` open** — returning the *identical* macro payload | live: `/api/v1/...` 401, `/api/state` 200 | middleware over all `/api/*`; allow-list `/health` |
| D21 | Directive 10 specified a **hardcoded fallback token** `INSTITUTIONAL_CORE_SECURE_TOKEN_2026` when unset | code inspection | fails closed; ephemeral random token if unset; blank authenticates nothing |
| D22 | CORS `allow_origins=["*"]` **with** `allow_credentials=True` is spec-invalid; Starlette echoes the origin, so the directive's own assertion failed | ran the config → header was the origin, not `*` | credentials dropped, `GET` only, origins env-configurable |
| D23 | `force_gate` test hook could flip the gate banner on a decision surface | live: `?force_gate=CLOSED` | removed |
| D24 | `/api/history` was an unauthenticated, unbounded upstream passthrough | live: unparameterized → 200 | both params required + allow-listed → 422 |
| D25 | CORS/K8s secret key `AUTH_TOKEN` would never reach the app (`envFrom` maps keys verbatim) | manifest review | key renamed `SYSTEM_AUTH_TOKEN` |

### 4e. Data-integrity and topology class

| # | Defect | Evidence | Resolution |
|---|---|---|---|
| D26 | `df.iloc[-1]` used for "latest" — an appended older row hijacked it, moving the timestamp backward | appended out-of-order row | max-date selection everywhere |
| D27 | Directive 6 anchored repos at `backend/app/`, where they do not exist — **every** route would 503 | path check | real `data/` directory |
| D28 | Single shared `ReadWriteMany` PVC gave no writer isolation and would stay Pending on EBS/GCE/kind | manifest review | corrected topology (see D31) |
| D29 | The Ingress referenced `cockpit-ui-service`, which was **never defined** | manifest grep | both Services added |
| D30 | metrics-server absent → HPA stuck at `<unknown>`; the Directive 12 check echoed the *static* spec value and claimed success regardless | `kubectl top` → Metrics API not available | metrics-server + APIService added; HPA now reports live `2%/75%` |
| D31 | **I introduced this in Directive 11:** the per-writer PVC split left readers mounting only the macro volume, so `/api/v1/spatial-boundaries` returned **503 unconditionally** and the cockpit spatial card was permanently blank | in-cluster valid-token probe → 503; `data/` empty in API pod | shared repository volume; readers see both files |

### 4f. Directive-internal contradictions (spec vs. its own code)

| # | Contradiction | Resolution |
|---|---|---|
| D32 | Directive 9 code triggered on parse failure; its prose said block. Opposites. | block on clock anomaly (safer); self-heal on corrupt contents |
| D33 | Directive 12 claimed to test a "mocked token failure" but sent **no token**; claimed to verify metrics-server but read a static value | both are now tested for real |
| D34 | Directive 5's test subclassed plain `object` (not `TestCase`) → runner reported **"NO TESTS RAN"** | proper `TestCase`; runner reports real counts |

---

## 5. Verification evidence

- **91 automated tests** across backend and frontend; guards are **mutation-verified**
  (reintroducing each defect fails the suite).
- **Live stack:** orchestrator → API → cockpit with real data; authenticated and
  unauthenticated paths exercised.
- **Docker:** image built; backend container healthy; backend not host-published;
  cockpit authenticates across the compose network.
- **Kubernetes (live `kind` cluster):** all objects applied; 3 API + 2 cockpit pods
  Running; CronJobs executed (spatial 49 rows; macro SDI 0.5471/CLOSED); in-cluster
  Service `health=200`, `401` unauthenticated; HPA live metrics.
- **Pipeline gate:** green (exit 0); mutations trip it (exit 1) for a missing CronJob,
  a replica shortfall, and an injected future-dated row.
- **Secret scan** of working tree and full git history before pushing to a public repo:
  no credentials present.

---

## 6. Standing limitations

Recorded so they are not mistaken for solved:

1. `fedwatch_dovish_prob` is a **FRED-derived proxy**, not true CME FedWatch (needs a
   paid futures feed). It gates the strategy; the distinction matters.
2. The price-history chart is COMEX **futures** (`GC=F`), a labelled proxy — not XAU/USD spot.
3. **Single static token.** No rotation, no brute-force lockout, no rate limiting on 401s.
4. The `ClusterIssuer` needs a **publicly resolvable domain**; `.local` will never
   satisfy an ACME challenge. TLS is configured but unproven.
5. `ReadWriteOnce` reader volume means the HPA **cannot fan out across nodes**.
   `maxReplicas: 10` is aspirational until readers move to shared read-only storage.
6. Live-cluster validation was on **`kind`, single-node**. Multi-node scheduling, real
   CSI, and real ingress termination are untested.
7. `metrics-server.yaml` contains `--kubelet-insecure-tls` for dev clusters; it must be
   removed on real infrastructure.

---

## 7. Assessment

The system is a rigorously verified **single-node reference deployment**. Its defining
property is that it does not lie: no fabricated data reaches the cockpit, unavailable
sources are visibly unavailable, and the verification gate fails when infrastructure
is wrong.

It is not yet battle-tested on production infrastructure. Items §6 should be closed
before it carries capital.
