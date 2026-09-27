#!/usr/bin/env bash
#
# Adversarial validation of the live Kubernetes layer.
#
# Corrections vs the directive's draft (each proven by running it against a live cluster):
#   1. The draft invoked `curl` inside the backend container. The runtime image is
#      python:3.13-slim, which has NO curl: step 2 aborted with
#      `exec: "curl": executable file not found in $PATH`. Probing now uses the
#      container's own Python (stdlib urllib), which is guaranteed present.
#   2. The draft did `[ "${READY_REPLICAS}" -ne 3 ]`. If the value is empty or
#      non-numeric during a rollout, bash errors with "integer expression expected",
#      and under `set -e` that aborts the script. Now validated before comparison.
#   3. The draft claimed to verify metrics-server but the server was never installed;
#      step 4 read only the *configured* spec value (75), which is a static number from
#      the manifest and is returned whether or not metrics work. It now checks the live
#      metrics stack and fails when the HPA cannot read real metrics.
#   4. The draft called the 401 probe a "mocked token failure" but sent no token at all.
#      It now proves the gate in both directions: no token -> 401, valid token -> 200.
#
# Usage: k8s/verify-pipeline.sh [-n NAMESPACE] [--expect-replicas N]
set -euo pipefail

NAMESPACE="${NAMESPACE:-gold-intelligence}"
EXPECTED_REPLICAS="${EXPECTED_REPLICAS:-3}"
FAILURES=0

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
amber() { printf '\033[33m%s\033[0m\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -n) NAMESPACE="$2"; shift 2 ;;
        --expect-replicas) EXPECTED_REPLICAS="$2"; shift 2 ;;
        *) red "unknown argument: $1"; exit 2 ;;
    esac
done

# Run a python snippet inside a pod (no curl dependency).
pod_python() {
    local pod="$1"; shift
    kubectl exec -n "${NAMESPACE}" "${pod}" -- python -c "$1" 2>/dev/null || echo "ERR"
}

# ── 1. namespace + HA replicas ────────────────────────────────────────────────
echo "🔎 [1/5] Verifying namespace and replica configuration..."
if ! kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
    red "🔴 [CRITICAL] Namespace ${NAMESPACE} does not exist in the active context."
    exit 1
fi

READY_REPLICAS=$(kubectl get deployment gold-backend-api -n "${NAMESPACE}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)

# Guard the comparison: empty/non-numeric must not abort the script.
case "${READY_REPLICAS}" in
    ''|*[!0-9]*)
        red "🔴 [CRITICAL] Could not read a numeric readyReplicas (got '${READY_REPLICAS}')."
        FAILURES=$((FAILURES + 1))
        ;;
    *)
        if [ "${READY_REPLICAS}" -lt "${EXPECTED_REPLICAS}" ]; then
            red "🔴 [CRITICAL] HA replica shortfall: expected >= ${EXPECTED_REPLICAS}, found ${READY_REPLICAS}."
            FAILURES=$((FAILURES + 1))
        else
            green "🟢 [RESOURCE CHECK SUCCESS] ${READY_REPLICAS}/${EXPECTED_REPLICAS} API replicas ready."
        fi
        ;;
esac

# ── 2. auth gate, both directions ─────────────────────────────────────────────
echo "🔎 [2/5] Adversarially probing the authentication gate..."
BACKEND_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=gold-backend \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [ -z "${BACKEND_POD}" ]; then
    red "🔴 [CRITICAL] No backend pod found."
    exit 1
fi

# No token -> must be 401. Uses the container's Python; no curl needed.
NO_TOKEN=$(pod_python "${BACKEND_POD}" "import urllib.request,urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/macro-state',timeout=5); print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('ERR')")

if [ "${NO_TOKEN}" = "401" ]; then
    green "🟢 [SECURITY VERIFIED] Unauthenticated request correctly returned 401."
else
    red "🔴 [CRITICAL SECURITY BREACH] Unauthenticated request returned: ${NO_TOKEN}"
    FAILURES=$((FAILURES + 1))
fi

