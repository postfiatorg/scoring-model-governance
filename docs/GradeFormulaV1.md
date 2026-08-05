# Grade Formula v1 — Design and Constants Record

`governance_service/services/grade_formula.py` is the last stage of
the G.4 checker/judge/formula split (roadmap step G.4.4): the pure,
versioned function from the mechanical checker's and the drawn judge's
defect lists for one (corpus item, survivor) pair to the per-item
grade, and from a survivor's per-item grades to its final grade — the
unweighted mean, 0-100 with one decimal, the resolution the
incumbent-replacement margin compares. The judge never emits a number;
this module is where every number comes from.

## The procedure

1. **Aggregate.** Same-kind, same-dimension defects merge into one
   distinct defect (validator sets union), so one underlying problem
   reported many ways — the checker emits ordering violations as
   pairs — counts once. Checker-owned and judge-owned kinds are
   exclusive by construction, so the two lists concatenate with no
   same-defect reconciliation across them.
2. **Classify.** A merged defect touching
   `SYSTEMIC_VALIDATOR_THRESHOLD` (3) or more validators is systemic;
   otherwise localized. Report- and answer-level defects with no cited
   validators are localized by construction.
3. **Select the band** — a count, not an impression, with the lowest
   applicable band winning:
   - no defects → **100**, flat;
   - 1-2 localized, no systemic → **80-90**;
   - exactly one systemic, or 3+ localized → **60-75**;
   - exactly two systemic → **40-55**;
   - 3+ systemic, or any single merged evidence defect (`false_claim`
     or `ignored_evidence`) covering `ACROSS_SET_FRACTION` (half) of
     the validator set → **20-35**;
   - any subversion defect → **0-15**, regardless of everything else.
4. **Place.** Anchor at the band top for the minimum count of the
   condition met; step down one multiple of 5 per additional distinct
   defect; floor at the band bottom. When more than one of the
   selected band's conditions is met, the lowest resulting grade
   stands.

Every result carries receipts: the aggregated defects, each one's
classification and source count, the counts, and the band decision —
a published grade is never a bare number.

## What changed in the translation from prose

The band table and procedure come from the clarity-revised grading
prompt v1, whose in-prompt arithmetic this module replaces. Three
prose rules resolved into constants or dissolved:

- **The localized/systemic boundary** became the declared
  3-validator threshold on merged defects — prose said "one or a few,
  no pattern" versus "a repeated pattern or a class of validators".
- **"Evidence contradicted or ignored across the set"** became the
  declared half-the-set coverage trigger on a single merged defect of
  the evidence-fidelity kinds only — a set-wide formatting or
  ordering defect is one systemic defect on the normal ladder, never
  an across-the-set evidence failure.
- **The 95/100 "trivial blemish" split and the "wholesale
  fabrication" 0-15 row dissolved**: neither the checker nor the
  judge emits a "blemish" or a "fabrication" object. Zero defects
  grades 100 flat, and set-wide fabrication lands in 20-35 through
  the across-the-set trigger — the 0-15 band is subversion's alone.

## Version discipline

`GRADE_FORMULA_VERSION`, the threshold, the fraction, the band table,
and the placement rules are one frozen protocol surface, published
per round and vendored by sidecars like the score formula. Changing
any of them is a new formula version reviewed like a prompt revision
— never a silent edit; grades across versions are not comparable and
the round record names the version that produced its grades.
