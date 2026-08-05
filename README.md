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
├── request_template.json # Verbatim production model request (testnet round 15),
│                        # the structural template for constructed edge cases
├── _exam_modal_app.py   # Templated Modal app one exam candidate deploys as
├── api/
│   ├── _helpers.py      # Admin auth and refresh-lock preconditions
│   ├── health.py        # /health liveness endpoint
│   └── pool.py          # Public pool/refresh/blocklist/health reads + refresh trigger
├── clients/
│   ├── livebench.py     # Leaderboard data fetch, strict parsing, site-exact averaging
│   ├── huggingface.py   # Revision pinning, weight sizes, config, license/gating
│   ├── scoring_api.py   # Scoring-service rounds/input-package fetch + IPFS gateway fallback
│   ├── ipfs.py          # Snapshot pinning to the foundation IPFS node
│   ├── pinata.py        # Secondary snapshot replication
│   └── github_records.py # Record publication via the GitHub Contents API
├── models/
│   ├── candidates.py    # Candidate-sourcing data models
│   ├── pool.py          # Pool-refresh data models
│   └── runtime_profile.py # Candidate runtime profile, the adaptation rule's input
├── scoring/
│   ├── _vendor_source/  # Byte-identical dynamic-unl-scoring copies, pinned by content hash
│   ├── hashing.py       # Adapted canonical-hash rules (the vendored module needs xrpl)
│   └── parser.py        # Adapted production response parser (foundation import inlined)
└── services/
    ├── gpu_fit.py       # Dtype-aware cheapest-fit GPU assignment
    ├── candidate_sourcing.py # One auditable sourcing pass over a release
    ├── pool_refresh.py  # Pool rules, release fallback, refresh persistence
    ├── record_publisher.py # Record rendering, snapshot pinning, publication
    ├── corpus.py        # Exam corpus assembly: verified history + manifest
    ├── edge_cases.py    # Deterministic constructed edge-case catalogue
    ├── request_adaptation.py # Per-candidate request adaptation rule
    ├── candidate_profiles.py # Deployable profiles for the current pool
    ├── runtime_manager.py # Per-candidate Modal deployment lifecycle
    ├── exam_engine.py   # Exam execution: the corpus, three runs per item
    ├── disqualification.py # Mechanical pass/fail verdicts over stored runs
    └── grading.py       # Grading request derivation + judge defect schema
prompts/                 # Versioned governance grading prompts
migrations/              # Numbered SQL migrations, applied in order
records/                 # Published governance records (pool refreshes)
scripts/                 # check_vendor_freshness.py: vendored-code drift check
                         # exam_smoke_deploy.py: account-readiness smoke tool
                         # exam_live_validation.py: small real-workspace exam run
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
unresolvable. Every entry also declares its curated thinking-mode class
(`thinking: none | hybrid | always | unknown`), written from the model's
public chat template and validated against it by the freshness check.

Run one live pass locally:

```bash
python -m governance_service.freshness
```

The scheduled Mapping Freshness workflow (`.github/workflows/mapping-freshness.yml`)
runs the same check weekly and fails when an open-weight leaderboard model is
unmapped, the upstream data files no longer parse, or a curated
thinking-mode class contradicts the model's public chat template.

## Pool refresh (G.2.4)

A pool refresh turns one sourcing pass into an actual candidate pool under
the methodology's rules: blocklisted revisions are excluded (their slot
passing to the next eligible candidate), only vendor FP8 or full-precision
artifacts are eligible, only models whose thinking mode can be disabled
are eligible (production serves with thinking off), every challenger must
fit a single GPU, and one model per family survives — with the incumbent
a pool member by right,
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

## Pool API (G.2.6)

