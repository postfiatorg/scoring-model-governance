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
├── scoring_rules.yaml   # Curated per-scoring-prompt-version checker rules
├── api/
│   ├── _helpers.py      # Admin auth and advisory-lock preconditions
│   ├── health.py        # /health liveness endpoint
│   ├── pool.py          # Public pool/refresh/blocklist/health reads + refresh trigger
│   └── rounds.py        # Rounds API: list/detail, package + record routes, config,
│                        # and the admin-guarded manual round trigger
├── clients/
│   ├── pftl.py          # PFTL chain client: publisher wallet, typed memo submission
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
    ├── grading.py       # Grading request derivation + judge defect schema
    ├── checker.py       # Mechanical grading checker: closed-form defects
    ├── grade_formula.py # Versioned formula: defect lists -> grades
    ├── grading_engine.py # Judge execution: pairs, repeats, verdicts
    ├── regrading.py     # Offline chain: frozen material -> grades
    ├── orchestrator.py  # Governance round state machine + stage pipeline
    ├── scheduler.py     # Round cadence scheduler + advisory locking
    ├── round_package.py # Frozen round package: assembly, pinning, persistence
    ├── announcement.py  # Governance memo formats + the on-chain announce stage
    ├── judge_draw.py    # Ledger-randomness judge draw + redraw ordering
    ├── exam_stage.py    # Exam stage: frozen material into the engine, links, verdicts
    ├── final_publication.py # Withholding hold + record pin, receipt, repo publish
    └── decision.py      # Margin-gated verdict, ledger tie-break, blocklist writing
prompts/                 # Versioned governance grading prompts
migrations/              # Numbered SQL migrations, applied in order
records/                 # Published governance records (pool refreshes, rounds)
scripts/                 # check_vendor_freshness.py: vendored-code drift check
                         # exam_smoke_deploy.py: account-readiness smoke tool
                         # exam_live_validation.py: small real-workspace exam run
                         # exam_stage_live_validation.py: wired-stage real run
                         # grading_live_validation.py: real-workspace grading run
                         # regrade.py: offline re-grading over frozen material
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
past round — and is appended into the `blocklist` table when a refresh
runs. Since G.5.6 the table is the effective blocklist a refresh filters
by: governance rounds book their disqualifications there directly, so a
verifier reproducing a refresh needs the curated file plus the round
records' entries.

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
(G.4.3), the per-item grade is computed by the versioned grade
formula (G.4.4), and the drawn judge owns only the language checks.
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
(deterministic judge execution, G.4.5).

The v1 prompt (retained as `prompts/grading_v1.txt`) was shaped by
live grading trials on real frozen-round material and emitted banded
grades itself; the split carved its mechanical checks and band
procedure out into code. Later prompt versions follow the scoring
prompt's path — defects noticed in real rounds drive each revision,
devnet first. Design rationale: `docs/GradingPromptV2.md` (current),
`docs/GradingPromptV1.md` (v1 history and the clarity revision that
drove the split).

## Mechanical grading checker (G.4.3)

`services/checker.py` is the split's code half: pure, deterministic
defect detection over one (corpus item, parsed answer) pair —
identical-evidence sub-score divergence per dimension, ordering
violations where strictly better evidence scored strictly worse (ties
excused exactly where an era rule forces them), the era's numeric
rules (the worst-window consensus ceiling, multiples-of-5 banding),
and the structural checks (missing or invented validator entries).
Checker defects mirror the judge defect objects' shape with
checker-exclusive kinds, so the grade formula concatenates the two
lists without reconciliation.

Because grading is instruction-relative and the corpus spans
scoring-prompt eras, each item's rules come from
`governance_service/scoring_rules.yaml` — one hand-curated row per
published scoring-prompt version, keyed by the SHA-256 of the exact
instructions text embedded in the frozen request, and fail-closed:
unknown instructions and mis-curated evidence fields are hard errors,
never guesses. The Vendor Freshness workflow verifies every row's
pinned hash against the upstream prompt file, and the v5 row is
validated against the vendored real production request in the test
suite. Curation rationale per row: `docs/MechanicalGradingChecker.md`.

