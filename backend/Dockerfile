# syntax=docker/dockerfile:1.7
#
# Multi-stage. 3.12 not 3.13 — some LangGraph/psycopg extras still lag on 3.13
# wheels, and a wheel hunt mid-build is not how this week should be spent.
#
# Build for ARM64 on ECS (runtime_platform.cpu_architecture = "ARM64") for ~20%
# compute savings. Build natively on an arm64 runner, not under QEMU emulation.

# ----------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency layer first: pyproject alone changes far less often than source,
# so edits to app/ reuse this layer instead of reinstalling the world.
COPY pyproject.toml ./
RUN uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# ----------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root. ECS will not enforce this for you.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser app ./app

USER appuser
EXPOSE 8000

# Fargate slim images ship no curl, so the usual `curl -f` healthcheck fails in a
# way that looks like the app is down. Use the interpreter that is definitely here.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

# --timeout-keep-alive must exceed the ALB idle timeout (300s), or the ALB will
# reuse a connection uvicorn just closed and the client sees a 502.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-keep-alive", "305"]