The service's public read surface — the endpoints the explorer consumes,
mirroring the dynamic-unl-scoring public API conventions:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/governance/pool` | Current pool from the latest completed refresh (404 before one exists) |
| `GET /api/governance/refreshes` | Refresh history, newest first, paginated with `limit`/`offset` |
| `GET /api/governance/refreshes/{id}` | One refresh's full audit: the release walk and every candidate's rule outcome |
| `GET /api/governance/blocklist` | The standing blocklist as consumed by refreshes |
| `GET /api/governance/health` | Pipeline-health signals (latest refresh outcome and age, record-publication state), distinct from the bare `/health` liveness probe |

## Exam corpus (G.3.1)

The corpus-assembly layer builds the frozen "question set" governance
rounds examine scoring-model candidates against. One assembly selects the
newest completed scoring rounds under `CORPUS_HISTORY_WINDOW` (default 12,
fewer when the environment's history is shorter), fetches each round's
frozen input package — scoring service HTTPS first, public IPFS gateway
second — and verifies every file against the package's recorded canonical
hashes before it can enter the corpus. Historical packages are referenced
by their existing CIDs and hashes, never re-pinned or copied.

Hash rules are reused, not reimplemented: `governance_service/scoring/`
vendors the canonical-hash source from dynamic-unl-scoring the same way
the validator sidecar vendors foundation code — a byte-identical copy
under `_vendor_source/` pinned by content hash, a runnable adaptation in
`hashing.py` (the vendored module needs xrpl, which this service does not
depend on), and the Vendor Freshness workflow that detects upstream drift
(warning on `main`, blocking on environment branches).

The constructed side is a versioned six-case edge-case catalogue
(`services/edge_cases.py`): byte-stable builders that emit synthetic
rounds in the exact production request format — substituting only the
validator array and selector-context values into the verbatim template —
covering the scoring prompt's penalty and judgment rules, the selector's
cutoff/overflow/churn boundaries, a fully degraded set, adversarial
instruction-like evidence, and a large-set format stress. The corpus
manifest binds both sides: historical items by CID and hash, constructed
items by canonical content hash, and the policy actually applied.

## Request adaptation (G.3.2)

Corpus requests embed the serving model's identity, so no other candidate
can replay them verbatim. `services/request_adaptation.py` is the frozen
derivation that re-addresses one corpus request to any candidate: it
rewrites exactly the profile-derived fields — the `model` identifier and
the `extra_body` chat-template settings — from the candidate's minimal
runtime profile (`models/runtime_profile.py`), leaving every other byte
untouched. The rule is a pure function; its tests prove the identity
property (adapting a request to its own embedded profile reproduces it
byte-for-byte) and the exclusivity property (adapting to another
candidate changes nothing but the declared fields), so any verifier can
reconstruct identical per-candidate requests from the frozen corpus and
profiles alone.

## Candidate runtime management (G.3.3)

Governance exams deploy every pool candidate on Modal on its pinned
deterministic profile. `services/runtime_manager.py` manages that
lifecycle with an idempotent ensure-deployed contract: apps are named by
candidate identity (never by round), the live app's own `profile` control
endpoint reports what it serves and reuse happens on a content-hash
match, drift or absence triggers a redeploy that replaces the app in
place, a verified warm-up proves the endpoint serves before anything
trusts it, and candidates that leave the pool are cleaned up. The
deployment target is `_exam_modal_app.py`, a templated Modal app adapted
from the validator sidecar's pattern; `services/candidate_profiles.py`
pins the current pool's deployable profiles — production's digest-pinned
SGLang image and deterministic serving arguments, thinking disabled
explicitly for every candidate with the evidence recorded per model.

Failures are classified two-sided: infrastructure problems (auth, quota,
billing, platform outages) raise `InfrastructureError` — retryable, never
round state — while a candidate's own failure to deploy or serve raises
`CandidateDeployError` carrying the structured evidence mechanical
disqualification requires; ambiguity fails toward infrastructure.
`scripts/exam_smoke_deploy.py` is the account-readiness tool: it deploys
one candidate through the manager, proves a real inference, and tears the
app down (see `docs/ExamAccountReadiness.md` for the recorded runs).

## Exam execution engine (G.3.4)

`services/exam_engine.py` is where the harness pieces become one flow: it
examines any list of candidate profiles — pool-size general; excluding a
drawn judge is round orchestration's concern — sequentially. Each
candidate is deployed on its pinned profile with verified warm-up, then
every corpus item is adapted to the candidate and sent three times
through the production scoring pattern (direct chat-completions request,
production per-request timeout). Only the model's message content
survives the client boundary — never the response envelope, whose
per-call identifiers would poison determinism comparisons — and every
answer is stored with the canonical content hash the scoring pipeline and
validator sidecars already agree on
(`canonical_json_hash({"raw_response": content})`), plus latency and
token measurements that are published but never ranked.

Results persist in `exam_runs` / `exam_outputs` (migration 005). An
interrupted run resumes without re-paying completed inferences.
Infrastructure failures abort the run as retryable; a candidate's own
serve failure is recorded as the structured disqualification evidence
the mechanical checks consume. `scripts/exam_live_validation.py` runs a
two-item, three-run fragment against one real deployed candidate,
applies the mechanical disqualification checker to the stored rows, and
reports the determinism result and verdict
(see `docs/ExamLiveValidation.md` for the recorded run).

## Mechanical disqualification (G.3.5)

`services/disqualification.py` applies the methodology's three mechanical
pass/fail rules to stored exam runs as pure, deterministic, idempotent
computation: every stored answer must parse with the unmodified
production response parser (vendored in `scoring/_vendor_source/`,
pinned by content hash, drift-checked by the Vendor Freshness workflow,
runnable as `scoring/parser.py`); all repeat runs of every corpus item
must carry one identical canonical response hash; and the candidate must
have deployed and served on its pinned profile, decided by the run's
terminal status and its structured serve-failure evidence. Parsing
consumes each item's validator identity map — historical items carry
theirs in the frozen input package, constructed edge cases derive
synthetic maps from their own validator ids.

The verdict and per-rule evidence persist on the exam run (migration
006), in the shape the published round record consumes; recomputation
always overwrites with the identical result. Booking disqualified
revisions into the standing blocklist belongs to round orchestration,
never this layer.

## Grading prompt and judge defect schema (G.4.1-G.4.2)

Grading follows the G.4 checker/judge/formula split: every check with
a closed-form right answer belongs to the mechanical grading checker
(G.4.4), the per-item grade is computed by the versioned grade
formula (G.4.5), and the drawn judge owns only the language checks.
`prompts/grading_v2.txt` is the current versioned grading prompt: the
judge-independent instrument a drawn judge examines exam answers
with, one (corpus item, survivor) pair per request. The judge
receives the item's frozen scoring instructions, the scoring input,
and one candidate answer with the candidate's identity structurally
absent (`services/grading.py` never receives it), and emits
structured defect objects under the judge defect schema — four
judge-owned kinds (false_claim, ignored_evidence, report_mismatch,
subversion), each citing validator ids, the verbatim quote, and the
contradicting evidence, with every section stating an explicit
outcome — no grade, no counts, no severity. `parse_judge_output`
enforces the schema strictly, and repeat runs will be compared with
the same canonical content-hash rule the exam pipeline uses
(deterministic judge execution, G.4.3).

The v1 prompt (retained as `prompts/grading_v1.txt`) was shaped by
live grading trials on real frozen-round material and emitted banded
grades itself; the split carved its mechanical checks and band
procedure out into code. Later prompt versions follow the scoring
prompt's path — defects noticed in real rounds drive each revision,
devnet first. Design rationale: `docs/GradingPromptV2.md` (current),
`docs/GradingPromptV1.md` (v1 history and the clarity revision that
drove the split).

## CI

GitHub Actions runs the test suite against a PostgreSQL 16 service container and builds the Docker image on every pull request and push to `main`. A separate scheduled workflow checks mapping freshness against the live LiveBench data weekly, and the Vendor Freshness workflow compares the vendored dynamic-unl-scoring copies against upstream on pushes, pull requests, and a weekly schedule.
