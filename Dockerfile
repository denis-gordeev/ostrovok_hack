FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --chown=app:app agent.py agent_limits.py geo_tools.py geo_api.py transit_routing.py map_tools.py ui_api.py region_config.py wiki_store.py wiki_embeddings.py wiki_retrieval.py retry_utils.py langgraph_app.py langgraph.json langgraph.server.json ./
COPY --from=web --chown=app:app /web/dist ./web/dist
RUN mkdir -p /app/.cache && chown app:app /app/.cache
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 CACHE_DIR=/app/.cache
USER app
EXPOSE 8080
CMD ["uvicorn", "geo_api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
