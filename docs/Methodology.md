# Model Governance Methodology

How Post Fiat selects, re-confirms, and replaces the scoring model behind the Dynamic UNL — and how the models judging that choice are chosen.

Model governance works the same way scoring does: it runs as a recurring, reproducible round. Rounds are foundation-operated at first and verified by validator sidecars from day one; over time operation itself moves to the validator side, mirroring the scoring pipeline's own path. The cadence (every few days or weeks) is not decided yet. The scoring model can change only through a completed governance round — there are no silent changes.

This repository is the public record. Every exam, grade, and decision is committed here and pinned to IPFS, under the same durability rules as scoring artifacts. Validator runtime never reads this repository: sidecars learn about a model only from the per-round execution manifest, and the scoring service is configured with the selected model at deployment and emits that manifest.

## The candidate pool

The pool of models eligible for governance rounds is maintained as its own process, refreshed more often than rounds run. Each refresh:

1. **Pull the open-weight models from a public benchmark leaderboard.** Which leaderboard is still to be decided — LiveBench is the leading candidate. Models are ordered by our criteria through the leaderboard's own filters.
2. **Keep only single-GPU models.** A model must fit on one GPU — a modest one or the strongest available, but exactly one — with at least 10% memory headroom. This mirrors the production constraint: deterministic inference holds only on a single GPU, so a model that needs more can never serve scoring.
3. **Deduplicate families.** One model per family, keeping the best-ranked one. Pool members judge each other, and distinct families remove same-family judging bias.

Every refresh is published in this repository. A refresh affects only governance rounds frozen after it — the pool a round uses is the pool as it stood at that round's freeze, and nothing changes mid-round.

## The governance round

```
1. Freeze & publish   exam + grading prompt + candidate pool + incumbent margin,
                      pinned to IPFS and announced on-chain, before anything runs
2. Judge draw         a ledger that closes after the freeze picks this round's
                      judge by its hash; the judge is excluded from competing
3. Exam               every remaining candidate scores the corpus, each input
                      several times
4. Disqualify         mechanical failures are removed, publicly
5. Grade              the drawn judge grades the survivors
6. Verify             sidecars re-run everything and attest on chain
7. Decide             the highest grade wins, subject to the standing rules
8. Roll out           manifest change, safe endpoint swap, one shadow-verified round
```

### 1. Freeze & publish

Before any candidate touches the exam, the foundation freezes and publishes:

- the exam corpus,
- the grading prompt — judge-independent, because any pool member may end up grading with it; the governance counterpart of the scoring prompt the scoring pipeline already publishes,
- the candidate pool with pinned revisions and runtime profiles — the same pins an execution manifest carries for the scoring model,
- the incumbent-replacement margin for this round (see Decide),
- the judge-draw procedure: which validated ledger's hash is used and how it maps to a pool member.

The frozen candidate list is the maintained pool as it stands at freeze time, and it must contain **the incumbent plus at least two challengers** — with a single challenger, that challenger would always be drawn as judge, never get scored, and could never win.

Publication points to what already exists: the historical input packages in the corpus are referenced by their existing `input_package_cid`s, never re-pinned. Only new artifacts — the constructed edge cases, the grading prompt, and the round manifest listing everything — are pinned fresh. The CIDs are announced on-chain from the foundation publisher account, like a scoring-round announcement. After this point nothing can be tuned — changing anything, including pool membership, means abandoning the round and starting a new one. The exam is provably fixed before anyone takes it.

### 2. Judge draw

The round's judge is drawn from the pool by ledger randomness: the hash of the pre-specified validated ledger — one that closes only after the freeze announcement — maps to a pool member through the frozen procedure. The draw is unpredictable at freeze time and recomputable by anyone afterward, so no party, the foundation included, can influence which model judges the round.

Two standing rules: the drawn judge is excluded from that round's competition (it is not scored and cannot win, but competes again in later rounds), and the incumbent is never drawn — it always defends its seat.

