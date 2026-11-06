# The ads API has its own Django 5.2 lockfile; do not merge with common API.
FROM python:3.12-slim-bookworm

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
