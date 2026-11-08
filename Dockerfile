# API, chat client, and dashboard. Docker is built in CI; locally run with uv (see README).
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app /app/.venv /app/.venv
# Chat history is kept in SQLite under /app/data at runtime, so the non-root user owns it.
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    INCIDENTBOT_DB=/app/data/chat.db \
    INCIDENTBOT_WRITER=template
USER app
EXPOSE 8850
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["incidentbot", "healthcheck", "--port", "8850"]
ENTRYPOINT ["incidentbot"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8850"]
