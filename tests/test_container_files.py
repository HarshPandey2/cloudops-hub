from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_production_image_is_locked_minimal_and_non_root():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "FROM python:3.13-slim-trixie AS runtime" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'CMD ["fastapi-production-api"]' in dockerfile


def test_docker_context_excludes_secrets_and_local_environments():
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert dockerignore.startswith("**\n")
    assert "!.env" not in dockerignore
    assert "!.venv" not in dockerignore
    assert "!src/**" in dockerignore


def test_compose_requires_explicit_migrations_and_hardens_runtime():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'profiles: ["migration"]' in compose
    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert 'command: ["python", "-m", "app.services.job_dispatcher"]' in compose
    assert compose.count("read_only: true") == 4
    assert compose.count("no-new-privileges:true") == 4
    assert '"127.0.0.1:8000:8000"' in compose
    assert '"127.0.0.1:5432:5432"' in compose
    assert '"127.0.0.1:6379:6379"' in compose
    assert "service_completed_successfully" not in compose


def test_ci_runs_for_the_main_and_cloudops_feature_branches():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("branches: [main, feature/cloudops-jobs]") == 2
    assert "permissions:\n  contents: read" in workflow
    assert "Check tracked files for common secret formats" in workflow
