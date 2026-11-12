# Immutable React assets for the production web host; no Vite server at runtime.
FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS frontend
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /frontend
RUN npm install --global pnpm@10.19.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# Development image. Runtime source can be bind-mounted by compose.
FROM python:3.14-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527 AS development

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/app

WORKDIR /workspace/app
COPY backend/requirements.lock /tmp/mlo-requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/mlo-requirements.lock

# MCP uses its own Pydantic version. Keep its runtime isolated from Django and
# lock all Python packages; no venue snapshots or secrets are baked in.
COPY prototype/venue-recommendation-mcp/requirements.lock /tmp/mlo-venue-mcp-requirements.lock
RUN python -m venv /opt/mlo/venue-mcp \
    && /opt/mlo/venue-mcp/bin/python -m pip install --no-cache-dir -r /tmp/mlo-venue-mcp-requirements.lock \
    && /opt/mlo/venue-mcp/bin/python -m pip check
ENV VENUE_MCP_PYTHON=/opt/mlo/venue-mcp/bin/python

RUN groupadd --gid 10001 mlo && useradd --uid 10001 --gid mlo --create-home mlo
COPY --chown=mlo:mlo . /workspace/app/
RUN mkdir -p /workspace/app/.local /workspace/app/.local-preview-logs \
    && chown -R mlo:mlo /workspace/app
USER mlo

EXPOSE 8001
CMD ["python", "-m", "backend.manage", "runserver", "0.0.0.0:8001"]

FROM development AS production
ARG APP_REVISION
RUN python -c 'import re,sys;sys.exit(0 if re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}",sys.argv[1]) else 2)' "$APP_REVISION"
ENV APP_REVISION=$APP_REVISION APP_KIND=web DJANGO_SETTINGS_MODULE=backend.cafe.settings C_LOCAL_DEMO=0 FRONTEND_REQUIRED=1
ENV VENUE_MCP_SERVER=/workspace/app/prototype/venue-recommendation-mcp/venue_recommendation_mcp.py
COPY --from=frontend --chown=mlo:mlo /frontend/dist /workspace/app/frontend/dist
COPY --chmod=0555 docker/runtime/serve.py /opt/mlo/bin/serve
COPY --chmod=0555 docker/runtime/readiness.py /opt/mlo/bin/readiness
COPY --chmod=0444 docker/runtime/app_runtime.py /opt/mlo/bin/app_runtime.py
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD ["/opt/mlo/bin/readiness"]
CMD ["/opt/mlo/bin/serve", "--config", "/etc/mlo/service.json"]
