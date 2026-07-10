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

## Deployment

The service follows the PostFiat branch-based deployment pattern:

| Environment | Branch | Docker image tag | Compose file |
|-------------|--------|------------------|--------------|
| Local dev | `main` | built from source | `docker-compose.yml` |
| Devnet | `devnet` | `agtipft/scoring-model-governance:devnet-latest` | `docker-compose.devnet.yml` |
| Testnet | `testnet` | `agtipft/scoring-model-governance:testnet-latest` | `docker-compose.testnet.yml` |

Pushing to an environment branch runs the tests, builds and pushes the Docker image (the environment tag plus an immutable commit tag), connects to the environment's Vultr host over SSH, writes the runtime `.env` from GitHub secrets, and recreates the containers. Each host needs a one-time preparation before its first deploy: install Docker, allow ports 22 and 8002 through the firewall, and create `/opt/scoring-model-governance`. The service listens on port 8002 over HTTP; DNS and TLS termination follow once the environment gets a hostname. Testnet is wired but dormant until its host is provisioned.

### GitHub secrets

| Secret | Description | Per-environment |
|--------|-------------|-----------------|
| `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` | Docker Hub login and access token | Shared |
| `VULTR_SSH_USER` / `VULTR_SSH_KEY` | SSH user and private key for the Vultr hosts | Shared |
| `VULTR_DEVNET_HOST` / `VULTR_TESTNET_HOST` | Environment host IP | Per-environment |
| `DEVNET_DB_PASSWORD` / `TESTNET_DB_PASSWORD` | PostgreSQL password, written into the host `.env` at deploy time | Per-environment |

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
