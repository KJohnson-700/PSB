# PolyBot — Railway / container deploy (see docs/RAILWAY.md)
FROM python:3.11-slim-bookworm

# GitHub-triggered Docker builds pass this; baked into the image so /health can show git_sha
# even when Railway UI "Redeploy" replays an image (no fresh Git metadata at runtime).
ARG RAILWAY_GIT_COMMIT_SHA=
ENV RAILWAY_GIT_COMMIT_SHA=${RAILWAY_GIT_COMMIT_SHA}

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Build deps for scientific stack (wheels) and optional native deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    psmisc \
    && rm -rf /var/lib/apt/lists/*

# Railway: avoid nautilus_trader (very large); see requirements-railway.txt
COPY requirements-railway.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy config (secrets.env excluded via .dockerignore — secrets come from Railway env vars)
COPY config/settings.yaml config/settings.yaml
COPY src/ src/
# Fail the image build if dashboard/server has syntax errors (avoids "SUCCESS" on stale layers).
RUN python -m py_compile src/dashboard/server.py
COPY scripts/ scripts/
COPY tests/ tests/
COPY pytest.ini pytest.ini

RUN mkdir -p data/backtest/reports data/backtest/ohlcv data/paper_trades

# Paper trading by default; override via Railway start command if needed.
CMD ["python", "-m", "src.main", "--paper"]
