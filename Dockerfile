# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, so a source change doesn't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# README.md is not documentation here: hatchling validates the `readme`
# field at build time and fails without it.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 filen_s3_emulator
WORKDIR /app

COPY --from=builder --chown=filen_s3_emulator:filen_s3_emulator /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

USER filen_s3_emulator
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "filen_s3_emulator.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