## Deterministic judge execution (G.4.5)

`services/grading_engine.py` runs a drawn judge for real: deployed
through the same idempotent Modal runtime manager candidates use
(judges are pool members), one grading request per (corpus item,
survivor answer) pair built by the frozen derivation, each sent
`REPEAT_COUNT` times through the production scoring pattern, every
answer stored with its canonical content hash in `grading_runs` /
`grading_outputs` (migration 007). Runs are idempotent per (judge
profile, material) and resume after interruption without re-paying
completed inferences; failures keep the exam engine's two-sided
taxonomy, with the judge's own failures persisted as the structured
evidence the redraw rule (G.5) consumes. `judge_mechanical_verdict`
computes the judge's mechanical bar over stored rows: every output
parses under the defect schema and every pair's repeats carry one
identical hash.

`services/regrading.py` is the offline re-grading chain — production
answer parser, mechanical checker, defect-schema parser, grade formula
— from frozen material to per-item grades and the final grade with
complete receipts, runnable by anyone via `scripts/regrade.py`
(no database, no Modal, no network), so judge rotation never erases
cross-round comparability. `scripts/grading_live_validation.py`
exercises the whole chain against one real deployed pool model; the
recorded run lives in `docs/GradingLiveValidation.md`.

## Grade formula (G.4.4)

`services/grade_formula.py` is where every grade number comes from:
the versioned pure function (`GRADE_FORMULA_VERSION = 1`) from the
checker's and the judge's defect lists to the per-item grade, banded
0-100 in multiples of 5. Same-kind, same-dimension defects aggregate
before anything counts; a merged defect touching 3+ validators is
systemic; band selection is a count with lowest-band-wins (a single
evidence defect covering half the validator set forces 20-35, and any
subversion defect forces 0-15); placement anchors at the band top and
steps down 5 per additional distinct defect, floored at the band
bottom; zero defects grade 100 flat. A survivor's final grade is the
unweighted mean of its per-item grades, 0-100 with one decimal — the
resolution the incumbent-replacement margin compares. Every result
carries receipts (the aggregated defects, classifications, counts,
and band decision), and the thresholds are named, versioned
constants: changing one is a new formula version. Design and
constants rationale: `docs/GradeFormulaV1.md`.

## Round state machine and scheduler (G.5.1)

`services/orchestrator.py` is the persisted state machine that will drive
complete governance rounds. A round lives in `governance_rounds`
(migration 008) and moves through completion-boundary states — `CREATED`,
`FROZEN`, `ANNOUNCED`, `JUDGE_DRAWN`, `EXAMINED`, `GRADED`,
`AWAITING_COMMIT_CLOSE`, `DECIDED` — to `COMPLETE`, `FAILED`, or
`ABANDONED`. Restart semantics follow the methodology's freeze contract:
a round interrupted before its freeze completed published nothing and is
abandoned by startup cleanup, while any post-freeze round resumes from
its persisted state, because frozen inputs are content-pinned and the
exam and grading engines resume idempotently — a governance exam is hours
of GPU work and is never discarded on a service restart. A parked round
(`AWAITING_COMMIT_CLOSE`) is released only once its recorded
`commit_closes_at` has passed — the fail-closed output-withholding hold.
Rounds never overlap: a new round starts only when no round is active.
The stage handlers themselves land with the remaining G.5 steps; until a
stage exists, a triggered round fails explicitly with a
`StageNotImplemented` message naming the milestone that delivers it. Exam
and grading runs carry a nullable `round_id` linking them to the
governance round that paid for them; runs reused across rounds keep the
round that produced them.

