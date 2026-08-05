# Grading Prompt v2 — Design Rationale

`prompts/grading_v2.txt` is the governance grading prompt under the
G.4 checker/judge/formula split (roadmap steps G.4.2–G.4.5 in
`dynamic-unl-scoring/docs/CurrentRoadmap.md`): the drawn judge stops
being examiner and accountant in one, and becomes only the examiner.
Its output is a set of structured defect objects under the judge
defect schema (`governance_service/services/grading.py`); every
mechanically decidable check moves to the mechanical grading checker
(G.4.3), and the grade itself is computed by the versioned grade
formula (G.4.4). The judge never emits a number.

## Why the split

The v1 prompt asked one no-thinking, temperature-0 model to verify a
~50-validator answer on four rubric dimensions, count its own defects,
and execute a band-selection procedure — all compressed into four
short findings and one grade. The clarity revision of v1 (see
`docs/GradingPromptV1.md`) made the text as unambiguous as prose can
be, but two structural weaknesses remained: the heavy verification
work was invisible (a judge can write "full-set check found none"
without having done it), and the grade arithmetic sat exactly where a
no-thinking model is weakest. Both have the same fix: give every
check with a closed-form right answer to code, and confine the judge
to what only language understanding can decide.

## The division of labor

Checker-owned (never asked of the judge; shipped as
`governance_service/services/checker.py`, G.4.3): identical-evidence
sub-score divergence, ordering violations, the scoring-prompt
version's numeric rules (ceilings, banding, required penalties), and
structural failures (missing or invented validator entries). The
prompt tells the judge explicitly that emitting a defect of an
unowned kind is a contract violation — the exclusivity is what lets
the grade formula concatenate the two defect lists without any
same-defect reconciliation.

One v1 rule dissolves in the split rather than moving: the
generic-reasoning consistency rule (identical boilerplate across
materially different profiles). Its load-bearing half — divergence
that no evidence supports — is checker-owned by construction, and a
reasoning string that misdescribes its own validator's evidence is
already a fidelity kind; boilerplate that stays truthful is not a
defect on either list.

Judge-owned, the four kinds of the defect schema:

1. **false_claim** — a factual claim in the answer's reasoning that
   the scoring input contradicts.
2. **ignored_evidence** — decisive evidence the answer treats as
   absent, in its text or in its sub-scores: a near-perfect sub-score
   irreconcilable with plainly degraded evidence is this kind even
   when the reasoning string stays silent. Reconcilability under the
   instructions' prose rules is judgment and belongs to the judge; a
   closed-form numeric rule the instructions state stays
   checker-owned.
3. **report_mismatch** — the round-level report contradicting the
   answer's own scores, the evidence, or the shown instructions'
   content rules.
4. **subversion** — grader-directed content anywhere in the answer;
   quoting adversarial text from the scoring input remains citation,
   never subversion.

## The output contract

Objects only — no prose findings. Three sections
(`evidence_fidelity`, `network_report_quality`, `subversion`), each
carrying an explicit `outcome` and its `defects`. `none_found` is a
statement that the full-set check ran and found nothing, so an
absent check is a parse failure rather than a silent omission;
`network_report_quality` alone may be `not_applicable` (no report
requested, or a volunteered report — whose content is examined under
no check while the subversion rule still applies to it). Each defect
carries its kind, the cited validator ids (required for the two
fidelity kinds), the verbatim quote from the answer, and an
explanation naming the contradicting evidence values — the defect is
auditable against the material without re-deriving the analysis.

No grade, no counts, no severity: classification
(localized/systemic), counting, banding, and the final per-item grade
belong to the grade formula, where the thresholds are declared
constants rather than prose.

The parser (`parse_judge_output`) is deliberately strict — exact
keys, kind-per-section enums, outcome/defects consistency, no repair
— and repeat stability stays byte-level: the canonical content-hash
rule the exam pipeline already applies to answers covers judge
outputs unchanged. Quote verbatim-ness is not parse-checkable (the
parser never sees the answer); the re-execution tooling that grades
from stored material verifies quote membership there, keeping the
auditability claim executable.

## What carried over from v1

The frame the split does not change: structural anonymity (the
builder never receives the candidate's identity), the three material
blocks with the scorer identified as the candidate, material-is-data,
instruction-relative judgment (the shown instructions are the
contract; judgment latitude is not a defect), full-set verification
demands for the language checks, the selected-UNL derivation before
judging selection-aware report claims, and the subversion rule with
its quoting exemption. The output budget grows to 8192 tokens —
defect lists on a badly flawed answer run long — still half of
production scoring's 16384.

## Standing of this version

Like v1, v2 is expected to evolve v2-vN from defects noticed in real
governance rounds, devnet first. The v1 live-trial evidence does not
transfer: trials against the pool's pinned judges must be re-run on
the v2 contract before the prompt is used in a live round. The first
live exercise of the v2 contract is recorded in
`docs/GradingLiveValidation.md` (one pool judge, the two-item
fragment, three repeats, end-to-end grades).
