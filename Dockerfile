# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, cached separately from app code so editing silpofit/
# doesn't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev

COPY silpofit ./silpofit


FROM python:3.14-slim

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/silpofit ./silpofit

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER app

EXPOSE 8000

# GEMINI_API_KEY and SILPOFIT_SERVICE_TOKENS are read from the environment at
# startup (see silpofit/config.py) — pass them with `docker run -e` / --env-file,
# never bake them into the image.
#
# Cloud Run injects PORT itself (usually 8080) and requires the container to
# listen on it, so this must read $PORT at runtime rather than hardcode it —
# shell form (not exec-array form) so the variable actually expands.
CMD uvicorn silpofit.server:app --host 0.0.0.0 --port "$PORT"
