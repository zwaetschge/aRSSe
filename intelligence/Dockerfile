FROM python:3.11-slim

LABEL maintainer="aRSSe Project"
LABEL description="Intelligence Layer for aRSSe News Aggregator"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NLTK_DATA=/usr/share/nltk_data

WORKDIR /app

# Python dependencies (all available as wheels, no compiler needed)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Stopwords for clustering; fail the build instead of clustering without them
RUN python -c "import nltk, sys; sys.exit(0 if nltk.download('stopwords', download_dir='$NLTK_DATA') else 1)"

# Application code
COPY . .

# The entrypoint starts as root, hands /app/data to PUID:PGID (default
# 1000:1000 = arsse) and drops privileges before starting the service;
# the code stays root-owned but readable for any PUID
RUN useradd -m -u 1000 arsse && mkdir -p /app/data && chown arsse:arsse /app/data \
    && chmod -R a+rX /app

EXPOSE 8081

# Unhealthy when no clustering run succeeded within 3 intervals
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('WEB_PORT', '8081'), timeout=5)"

ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["python", "news_clustering.py"]
