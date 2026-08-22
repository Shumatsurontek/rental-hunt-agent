FROM ghcr.io/astral-sh/uv:0.11.8 AS uv

FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && .venv/bin/playwright install --with-deps chromium

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev \
    && adduser --disabled-password --gecos "" --uid 10001 app \
    && mkdir -p /data/browser /data/debug \
    && chown -R app:app /data

USER app
EXPOSE 8000
ENTRYPOINT ["rental-hunt"]
CMD ["serve"]
