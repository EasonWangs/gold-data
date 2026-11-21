FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# tzdata provides the Asia/Shanghai zone database on slim-based images.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --create-home app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=app:app . ./

# A named volume mounted here inherits app ownership on first creation, so the
# admin UI can persist webhook credentials without running the service as root.
RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 5080

# Also serve correctly when the NAS creates a container directly from this image.
# Keep connection/buffer use bounded; this service only accepts small JSON bodies.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5080", "--threads=4", "--connection-limit=64", "--backlog=64", "--channel-timeout=60", "--max-request-body-size=1048576", "--outbuf-high-watermark=1048576", "gold_service:app"]
