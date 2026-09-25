"""Tests for the Docker runtime configuration.

These read the two files as text on purpose: pulling in a YAML parser just for this
would be another dependency, and the settings guarded here are the ones whose loss
breaks behaviour silently rather than loudly.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"


def test_compose_mounts_the_data_directory():
    """The database and the catalog must survive a rebuild, so /app/data is a bind mount."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "./data:/app/data" in compose


def test_compose_keeps_the_container_timezone_settable():
    """SQLite decides the day boundary with local time.

    Without TZ the container runs in UTC and every day would start two hours off in
    Czech winter time, which is the bug the localtime day filters exist to prevent.
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "TZ:" in compose
    assert "TZ=Europe/Prague" in dockerfile
    # The TZ variable does nothing unless the zone database is installed.
    assert "tzdata" in dockerfile


def test_compose_binds_the_dashboard_to_localhost_by_default():
    """The dashboard has no authentication, so it is not published on every interface.

    The bind address stays configurable because the dashboard is also opened from other
    devices over Tailscale; DASHBOARD_URL then has to name the same address, otherwise
    the links the bot posts point somewhere unreachable.
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert '"${WEB_BIND:-127.0.0.1}:8000:8000"' in compose
    assert "restart: unless-stopped" in compose


def test_image_listens_on_every_interface_and_restarts_the_bot():
    """Inside the container the server has to bind 0.0.0.0, and it has to come back alone."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "WEB_HOST=0.0.0.0" in dockerfile
    assert 'CMD ["nutrition-agent", "--run-all"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    # The build context must not carry the live database or the secrets.
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    assert "*.db" in dockerignore
