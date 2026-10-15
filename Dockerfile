FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data \
    AGENT_CONFIG=/app/agents.toml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --home-dir /app bot \
    && mkdir /app/data \
    && chown bot:bot /app/data

COPY bot.py gamerules.json plot.json ./
COPY core/ ./core/
COPY engine/ ./engine/
COPY adapters/ ./adapters/
COPY tools/ ./tools/

USER bot:bot

CMD ["python", "bot.py"]