### 3. Exam

The corpus is real production work: historical frozen round input packages (`input_package_cid`), each containing the exact request (`inputs/model_request.json`) that production inference consumed — already public, already hash-pinned. On top of that, constructed edge cases the real rounds have not produced yet: heavily degraded validators, ties at the selection cutoff, adversarial-looking evidence.

Every pool member except the drawn judge — the incumbent included — scores the full corpus on its pinned deterministic runtime profile. Every input runs several times.

### 4. Disqualify

Mechanical pass/fail rules, applied to the exam results:

- every output parses with the unmodified production response parser;
- repeated runs of the same input are bit-identical;
- the candidate handles the production request at least as cleanly as the incumbent;
- it ran on its pinned SGLang profile, which also demonstrates the model is servable under the production runtime.

Failing any rule removes the candidate from grading. Disqualifications are published with their evidence, never silent.

### 5. Grade

The drawn judge grades each survivor's outputs against the frozen grading prompt — and that grade is the ranking. Cost and latency (GPU seconds, wall-clock per round) and operational feasibility (GPU class, cold-start behavior, the cost a validator operator bears to run it) are measured and published alongside the results for operators and future preference voting, but they are not combined into a weighted formula.

The judge runs with the same discipline as any pool member: pinned revision, pinned SGLang image, deterministic profile. It grades only what cannot be checked mechanically — the quality of scoring reasoning. Because judging is deterministic and every artifact is pinned, anyone can re-run this round's grading and get identical grades — and can re-grade any past round under any judge offline, so judge rotation never erases cross-round comparability.

### 6. Verify

Sidecars re-run the judge draw, the exam, and the grading from the frozen inputs and commit/reveal their result hashes on chain — the same commit-reveal machinery scoring rounds already use, sealed the same way. Since every step is deterministic, unanimity is expected; any divergence halts the round until its cause is established.

This is verification, not voting: converged hashes prove the published results are exactly what the frozen process produces, and that nothing was fabricated or cherry-picked. A preference layer — operators weighting the verified results by their own priorities — can be added once community validators run sidecars; it changes nothing about the verification contract.

### 7. Decide

The highest-graded survivor wins. Two standing rules hold in every round:

- **Incumbent margin.** The incumbent stays unless a challenger beats it by the pre-declared margin. Model churn has real cost — redeployment, re-verification, operator disruption — and a marginal winner does not justify it. The margin also absorbs the round-to-round grading noise that judge rotation introduces.
- **Deployability fallback.** If the winner cannot in practice be deployed on Modal, the decision falls to the next-ranked candidate, repeating down the ranking as needed.

The decision and its full rationale — including an explicit "incumbent retained" statement when nothing changes — are published in this repository.

### 8. Roll out

- A model change reaches production only through the execution manifest. Modal-mode sidecars redeploy themselves to the new pinned profile automatically; local-runtime operators receive published upgrade instructions.
- The protocol never changes for a model change: commit-reveal, manifest schema, artifact contracts, and announcement formats all stay exactly as they are.
- The endpoint swap is delete-after-confirm: the new Modal endpoint is deployed alongside the old one, and the old one is deleted only after the new one is confirmed active and serving.
- Before anything relies on the new setup, validators reproduce at least one full scoring round on it — frozen artifacts, deterministic inference, commit-reveal, and a clean sealed convergence report.
- Rollback is the same move in reverse: a manifest change back to the previous pinned profile, which remains deployable.

When the round closes — whether the incumbent stays or a new model ships — its complete record is published: every candidate's raw outputs, the judge's grades, the sealed verification, and the decision with its rationale, all committed to this repository and pinned to IPFS.

## Decentralization path

The trajectory is the same as scoring's. Today the foundation runs the round and sidecars verify it. The end state is validator-side operation, where sidecars run the governance round themselves and the foundation becomes one participant among many. Each step of that shift only widens who executes the round — the round itself does not change.
