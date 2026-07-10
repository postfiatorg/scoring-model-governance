# Scoring Model Governance

Model governance for the PFT Ledger Dynamic UNL scoring model. This repository has two roles:

- **Public governance record** — `docs/Methodology.md` defines how the scoring model is selected, re-confirmed, and replaced through recurring governance rounds; candidate-pool refreshes, the blocklist, and complete round records are published here as they are produced.
- **Governance service** — `governance_service/` is the foundation-side FastAPI service that maintains the candidate pool and, in later roadmap steps, runs governance exams, grading, and round orchestration. It mirrors the conventions of [dynamic-unl-scoring](https://github.com/postfiatorg/dynamic-unl-scoring).

Validator runtime never reads this repository: sidecars learn about models only from per-round execution manifests.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up
curl http://localhost:8002/health
```

`docker compose up` starts PostgreSQL 16 (host port 5433, so it can run next to the dynamic-unl-scoring stack) and the service with autoreload on host port 8002. Pending SQL migrations from `migrations/` are applied automatically on startup.

## Tests

Tests run against a real PostgreSQL database, the same way CI does:

```bash
docker compose up -d postgres
pytest tests/
```

`DATABASE_URL` overrides the default local connection string when set.

## Project structure

```text
governance_service/
├── main.py          # FastAPI app factory + startup lifecycle
├── config.py        # Environment-based settings
├── database.py      # PostgreSQL connection + migration runner
└── api/
    └── health.py    # /health liveness endpoint
migrations/          # Numbered SQL migrations, applied in order
tests/               # pytest suite (real database, like CI)
docs/                # The governance methodology and public records
```

## CI

GitHub Actions runs the test suite against a PostgreSQL 16 service container and builds the Docker image on every pull request and push to `main`.
