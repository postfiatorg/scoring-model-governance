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
| `DEVNET_ADMIN_API_KEY` / `TESTNET_ADMIN_API_KEY` | Admin API key for the pool-refresh trigger, written into the host `.env` at deploy time | Per-environment |
| `IPFS_API_URL` / `IPFS_API_USERNAME` / `IPFS_API_PASSWORD` | IPFS node HTTP API for pinning refresh snapshot files | Shared |
| `PINATA_API_KEY` / `PINATA_API_SECRET` | Pinata credentials for secondary snapshot replication | Shared |
| `DEVNET_RECORDS_GITHUB_TOKEN` / `TESTNET_RECORDS_GITHUB_TOKEN` | Fine-grained PAT (contents:write on this repository) for automatic record publication | Per-environment |

## Project structure

```text
governance_service/
├── main.py              # FastAPI app factory + startup lifecycle
├── config.py            # Environment-based settings
├── database.py          # PostgreSQL connection, advisory locks, migration runner
├── freshness.py         # Mapping/schema freshness check (python -m governance_service.freshness)
├── model_mapping.yaml   # Curated LiveBench key → HuggingFace artifact mapping
├── model_blocklist.yaml # Standing blocklist of revisions that failed past rounds
├── api/
│   ├── _helpers.py      # Admin auth and refresh-lock preconditions
│   ├── health.py        # /health liveness endpoint
│   └── pool.py          # Admin-guarded manual pool-refresh trigger
├── clients/
│   ├── livebench.py     # Leaderboard data fetch, strict parsing, site-exact averaging
│   ├── huggingface.py   # Revision pinning, weight sizes, config, license/gating
│   ├── ipfs.py          # Snapshot pinning to the foundation IPFS node
│   ├── pinata.py        # Secondary snapshot replication
│   └── github_records.py # Record publication via the GitHub Contents API
├── models/
│   ├── candidates.py    # Candidate-sourcing data models
│   └── pool.py          # Pool-refresh data models
└── services/
    ├── gpu_fit.py       # Dtype-aware cheapest-fit GPU assignment
    ├── candidate_sourcing.py # One auditable sourcing pass over a release
    ├── pool_refresh.py  # Pool rules, release fallback, refresh persistence
    └── record_publisher.py # Record rendering, snapshot pinning, publication
migrations/              # Numbered SQL migrations, applied in order
records/                 # Published pool-refresh records, one file pair per refresh
tests/                   # pytest suite (real database for DB paths, HTTP mocked
                         # over snapshot fixtures of live leaderboard data)
docs/                    # The governance methodology and public records
```

## Candidate sourcing (G.2.3)

The candidate-sourcing layer reads one LiveBench release (the latest; the
methodology's viable-pool fallback arrives with the pool rules in G.2.4),
filters to open-weight models, resolves each through
`governance_service/model_mapping.yaml` to a pinned HuggingFace artifact, and
assigns the cheapest fitting GPU from the supported table (L40S, A100, H100,
H200) using exact weight bytes plus a config-derived KV-cache estimate under
the production SGLang memory fraction. Models without a mapping entry are
reported as unmapped, never guessed — add a mapping line to make one eligible,
or a `skip_reason` entry to record a model whose artifact is known to be
unresolvable.

Run one live pass locally:

```bash
python -m governance_service.freshness
```

The scheduled Mapping Freshness workflow (`.github/workflows/mapping-freshness.yml`)
runs the same check weekly and fails when an open-weight leaderboard model is
unmapped or the upstream data files no longer parse.

## Pool refresh (G.2.4)

A pool refresh turns one sourcing pass into an actual candidate pool under
the methodology's rules: blocklisted revisions are excluded (their slot
passing to the next eligible candidate), only vendor FP8 or full-precision
artifacts are eligible, every challenger must fit a single GPU, and one
model per family survives — with the incumbent a pool member by right,
exempt from every rule, and its family's challenger slot open to a
better-ranked successor. A release is viable only when at least two
challengers survive; the refresh walks back one release at a time until
one qualifies and otherwise records a no-viable-pool finding that leaves
the current pool standing.

Every refresh is persisted in full: the `pool_refreshes` row carries the
walk (each considered release with its challenger count, fallback reason,
and unmapped models), and `pool_refresh_candidates` holds every evaluated
candidate's rule outcome for every considered release. The standing
blocklist lives in `governance_service/model_blocklist.yaml` — curated by
hand like the model mapping, one entry per pinned revision that failed a
past round — and is mirrored into the `blocklist` table when a refresh
consumes it.

A refresh is triggered manually (the development and operations path;
scheduling arrives with round orchestration):

```bash
curl -X POST http://localhost:8002/api/governance/pool/refresh \
  -H "X-API-Key: $ADMIN_API_KEY"
```

The endpoint mirrors the dynamic-unl-scoring trigger contract: 202 with
the refresh id when started, 409 while another refresh holds the advisory
lock, 403 when `ADMIN_API_KEY` is unset or wrong. The refresh runs in a
background thread; watch progress in the service log or the
`pool_refreshes` row.

## Published refresh records (G.2.5)

Every completed refresh (viable pool or no-viable-pool finding) is
published automatically as a public record under
`records/pool-refreshes/<environment>/`: a canonical JSON document plus a
human-readable summary (see the README there for the format). Publication
runs inside the refresh flow itself — after persistence the service pins
the upstream LiveBench snapshot files to IPFS (primary node plus
best-effort Pinata replication) and commits both record files through the
GitHub Contents API, mirroring the dynamic-unl-scoring VL distribution
client.

Publication state lives on the refresh row: `publication_status` is
`PUBLISHED` (with `record_commit_urls`, and `snapshots_cid` when IPFS is
configured), `FAILED` (with `publication_error`, preserving whatever CID
or commit URLs already succeeded), or `SKIPPED` when
`RECORDS_GITHUB_TOKEN` is not configured — the local-development
default. Refreshes that fail before completion never attempt publication
and keep a NULL `publication_status`. A publication failure never
changes the refresh outcome or the standing pool.

## CI

GitHub Actions runs the test suite against a PostgreSQL 16 service container and builds the Docker image on every pull request and push to `main`. A separate scheduled workflow checks mapping freshness against the live LiveBench data weekly.
