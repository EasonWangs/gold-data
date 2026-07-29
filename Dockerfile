FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

USER app

EXPOSE 5080

# The Compose file provides the web and scheduler commands explicitly.
CMD ["python", "gold_service.py"]
