FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEBCODER_DATA_DIR=/data \
    WEBCODER_HOST=0.0.0.0 \
    WEBCODER_PORT=8787

WORKDIR /app
COPY requirements-hub.txt ./
RUN pip install --no-cache-dir -r requirements-hub.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin webcoder \
    && mkdir -p /data && chown webcoder:webcoder /data
COPY --chown=webcoder:webcoder webcoder ./webcoder

USER webcoder
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)"
CMD ["python", "-m", "webcoder.hub", "run"]
