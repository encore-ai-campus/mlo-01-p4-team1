# Development image. Runtime source can be bind-mounted by compose.
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/app

WORKDIR /workspace/app
COPY backend/requirements.lock /tmp/mlo-requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/mlo-requirements.lock

RUN groupadd --gid 10001 mlo && useradd --uid 10001 --gid mlo --create-home mlo
COPY --chown=mlo:mlo . /workspace/app/
RUN mkdir -p /workspace/app/.local /workspace/app/.local-preview-logs \
    && chown -R mlo:mlo /workspace/app
USER mlo

EXPOSE 8001
CMD ["python", "-m", "backend.manage", "runserver", "0.0.0.0:8001"]
