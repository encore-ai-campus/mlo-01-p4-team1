# The ads API has its own Django 5.2 lockfile; do not merge with common API.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS development

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/mlo-ads-venv \
    PATH=/opt/mlo-ads-venv/bin:$PATH \
    PYTHONPATH=/workspace/app/ad-server/backend

RUN python -m pip install --no-cache-dir "uv==0.9.13"
WORKDIR /workspace/app/ad-server/backend
COPY ad-server/backend/pyproject.toml ad-server/backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

RUN groupadd --gid 10002 mlo-ads && useradd --uid 10002 --gid mlo-ads --create-home mlo-ads
COPY --chown=mlo-ads:mlo-ads ad-server/backend/ ./
# The isolated ads presentation references the shared penguin asset outside ad-server/.
COPY --chown=mlo-ads:mlo-ads docs/assets/penguin/penguin-guide-first.png /workspace/app/docs/assets/penguin/
RUN chown -R mlo-ads:mlo-ads /workspace/app/ad-server/backend
USER mlo-ads

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM development AS production
ARG APP_REVISION
RUN python -c 'import re,sys;sys.exit(0 if re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}",sys.argv[1]) else 2)' "$APP_REVISION"
ENV APP_REVISION=$APP_REVISION APP_KIND=advertising DJANGO_DEBUG=0
COPY --chmod=0555 docker/runtime/serve.py /opt/mlo/bin/serve
COPY --chmod=0555 docker/runtime/readiness.py /opt/mlo/bin/readiness
COPY docker/runtime/app_runtime.py /opt/mlo/bin/app_runtime.py
EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD ["/opt/mlo/bin/readiness"]
CMD ["/opt/mlo/bin/serve", "--config", "/etc/mlo/service.json"]
