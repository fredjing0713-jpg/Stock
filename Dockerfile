# copybot: Discord 信号跟单 → IBKR 自动下单
# 构建: docker compose build    (或 docker build -t copybot .)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=America/New_York

# TZ only affects log timestamps (the base image ships the system tz database);
# trading logic always uses explicit America/New_York via zoneinfo.
WORKDIR /app

# Runtime dependencies only (no [dev] extras).
COPY pyproject.toml ./
COPY copybot ./copybot
RUN pip install . \
    && rm -rf build ./*.egg-info

# Non-root user. data/ and logs/ are volumes: on the host run
#   mkdir -p data logs && sudo chown -R 1000:1000 data logs
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" copybot \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin copybot \
    && mkdir -p /app/data /app/logs \
    && chown copybot:copybot /app/data /app/logs

USER copybot

ENV COPYBOT_CONFIG=/app/config.yaml

CMD ["python", "-m", "copybot"]