`services/scheduler.py` adapts the scoring service's scheduler
discipline: the persisted `governance_round_schedule.next_due_at`
advances by whole cadence periods at scheduled round start (a failed
round consumes its slot — the manual trigger is the recovery path), a
PostgreSQL advisory lock (99201) prevents concurrent rounds, and every
tick also abandons interrupted pre-freeze rounds, resumes interrupted
post-freeze rounds, and publishes parked rounds whose commit window
closed. Unlike the scoring scheduler, a fresh install seeds the first
round one cadence out instead of firing immediately — the first round of
a new environment should be a deliberate admin trigger. Cadence and
timing come from `ROUND_CADENCE_DAYS` (default 30),
`SCHEDULER_CHECK_INTERVAL_SECONDS` (300), and
`SCHEDULER_STARTUP_DELAY_SECONDS` (300).

A round is triggered manually with an explicit schedule choice —
`reanchor=true` resets the next automated round to one cadence from now,
`reanchor=false` leaves the schedule untouched:

```bash
curl -X POST "http://localhost:8002/api/governance/rounds/trigger?reanchor=true" \
  -H "X-API-Key: $ADMIN_API_KEY"
```

The endpoint mirrors the dynamic-unl-scoring trigger contract: 202 when
started, 400 without the `reanchor` choice, 409 while a round holds the
advisory lock or an earlier round is still active, 403 when
`ADMIN_API_KEY` is unset or wrong.

## Freeze and IPFS publication (G.5.2)

`services/round_package.py` is the freeze stage — the moment a round
becomes tamper-proof. It loads the maintained pool from the newest
completed refresh and maps every member to its deployable runtime
profile, enforcing the methodology's eligibility rule (the incumbent plus
at least two challengers, with matching revision pins) — an ineligible
pool fails the round with the evidence recorded. It then assembles the
frozen package: the corpus manifest (historical rounds by their existing
`input_package_cid`s, never re-pinned) and the constructed edge cases
pinned fresh, the pool pins with full runtime profiles, the grading
artifacts (grading prompt v2, the judge defect schema, the checker rules
table, grade formula v1 constants), the adaptation rule (versioned as
`ADAPTATION_RULE_VERSION`), and the round parameters — repeat count,
incumbent margin, commit/reveal window durations
(`ROUND_COMMIT_WINDOW_SECONDS` / `ROUND_REVEAL_WINDOW_SECONDS`,
conservative defaults finalized at the pre-round rehearsal), the
judge-draw procedure specification implemented at G.5.4, and the hash-set
contract implemented at G.6. The announcement formats join the package at
G.5.3 with the governance memo types.

The package follows the scoring input-package convention: `bundle.json`
carries `package_kind: "governance_round"` and per-file canonical
sha256es, and the package hash is the canonical hash of the bundle —
verifiers check it exactly the way they check scoring input packages.
Publication reuses the pin-with-fallback contract (foundation IPFS node,
Pinata replication by CID, Pinata direct upload as the write fallback)
and fails closed when no backend succeeds. Every package file is
persisted to `governance_round_artifacts` (migration 009) and served over
HTTPS at `GET /api/governance/rounds/{round}/package` (the bundle) and
`GET /api/governance/rounds/{round}/package/{path}` — the
gateway-independent side of the fetch-with-IPFS-fallback contract
sidecars use.

## On-chain publishing (G.5.3)

`services/announcement.py` makes a frozen round public and datable on the
PFT Ledger. The `_announce` stage submits a governance announcement memo
from the foundation publisher wallet — the same account scoring-round
announcements publish from — carrying the network, round number, package
CID and hash, and the absolute commit/reveal window timestamps, derived
at emission by the scoring discipline: anchored at validated-ledger close
time (service UTC as the logged fallback), with the commit window never
opening before the freeze. The governance memo types live in their own
versioned namespace (`pf_governance_round_announcement_v1`; the
round-close `pf_governance_round_receipt_v1` format is defined here and
emitted with the final record at G.5.5), MemoData is the canonical JSON
bytes of the payload, and the format specification is itself part of the
frozen package (`round/announcement_format.json`) so verifiers check the
on-chain memo against the frozen contract.

