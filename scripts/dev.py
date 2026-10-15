"""Cross-platform local-development commands for contributors."""

from __future__ import annotations

import argparse
import base64
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_TEMPLATE = PROJECT_ROOT / ".env.example"
ENV_FILE = PROJECT_ROOT / ".env"
SECRET_PLACEHOLDER = "change_this_to_a_random_secret_key"
RATE_LIMIT_SECRET_PLACEHOLDER = "change_this_to_a_random_rate_limit_key"
OUTBOX_SECRET_PLACEHOLDER = "change_this_to_a_random_outbox_key"
POSTGRES_PASSWORD_PLACEHOLDER = "replace_with_generated_local_postgres_password"


class DevelopmentError(RuntimeError):
    """Raised when the local development environment cannot be prepared."""


def require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise DevelopmentError(
            f"Required command '{command}' was not found on PATH. "
            "See DEVELOPMENT.md for installation instructions."
        )


def run_command(command: list[str]) -> None:
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def require_docker_engine() -> None:
    require_command("docker")
    result = subprocess.run(
        ["docker", "info"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DevelopmentError(
            "Docker engine is unavailable. Start Docker Desktop, select Linux "
            "containers, and verify that 'docker info' succeeds."
        )


def ensure_env() -> bool:
    """Create a local .env with generated secrets, without overwriting one."""
    if ENV_FILE.exists():
        print("Using existing .env file.")
        append_missing_compose_settings()
        return False

    template = ENV_TEMPLATE.read_text(encoding="utf-8")
    if (
        SECRET_PLACEHOLDER not in template
        or RATE_LIMIT_SECRET_PLACEHOLDER not in template
        or OUTBOX_SECRET_PLACEHOLDER not in template
        or POSTGRES_PASSWORD_PLACEHOLDER not in template
    ):
        raise DevelopmentError(
            ".env.example does not contain the expected secret placeholders."
        )

    generated_secret = secrets.token_urlsafe(48)
    generated_rate_limit_secret = secrets.token_urlsafe(48)
    generated_outbox_secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(
        "ascii"
    )
    generated_postgres_password = secrets.token_urlsafe(32)
    ENV_FILE.write_text(
        template.replace(SECRET_PLACEHOLDER, generated_secret, 1)
        .replace(
            RATE_LIMIT_SECRET_PLACEHOLDER,
            generated_rate_limit_secret,
            1,
        )
        .replace(OUTBOX_SECRET_PLACEHOLDER, generated_outbox_secret, 1),
        encoding="utf-8",
    )
    env_contents = ENV_FILE.read_text(encoding="utf-8")
    ENV_FILE.write_text(
        env_contents.replace(
            POSTGRES_PASSWORD_PLACEHOLDER, generated_postgres_password
        ),
        encoding="utf-8",
    )
    print("Created .env with generated local application and database secrets.")
    return True


def append_missing_compose_settings() -> None:
    """Add new local worker settings while preserving existing credentials."""
    contents = ENV_FILE.read_text(encoding="utf-8")
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in contents.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    defaults = {
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "JOB_MAX_RETRIES": "3",
        "JOB_RETRY_BACKOFF_SECONDS": "5",
        "JOB_RETRY_BACKOFF_MAX_SECONDS": "300",
        "JOB_STALE_RUNNING_SECONDS": "300",
    }

    database_url = values.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme.startswith("postgresql") and parsed.hostname:
        port = f":{parsed.port}" if parsed.port is not None else ""
        auth = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
        compose_netloc = f"{auth}@postgres{port}" if auth else f"postgres{port}"
        defaults["COMPOSE_DATABASE_URL"] = urlunsplit(
            (
                parsed.scheme,
                compose_netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        defaults["POSTGRES_USER"] = unquote(parsed.username or "fastapi_user")
        defaults["POSTGRES_DB"] = unquote(parsed.path.lstrip("/").split("/", 1)[0])
        if parsed.password is not None:
            defaults["POSTGRES_PASSWORD"] = unquote(parsed.password)

    missing = [f"{key}={value}" for key, value in defaults.items() if key not in values]
    if missing:
        ENV_FILE.write_text(
            f"{contents.rstrip()}\n\n# Local Celery and Compose settings\n"
            + "\n".join(missing)
            + "\n",
            encoding="utf-8",
        )


def setup(*, skip_docker: bool = False) -> None:
    require_command("uv")
    ensure_env()
    run_command(["uv", "sync", "--locked", "--all-groups"])

    if not skip_docker:
        require_docker_engine()
        run_command(["docker", "compose", "up", "-d", "--wait", "postgres", "redis"])

    run_command(["uv", "run", "alembic", "upgrade", "head"])
    print("\nSetup complete. Start the API with: python scripts/dev.py serve")


def serve() -> None:
    require_command("uv")
    run_command(
        [
            "uv",
            "run",
            "uvicorn",
            "app.main:app",
            "--reload",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]
    )


def migrate() -> None:
    require_command("uv")
    run_command(["uv", "run", "alembic", "upgrade", "head"])


def services_up() -> None:
    require_docker_engine()
    ensure_env()
    run_command(["docker", "compose", "up", "-d", "--wait", "postgres", "redis"])


def stack_up() -> None:
    require_docker_engine()
    ensure_env()
    run_command(["docker", "compose", "up", "--build", "-d", "--wait"])


def database_down() -> None:
    require_docker_engine()
    run_command(["docker", "compose", "down"])


def check() -> None:
    require_command("uv")
    pip_audit_cache = (
        Path(tempfile.gettempdir()) / "fastapi-production-api-pip-audit-cache"
    )
    commands = [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
        ["uv", "run", "alembic", "upgrade", "head"],
        ["uv", "run", "pytest", "--cov-report=xml"],
        ["uv", "run", "pip-audit", "--cache-dir", str(pip_audit_cache)],
        ["uv", "build"],
    ]
    for command in commands:
        run_command(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, run, and validate the local development environment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup",
        help="Create .env, install dependencies, start services, and migrate.",
    )
    setup_parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Use dependency URLs in .env without starting Docker Compose.",
    )
    subparsers.add_parser("serve", help="Run the API locally with auto-reload.")
    subparsers.add_parser("migrate", help="Apply all database migrations.")
    subparsers.add_parser(
        "db-up", help="Start and wait for local PostgreSQL and Redis."
    )
    subparsers.add_parser(
        "stack-up", help="Build and start the complete containerized stack."
    )
    subparsers.add_parser("db-down", help="Stop local Compose services.")
    subparsers.add_parser("check", help="Run the complete CI-equivalent quality gate.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    actions = {
        "serve": serve,
        "migrate": migrate,
        "db-up": services_up,
        "stack-up": stack_up,
        "db-down": database_down,
        "check": check,
    }

    try:
        if args.command == "setup":
            setup(skip_docker=args.skip_docker)
        else:
            actions[args.command]()
    except DevelopmentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(
            f"Error: command failed with exit code {error.returncode}.",
            file=sys.stderr,
        )
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