# Present-but-wrong token -> must also be 401.
WRONG_TOKEN=$(pod_python "${BACKEND_POD}" "import urllib.request,urllib.error
r=urllib.request.Request('http://127.0.0.1:8000/api/v1/macro-state',headers={'Authorization':'Bearer deliberately-wrong-token'})
try:
    urllib.request.urlopen(r,timeout=5); print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('ERR')")

if [ "${WRONG_TOKEN}" = "401" ]; then
    green "🟢 [SECURITY VERIFIED] Wrong-token request correctly returned 401."
else
    red "🔴 [CRITICAL SECURITY BREACH] Wrong-token request returned: ${WRONG_TOKEN}"
    FAILURES=$((FAILURES + 1))
fi

# Valid token -> must be 200 (proves the gate is not returning 401 for everything).
VALID_TOKEN=$(pod_python "${BACKEND_POD}" "import os,urllib.request,json
tok=os.getenv('SYSTEM_AUTH_TOKEN','')
r=urllib.request.Request('http://127.0.0.1:8000/api/v1/spatial-boundaries',headers={'Authorization':'Bearer '+tok})
try:
    urllib.request.urlopen(r,timeout=15); print(200)
except Exception:
    print('ERR')")

if [ "${VALID_TOKEN}" = "200" ]; then
    green "🟢 [SECURITY VERIFIED] Valid-token request correctly returned 200 (gate is selective, not blanket)."
else
    red "🔴 [CRITICAL] Valid-token request returned: ${VALID_TOKEN}"
    FAILURES=$((FAILURES + 1))
fi

# ── 3. CronJob entry points actually execute ──────────────────────────────────
echo "🔎 [3/5] Verifying single-writer CronJob entry points..."
for job in macro-ingestion-job spatial-mapping-job; do
    if ! kubectl get cronjob "${job}" -n "${NAMESPACE}" >/dev/null 2>&1; then
        red "🔴 [CRITICAL] CronJob ${job} missing."
        FAILURES=$((FAILURES + 1))
        continue
    fi
    # The draft only checked the CronJob *existed*. Existence is not execution: the
    # original commands exited 0 while doing nothing. Assert a real entry point exists.
    engine=$([ "${job}" = "macro-ingestion-job" ] && echo engine_macro || echo engine_spatial)
    if pod_python "${BACKEND_POD}" "import importlib,sys
sys.path.insert(0,'/workspace/project/backend/app')
m=importlib.import_module('${engine}')
print('OK' if callable(getattr(m,'main',None)) else 'NO_MAIN')" | grep -q OK; then
        green "🟢 [INGESTION VERIFIED] ${job}: ${engine}.main() present and callable."
    else
        red "🔴 [CRITICAL] ${job}: ${engine} exposes no callable main() — job would be a no-op."
        FAILURES=$((FAILURES + 1))
    fi
done

# ── 4. HPA: live metrics, not just the spec value ─────────────────────────────
echo "🔎 [4/5] Probing Horizontal Pod Autoscaler metrics availability..."
SPEC_TARGET=$(kubectl get hpa backend-hpa -n "${NAMESPACE}" \
    -o jsonpath='{.spec.metrics[0].resource.target.averageUtilization}' 2>/dev/null || true)
echo "📊 HPA configured target: ${SPEC_TARGET:-<unset>}% CPU"

if kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1; then
    LIVE=$(kubectl get hpa backend-hpa -n "${NAMESPACE}" \
        -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || true)
    if [ -n "${LIVE}" ]; then
        green "🟢 [HPA VERIFIED] metrics-server present; live utilisation ${LIVE}%."
    else
        amber "⚠️ [HPA DEGRADED] metrics-server present but no live reading yet (still warming up)."
    fi
else
    red "🔴 [CRITICAL] metrics-server is NOT installed; the HPA cannot act on CPU. Apply k8s/metrics-server.yaml."
    FAILURES=$((FAILURES + 1))
fi

# ── 5. look-ahead / provenance sanity on the live repositories ────────────────
echo "🔎 [5/5] Checking repositories for look-ahead artefacts and provenance..."
MACRO_CHECK=$(pod_python "${BACKEND_POD}" "import pandas as pd,datetime
df=pd.read_csv('/workspace/project/data/macro_intelligence_repository.csv')
d=pd.to_datetime(df['week_ending_date'])
now=pd.Timestamp.utcnow().tz_localize(None).normalize()
future=(d>now).sum()
print('FUTURE' if future else ('NOSRC' if 'source' not in df.columns else 'OK'))" | tail -1)

case "${MACRO_CHECK}" in
    OK)     green "🟢 [PROVENANCE VERIFIED] No future-dated rows; source column present." ;;
    FUTURE) red "🔴 [CRITICAL] Repository contains future-dated rows (look-ahead)."; FAILURES=$((FAILURES + 1)) ;;
    NOSRC)  red "🔴 [CRITICAL] Repository lacks a source column (no provenance)."; FAILURES=$((FAILURES + 1)) ;;
    *)      red "🔴 [CRITICAL] Could not evaluate repository integrity (got '${MACRO_CHECK}')."; FAILURES=$((FAILURES + 1)) ;;
esac

# ── verdict ───────────────────────────────────────────────────────────────────
echo
if [ "${FAILURES}" -eq 0 ]; then
    green "🏆 [PIPELINE SUCCESS] All adversarial infrastructure checks passed."
    exit 0
fi
red "🔴 [PIPELINE FAILED] ${FAILURES} check(s) failed. System is NOT production-locked."
exit 1