The announcement transaction's validated ledger index is persisted
(migration 010) as the anchor the frozen judge-draw procedure derives its
drawing ledger from, and `commit_closes_at` is recorded — the value the
withheld-publication release gates on. `clients/pftl.py` is the adapted
scoring PFTL client (publisher wallet from `PFTL_WALLET_SECRET`, typed
memo Payments, validated-ledger reads), with one divergence: submission
also returns the validated ledger index. Configuration: `PFTL_RPC_URL`,
`PFTL_WALLET_SECRET`, `PFTL_MEMO_DESTINATION`, `PFTL_NETWORK_ID`.

## Judge draw (G.5.4)

`services/judge_draw.py` implements the draw procedure the round package
froze at G.5.2: the drawing ledger is the announcement transaction's
validated ledger index plus the frozen offset (10); its hash, read as a
big-endian integer, modulo the challenger count indexes the challengers
sorted ascending by `hf_repo` in Unicode codepoint order. The challenger
list comes from the frozen package artifacts (`pool/candidates.json`),
never the live pool, and the incumbent is excluded by construction. The
`_draw_judge` stage (`ANNOUNCED → JUDGE_DRAWN`) waits within a bounded
window for the drawing ledger to validate (by construction it closes
under a minute after the announcement), then persists the drawn judge
and the drawing ledger's index and hash (migration 011).

The draw is a pure function of public data — anyone recomputes the
identical judge from the on-chain announcement and the frozen package —
and it is re-run-safe two ways: a round with a persisted draw returns it
without touching the chain, and a recomputation is deterministic anyway.
The deterministic redraw ordering (`next_judge`: cyclic successor in
draw order, skipping failed judges, `None` on exhaustion — the round is
then abandoned) lives here as a pure function; the stages that detect a
judge's mechanical failure invoke it when they land.

## Output withholding and final publication (G.5.5)

`services/final_publication.py` enforces the round's output-withholding
discipline and closes it with the publication. Nothing a round produces
becomes public before its commit window closes — sidecars must commit to
results they computed themselves, so early publication would let a
commitment echo the foundation's outputs. The hold half parks a graded
round in the fail-closed `AWAITING_COMMIT_CLOSE` state, refusing to park
a round with no recorded commit close (a NULL there would never
release); the state machine's release gate frees it once the announced
window has passed.

The publication half runs after release and the decision, each step
idempotent from its persisted identity (migration 012): the complete
record — round identity and anchors, exam runs with raw outputs,
disqualification verdicts, grading outputs; the decision section joins
with G.5.6 — is bundled under the frozen-package manifest convention and
pinned with the shared pin-with-fallback contract; the round-close
receipt memo (`pf_governance_round_receipt_v1`, carrying the final
record CID) is emitted from the publisher wallet with the announcement's
re-run safety; and the human-readable round record — summary plus the
CIDs pointing at the pinned bundle, never the raw outputs themselves —
is committed to `records/rounds/{environment}/` via the GitHub records
client (skipped, and recorded as skipped, when no records token is
configured).

## Decision engine (G.5.6)

`services/decision.py` turns a round's evidence into its verdict — pure
arithmetic over persisted data, so any verifier recomputes the identical
result. The rules are the methodology's: the highest-graded surviving
challenger replaces the incumbent only when it beats it by the frozen
replacement margin (meeting the margin exactly counts as beating by it);
a mechanically disqualified incumbent loses that protection and the best
survivor wins outright; with no survivor at all the incumbent keeps
serving by necessity and the condition is logged as a production alarm.
The explicit incumbent-retained verdict is always recorded. Grade ties
between challengers are broken by the round's drawing-ledger hash
through the judge draw's modulo mapping — public before any grade
existed, so the tie-break was fixed before a tie could be known, and no
challenger is favored by its name.

