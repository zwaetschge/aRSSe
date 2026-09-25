# python:3.11-slim (3.11.16, Debian trixie), pinned by digest so a rebuild
# uses the same base; Dependabot proposes updates
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

LABEL maintainer="aRSSe Project"
LABEL description="Intelligence Layer for aRSSe News Aggregator"
LABEL org.opencontainers.image.source="https://github.com/zwaetschge/aRSSe"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NLTK_DATA=/usr/share/nltk_data

WORKDIR /app

# Python dependencies, pinned with hashes (requirements.txt, generated
# from requirements.in); all available as wheels for amd64 and arm64
COPY requirements.txt .
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements.txt

# Stopwords for clustering from a pinned nltk_data commit; a checksum
# mismatch fails the build instead of clustering with other stopwords
COPY fetch_nltk_data.py .
RUN python fetch_nltk_data.py --dest "$NLTK_DATA"

# Application code, owned by root and only readable for the service user:
# the process cannot rewrite its own code or templates
COPY . .

# The entrypoint starts as root, hands /app/data to PUID:PGID (default
# 1000:1000 = arsse) and drops privileges before starting the service;
# only /app/data belongs to that user. Bytecode is compiled now, since the
# container runs with a read-only file system.
RUN useradd -m -u 1000 arsse && mkdir -p /app/data && chown arsse:arsse /app/data \
    && chmod -R a+rX,go-w /app && python -m compileall -q /app

# Runtime: no bytecode writes (read-only root file system) and one BLAS
# thread; clustering is not faster with more, but uses more memory
ENV PYTHONDONTWRITEBYTECODE=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

EXPOSE 8081

# Unhealthy when clustering fails or no run succeeded within 3 intervals
# (while starting, /healthz answers 200 with status 'starting')
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('WEB_PORT', '8081'), timeout=5)"

ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["python", "news_clustering.py"]
