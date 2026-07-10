# Contributing

## Workflow

- Work in feature branches and open pull requests against `main`.
- CI (tests against PostgreSQL + Docker build) must pass before merging.
- Environment branches (`devnet`, `testnet`) and deployment workflows arrive with roadmap step G.2.2; until then `main` is the only long-lived branch.

## Conventions

This service deliberately mirrors [dynamic-unl-scoring](https://github.com/postfiatorg/dynamic-unl-scoring) so both codebases stay navigable through the same patterns:

- Settings come from environment variables via `pydantic-settings` (`governance_service/config.py`); no configuration constants in code.
- Database schema changes are numbered SQL files in `migrations/`, applied in order by the startup migration runner. Never edit an applied migration; add a new one.
- Tests run against a real PostgreSQL database, not mocks of it — this repository's own rule, deliberately stricter than the scoring service's mock-based tests. New functionality lands together with its tests.
- The `docs/` directory is the public governance record; service code changes must not rewrite published records.

## Roadmap

The milestone plan for this service lives in the Dynamic UNL roadmap:
`dynamic-unl-scoring/docs/CurrentRoadmap.md`, Model Governance phase (steps G.2.x onward).
