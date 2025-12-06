FROM python:3.11-slim

LABEL maintainer="aRSSe Project"
LABEL description="Intelligence Layer for aRSSe News Aggregator"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download NLTK data for German stopwords
RUN python -c "import nltk; nltk.download('stopwords'); nltk.download('punkt')"

# Application code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Non-root user for security
RUN useradd -m -u 1000 arsse && chown -R arsse:arsse /app
USER arsse

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "-u", "news_clustering.py"]
