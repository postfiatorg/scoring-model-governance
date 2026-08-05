# Mechanical Grading Checker — Design and Curation Record

`governance_service/services/checker.py` is the checker of the G.4
checker/judge/formula split (roadmap step G.4.3): pure-code,
deterministic defect detection over one (corpus item, parsed candidate
answer) pair. It owns every defect kind with a closed-form right
answer; the judge is never asked about them, so the two defect lists
the grade formula (G.4.4) will consume cannot overlap by construction.

## Era resolution

Grading is instruction-relative, and the exam corpus spans
scoring-prompt eras, so the checker first resolves which version's
rules govern an item — by the SHA-256 of the exact scoring-instructions
text embedded in the frozen request, never by metadata. The hash keys a
row in `governance_service/scoring_rules.yaml`. Resolution is
fail-closed twice:

- an item whose instructions match no row raises
  `UnknownScoringVersionError` — curating the missing row is the fix,
  never guessing;
- a rules feature that no validator entry carries raises
  `CheckerError` — a mis-curated field name must never silently make
  every validator look identical.

The Vendor Freshness workflow machine-validates every row: each pinned
hash must equal the hash of the upstream prompt file's rendered system
section, so a curation typo or upstream rewrite surfaces in CI. The v5
row is additionally validated against real production material in the
test suite: the vendored testnet round 15 request resolves to it.

## The checks

- **Equality** (`inconsistent_scores`): per dimension, group validators
  by the row's evidence features; identical evidence with divergent
  sub-scores is a defect. Validators missing any of the dimension's
  features are excluded from that dimension's comparisons — missing
  evidence is judgment territory, not arithmetic.
- **Ordering** (`ordering_violation`): a validator whose compared
  evidence is strictly better (no feature worse, at least one better)
  must score strictly higher. A tie is excused exactly when the better
  validator cannot legally score higher: at the scale top, or at its
  consensus ceiling when the era states one — the cap working is never
  a violation.
- **Numeric rules** (`ceiling_exceeded`, `banding_violation`): the
  era's stated arithmetic — the worst-window consensus ceiling (floored
  whole number) and multiples-of-5 banding.
- **Structural** (`missing_validator`, `invented_validator`): input
  validators absent from the parsed answer, and answer entries matching
  no input validator (surfaced by the vendored production parser).

Defects mirror the judge defect objects' shape — kind, the validators
concerned, and a details string carrying the numbers — and the output
tuple is deterministically ordered, so re-running a check reproduces it
exactly.

## Per-version curation

A row enables only checks the version's own text states crisply; prose
rules (guide bands, "where appropriate" penalties, judgment latitude)
never enter a row and remain the judge's. The current rows:

- **v5** — the constructed edge-case catalogue's era. States no
  equality, ordering, ceiling, or banding rule; every dimension's
  guidance is prose. Structural checks only. This is deliberate: a
  v5-era answer with divergent identical-evidence scores is a
  reconcilability question for the judge, not a mechanical defect,
  because v5 never promised otherwise.
- **v8** — states the same general within-dimension
  identical/strictly-better sentence as v9, consensus equality and
  strict ordering on agreement evidence, and diversity
  equality/ordering by observable endpoint concentration (peers
  sharing the validator's country and AS name, counted across the
  entries). Its software and identity dimension texts are
  byte-identical to v9's, so those equality rows carry over.
  Reliability still mixes accountability prose ("where appropriate")
  and never enumerates its evidence, so it stays judge-owned; no
  ceiling, no banding.
- **v9** — the full current set: worst-window consensus ceiling,
  multiples-of-5 on the four non-consensus dimensions,
  operational-evidence-only reliability, concentration-block diversity
  (precomputed counts; a provider_family of "unknown" is an unresolved
  endpoint), and identity equality on the accountability fields.
  Software and identity ordering stay out: version recency and
  accountability comparisons are hedged prose, not closed-form.

Two stated rules are deliberately not encoded. v9's "when only the
30-day window is degraded and the recent windows are clean, score at
the ceiling" pins an exact value, but "clean" and "degraded" are not
crisply defined, so enforcing it would guess a threshold — it stays
with the judge. And the structural kinds (`missing_validator`,
`invented_validator`) cannot fire for exam survivors — mechanical
disqualification already requires complete, error-free parses — they
exist as defense-in-depth for the offline re-grading tool (G.4.5),
which checks material no disqualification pass has filtered.

Row changes are protocol changes: review them like prompt revisions.
A future scoring-prompt version needs one new row before its rounds
can enter a governance exam — until then the checker fail-closes with
the missing row named.

## Evidence-format notes

Feature extraction matches the production prompt builder's entry
rendering: country lives nested under `geolocation`, the AS name under
`asn`, `provider_family` is flat, and the v9 concentration block is
the builder's list-of-objects shape (`provider_families`, `countries`,
`unresolved_endpoints`). Older flat fields are accepted where real
material carries them. The first real v9-era exam run validates these
shapes against live material; any mismatch fail-closes via the
feature-presence guard rather than producing false defects.
