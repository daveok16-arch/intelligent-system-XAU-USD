# Multi-stage build, non-root runtime.
#
# Corrections vs the directive's snippet:
#   - `COPY requirements.txt .` failed: no root requirements.txt existed, so the
#     layer errored. A root requirements.txt is now provided.
#   - `USER 1000` is a raw uid with no passwd entry; a real user is created so the
#     runtime has a home and a resolvable identity.
#   - PYTHONUNBUFFERED so orchestrator logs reach the container log driver.
#   - HEALTHCHECK uses the stdlib (python:3.13-slim has no curl/wget).
#   - The app binds 0.0.0.0 (see API_HOST in backend/orchestrator.py); binding
#     127.0.0.1 inside a container would be unreachable through EXPOSE.

FROM python:3.13-slim AS builder
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/workspace/project/data

COPY --from=builder /install /usr/local

WORKDIR /workspace/project
COPY . .

# Non-root runtime identity (passwordless, no shell login).
RUN groupadd --gid 1000 appuser \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /workspace/project/data \
 && chown -R appuser:appuser /workspace/project

USER 1000

EXPOSE 8000 8501

# Stdlib health probe (no curl in slim). /health is the one unauthenticated route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"]

CMD ["python", "-m", "backend.orchestrator"]
