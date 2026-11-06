# Local-only feedback Spark driver: the Django application requires Python 3.14,
# while the upstream Spark 4.1.3 Python image ships Python 3.10.
FROM apache/spark:4.1.3-java21-python3@sha256:bf9d035a7c32a8ca46aa58d6348182ffd7d2dff6409206ecfbb3915ff1fef211

USER root
RUN python3 -m pip install --no-cache-dir "uv==0.9.13"
ENV UV_PYTHON_INSTALL_DIR=/opt/mlo/managed-python
RUN uv python install 3.14.0 \
    && uv venv --python 3.14.0 /opt/mlo/venv \
    && uv pip install --python /opt/mlo/venv/bin/python \
      "Django==6.1.2" "djangorestframework==3.18.3" \
      "psycopg[binary]==3.3.6" "argon2-cffi==25.1.0" \
    && /opt/mlo/venv/bin/python -m django --version

WORKDIR /opt/mlo/app
COPY --chown=spark:spark backend/ ./backend/
COPY --chown=spark:spark pipeline/ ./pipeline/
RUN /opt/mlo/venv/bin/python -c "from pathlib import Path; from zipfile import ZipFile, ZIP_DEFLATED; root=Path('/opt/mlo/app'); z=ZipFile(root / 'feedback-code.zip', 'w', ZIP_DEFLATED); [z.write(p, p.relative_to(root).as_posix()) for name in ('backend', 'pipeline') for p in (root / name).rglob('*.py')]; z.close()" \
    && mkdir -p /opt/mlo/checkpoints /opt/mlo/ivy \
    && chown -R spark:spark /opt/mlo/checkpoints /opt/mlo/ivy /opt/mlo/app \
    && chmod -R a+rX /opt/mlo/venv /opt/mlo/managed-python

ENV PYTHONPATH=/opt/mlo/app \
    PYSPARK_PYTHON=/opt/mlo/venv/bin/python \
    PYSPARK_DRIVER_PYTHON=/opt/mlo/venv/bin/python \
    PATH=/opt/mlo/venv/bin:$PATH

USER spark
CMD ["/opt/spark/bin/spark-submit", "--version"]
