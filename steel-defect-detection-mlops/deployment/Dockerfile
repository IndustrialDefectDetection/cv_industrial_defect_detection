FROM python:3.10.20-slim-bookworm@sha256:9643927a6fc74bd81b0f1bbb5cce3cb4a491f46b4c5dbee770f28e575f180015

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib \
    TORCH_HOME=/tmp/torch \
    MODEL_PATH=/app/models/best.pt

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY deployment/requirements-api.txt /tmp/requirements-api.txt
RUN python -m pip install --no-compile --requirement /tmp/requirements-api.txt \
    && rm /tmp/requirements-api.txt

RUN groupadd --gid 10001 app \
    && useradd \
        --uid 10001 \
        --gid app \
        --create-home \
        --shell /usr/sbin/nologin \
        app \
    && install -d -o app -g app /app/models

COPY --chown=app:app deployment/api.py /app/api.py
COPY --chown=app:app deployment/container_entrypoint.py /app/container_entrypoint.py
COPY --chown=app:app deployment/model_integrity.py /app/deployment/model_integrity.py
COPY --chown=app:app \
    runs/detect/steel_defect_colab_50_epochs/weights/best.pt \
    /app/models/best.pt

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).read()"]

ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080", "--limit-concurrency", "16", "--timeout-keep-alive", "5", "--no-proxy-headers", "--no-server-header"]