The incumbent identity and the margin come from the frozen package
artifacts, never live configuration. Disqualified revisions are booked
into the standing blocklist with the round as their reference, and pool
refreshes now filter by the blocklist table — the curated file syncs
into it append-only, and rounds write their entries there directly, so
a round's disqualification takes effect at the next refresh without a
hand edit. The verdict, winner, and full rationale persist on the round
(migration 013, which also adds the per-candidate `final_grade` the
grading-stage wiring will populate) and join the final record bundle and
the repository record document.

## Rounds API (G.5.7)

`api/rounds.py` is the read-only surface sidecars and the explorer
consume:

| Endpoint | Returns |
|----------|---------|
| `GET /api/governance/rounds` | Round list, newest first, shared pagination shape |
| `GET /api/governance/rounds/{round}` | One round's full persisted identity |
| `GET /api/governance/rounds/{round}/package[/{path}]` | Frozen-package manifest / file (public at announcement) |
| `GET /api/governance/rounds/{round}/record[/{path}]` | Final-record manifest / file (withheld until commit close) |
| `GET /api/governance/config` | The participation surface for sidecars and the explorer |

The final-record routes re-assemble the record bundle deterministically
from persisted round data on demand — nothing is stored twice — and
enforce the output-withholding rule at the API boundary through the same
`commit_window_closed` predicate publication gates on: a round's outputs
are refused (403) until its recorded commit window has closed. The
frozen-package routes from G.5.2 stay as they are: freeze-time artifacts
are public by design the moment the round is announced.

`GET /api/governance/config` is the participation surface, mirroring the
scoring service's verifier configuration endpoint: the governance memo
types, the foundation publisher address (null when no wallet is
configured), the commit/reveal window durations, the draw ledger offset,
the incumbent margin, the round cadence, and the protocol version — what
a sidecar needs to find and decode governance memos, and what the
explorer renders without hardcoding constants.

## Exam stage wiring (G.5.8)

`services/exam_stage.py` runs the G.3 exam engine inside a governance
round (`JUDGE_DRAWN → EXAMINED`). Everything it consumes comes from the
round's frozen package artifacts, never live state: candidate profiles
from `pool/candidates.json` (each verified against its recorded profile
hash), the repeat count from `round/parameters.json`, and the corpus
from `corpus/manifest.json` — historical items re-fetched by their
pinned CIDs and verified against the recorded package hashes exactly as
corpus assembly verified them, constructed cases read from the frozen
artifacts and checked against the manifest's content hashes. Every pool
member except the drawn judge — the incumbent included — sits the exam,
and mechanical disqualification persists each run's verdict; validator
identity maps come from the historical packages'
`inputs/validator_map.json` and, for constructed cases, from the
synthetic derivation.

The runs answering for a round are linked in
`governance_round_exam_runs` (migration 014): exam runs are reusable
across rounds and a reused terminal run keeps the `round_id` that paid
for it, so the decision engine and the final record read the round's
evidence through the links, never through `exam_runs.round_id`. When a
round retriggered after an infrastructure failure freezes an identical
corpus and pool, it therefore reuses every already-paid inference —
verdicts included — and still decides over a complete evidence set.

Failures keep the engine's two-sided taxonomy: a candidate's own
failure becomes its disqualification evidence and the exam moves on; an
infrastructure failure fails the round explicitly, with the manual
trigger as the recovery path. `scripts/exam_stage_live_validation.py`
runs the wired stage for real — a fabricated local round over a
two-item corpus (one live-fetched historical round, one constructed
case) with one deployed candidate — and proves material reconstruction,
verdicts, links, and full inference reuse on a re-run; the recorded run
lives in `docs/ExamStageLiveValidation.md`.

## CI

GitHub Actions runs the test suite against a PostgreSQL 16 service container and builds the Docker image on every pull request and push to `main`. A separate scheduled workflow checks mapping freshness against the live LiveBench data weekly, and the Vendor Freshness workflow compares the vendored dynamic-unl-scoring copies against upstream on pushes, pull requests, and a weekly schedule.
