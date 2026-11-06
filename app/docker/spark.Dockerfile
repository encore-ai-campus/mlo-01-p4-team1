# Local integration only: Spark 4.1.3 + existing 3rd-stage weather package.
# Production requires the reviewed connector lock from pipeline/weather/submit_spark.py.
FROM apache/spark:4.1.3-java21-python3@sha256:bf9d035a7c32a8ca46aa58d6348182ffd7d2dff6409206ecfbb3915ff1fef211

USER spark
ENV PYTHONPATH=/opt/mlo/app
WORKDIR /opt/mlo/app
COPY --chown=spark:spark pipeline/ ./pipeline/
RUN python3 -c "from pipeline.weather.submit_spark import build_package; print(build_package())" \
    && mkdir -p /opt/mlo/ivy /opt/mlo/checkpoints

CMD ["/opt/spark/bin/spark-submit", "--version"]
