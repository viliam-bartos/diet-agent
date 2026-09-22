# The bot runs unattended, so the image stays on a slim, stable Python.
FROM python:3.12-slim-bookworm

# SQLite decides the day boundary with local time (date(logged_at, 'localtime')), so the
# container has to know the zone. Without tzdata the TZ variable is silently ignored and
# every day would start at UTC midnight.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    TZ=Europe/Prague

WORKDIR /app

# The source is copied before the install because the install is editable; a source change
# therefore reinstalls the dependencies too. That keeps this file short at the cost of a
# slower rebuild, which is a fair trade for a service that is rebuilt rarely.
COPY pyproject.toml README.md ./
COPY src ./src
# Editable on purpose: the dashboard template is read from the package directory, and this
# way the package always resolves to the /app/src tree that is present in the image.
RUN pip install -e .

# Seed only: the database and the curated catalog are mounted from the host over /app/data.
COPY data/foods.json ./data/foods.json

# Run as an ordinary user whose ids match the owner of ./data on the host, otherwise the
# mounted database would not be writable. Override with --build-arg when `id -u` differs.
ARG PUID=1000
ARG PGID=1000
RUN groupadd --gid "${PGID}" app \
    && useradd --uid "${PUID}" --gid "${PGID}" --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/version', timeout=5)"]

CMD ["nutrition-agent", "--run-all"]
