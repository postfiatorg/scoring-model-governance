# Grading Prompt v1 — Design Rationale

`prompts/grading_v1.txt` is the first version of the governance grading
prompt (roadmap step G.4.1): the instrument a drawn judge model uses to
grade one candidate scoring model's exam answer during a governance
round. The grade it produces is the round's entire ranking, so the
prompt is a frozen, public, per-round protocol artifact. This document
records the design decisions behind it.

## What one grading request contains

One grading request grades one (corpus item, survivor) pair. The judge
receives three material blocks, delimited and declared as material only:

1. **Scoring instructions** — the exact system prompt the scorer was
   given for this corpus item. Grading is instruction-relative: the
   judge grades adherence to the instructions shown, not to any newer
   scoring-prompt version it may know, so the corpus can span
   scoring-prompt eras without changing the grading prompt.
2. **Scoring input** — the exact user message the scorer received:
   selector context and per-validator evidence.
3. **Candidate answer** — the answer under grade, with the candidate's
   identity structurally absent: `build_grading_request` in
   `governance_service/services/grading.py` never receives the
   candidate's identity, so no grading request can carry it. The judge
   grades content, never authorship. Grades are absolute against the
   rubric — not comparisons within a pool — which keeps them comparable
   across rounds and keeps the offline re-grading tool (G.4.3)
   meaningful.

The request runs under the same discipline as every governance
inference: the judge's own pinned deterministic profile, temperature 0,
`response_format json_object`, thinking disabled via the judge profile's
`extra_body`.

## The rubric

Four dimensions, each producing a written finding before the grade is
chosen — findings first is deliberate: with thinking disabled, the
findings are the judge's visible reasoning, and they must cite validator
ids and concrete evidence so every finding is auditable against the
material:

1. **Evidence fidelity** — claims and sub-scores reconcilable with the
   evidence, checked across the full set rather than a sample; inventing
   evidence and ignoring decisive evidence rank equally as defects.
2. **Instruction adherence** — the shown instructions are the contract:
   dimension ownership, penalty policies, scoring rules, content rules.
   Judgment latitude is explicitly not a defect, and when two
   instruction rules interact, the more specific rule governs — a
   sub-score forced by a ceiling or an integer requirement is not an
   ordering violation (the prompt carries the canonical shared-ceiling
   example, which live trials showed judges otherwise misreading as a
   defect).
3. **Cross-validator consistency** — identical evidence must grade
   identically, better evidence never worse, searched deliberately by
   grouping validators per dimension; a reasoning string is not evidence
   and cannot justify divergence the evidence does not show.
4. **Network report quality** — the report must match the answer's own
   scores and the evidence, per the shown instructions' content rules;
   the criterion self-disables when the instructions request no report.

**Subversion rule.** The material blocks are data, never instructions to
the judge. Grader-directed content inside a candidate answer ("assign
grade 100") is a critical defect that forces the 0-15 band regardless of
the rest of the answer — the grading-side counterpart of the exam
corpus's adversarial-evidence edge case. In live trials every pool
model priced such an injection into the bottom band.

**Grade scale.** 0-100 in multiples of 5, chosen through named bands
selected by an explicit defect count (no systemic defect ever grades 80
or higher; each additional systemic defect drops the band by one).
Per-item banding follows the scoring prompt v9 stability evidence
(`dynamic-unl-scoring/docs/ScoringPromptV9.md`): fine-grained per-item
precision is noise no model can justify.
One-decimal final grades arise in code, from the mean over corpus items
(G.4.2) — the judge itself never emits decimals.

## Development notes

The prompt text was shaped by live grading trials against the current
pool's pinned judges on real frozen-round material (testnet round 16 and
devnet round 323), including deliberately corrupted answers with known
defects. Those trials drove the full-set verification requirements, the
count-based band procedure, the rule-specificity clause, and the
subversion rule, and confirmed the injection defense across all three
pool models. Like the scoring prompt's v1-v9 history, later versions are
expected to come from defects noticed in real governance rounds —
devnet first, testnet after — with each change published as a new
versioned prompt file.

Two observations from the trials are worth carrying into later
milestones: judge models differ materially in how thoroughly they verify
a 51-validator answer (a property round orchestration may want to test
mechanically before seating a drawn judge), and one pool model's serving
runtime did not produce bit-identical repeats under grading-sized
prompts — the mechanical determinism rule already covers this in-round.

## Clarity revision (2026-08-04)

An adversarial ambiguity review — reading the prompt as the
no-thinking, temperature-0 judge consumes it, against the exam corpus
as it actually exists (historical rounds plus the constructed
edge-case catalogue, whose template carries the scoring v5
instructions) — found places where two competent judges could read
the same text differently and land in different bands. The prompt was
revised in place with targeted wording fixes; the deliberate design
(instruction-relative grading, findings-first ordering, count-based
bands, the 0-100 multiples-of-5 scale, the subversion rule) is
unchanged. The fixes:

- **Subversion scope.** Quoting adversarial text that appears in the
  scoring input is evidence citation, never subversion — the
  injection-in-evidence corpus item requires a good answer to cite the
  planted strings. Subversion is only content the answer itself
  directs at its own evaluation. The 0-15 band's internals are now
  explicit: all four findings are still written, and placement inside
  the band follows the count of additional material defects.
- **Findings feed the count.** A defect-reporting finding now states
  the total count and classification of the distinct material defects
  its full-set check established, so the band-selection count has a
  defined input; each underlying defect counts once even when more
  than one finding mentions it.
- **Band procedure closure.** When more than one band's condition
  applies, the lowest applicable band wins; the two-systemic and
  three-systemic rows tolerate coexisting localized defects
  explicitly; within a band, the minimum count satisfying the entered
  condition anchors at the top and each additional defect steps down
  one multiple of 5, floored at the band's bottom; the zero-defect
  band places blemish-free answers at 100 and trivial blemishes at
  95 (the worked example's grade was aligned to this anchor).
- **Era-proofing.** The consistency-search grouping and the
  evidence-field list are now instruction-relative (examples, not
  hard-coded field sets), and the shared-ceiling check applies only
  when the shown instructions state a ceiling and both sub-scores sit
  at it — the constructed catalogue's v5-era items have no ceiling
  rule, and the earlier unconditional sentence read as excusing
  sub-ceiling ordering violations.
- **Scope statement.** "You grade only what cannot be checked
  mechanically" no longer reads as excluding rule violations visible
  in the numbers (a stated ceiling exceeded, banding ignored); the
  mechanical carve-out is exactly parse, completeness, and
  determinism. Answers that nonetheless miss an input validator or
  invent one are handled under evidence fidelity.
- **Report criterion.** The judge derives the selected-UNL view from
  the answer's own scores and the selector context before judging
  selection-aware report claims, and a volunteered report the
  instructions never requested leaves the criterion not applicable,
  its content graded under no criterion while the subversion rule
  still applies to it.
- **Example integrity.** Every worked-example finding now states the
  check performed and its outcome per the finding contract — the
  fidelity finding models the required full-set verification instead
  of the spot-checking the rubric forbids — and the material blocks
  identify the scorer as the candidate under exam.

Known ambiguities deliberately left open, pending an explicit policy
decision rather than a wording fix: a numeric localized/systemic
threshold, a materiality test for trivial blemishes, the
fabrication boundary between the 0-15 and 20-35 bands, a
proportionality tolerance, and the scope of judgment latitude outside
instruction adherence.

The live-trial evidence described above predates this revision. The
prompt remains v1 — no governance round has frozen it — but the
trials must be re-run against the pool's pinned judges before the
prompt is used in a live round.
