# Model Governance Methodology

How Post Fiat selects, re-confirms, and replaces the scoring model behind the Dynamic UNL — and how the models judging that choice are chosen.

Model governance works the same way scoring does: it runs as a recurring, reproducible round. Rounds are foundation-operated at first and verified by validator sidecars from day one; over time operation itself moves to the validator side, mirroring the scoring pipeline's own path. The cadence (every few days or weeks) is not decided yet. The scoring model can change only through a completed governance round — there are no silent changes.

This repository is the public record. Every exam, grade, and decision is committed here and pinned to IPFS, under the same durability rules as scoring artifacts. Validator runtime never reads this repository: sidecars learn about the serving scoring model only from the per-round execution manifest, and the scoring service is configured with the selected model at deployment and emits that manifest.

## The candidate pool

The pool of models eligible for governance rounds is maintained as its own process. Each refresh:

1. **Pull the open-weight models from a public benchmark leaderboard.** Which leaderboard is still to be decided — LiveBench is the leading candidate. Models are ordered by our criteria through the leaderboard's own filters. Blocklisted revisions (see below) are skipped: the slot passes to the model's next non-blocked revision or family sibling, or to the next model on the leaderboard.
2. **Keep only single-GPU models.** A model must fit on one GPU — a modest one or the strongest available, but exactly one — with at least 10% memory headroom. Single-GPU inference is the profile production determinism is proven on, so a model that needs more can never serve scoring.
3. **Deduplicate families.** One model per family, keeping the best-ranked one. Pool members judge each other, and distinct families remove same-family judging bias.

These rules select the challengers. The incumbent — the model currently serving scoring — is a pool member by right: it is exempt from the leaderboard and family rules (its single-GPU fit is already proven in production), and no refresh can remove it. When the incumbent's own family produces a better-ranked successor, that successor takes the family's challenger slot and the pool carries both — refusing the pairing would lock out the most likely upgrade path, and the residual same-family judging bias is absorbed by the incumbent margin (see Decide).

The repository also carries a standing **blocklist**: one entry per pinned model revision that failed its mechanical checks in a past round, with a one-sentence reason and a reference to the round record holding the evidence. Refreshes consume the blocklist when filtering the leaderboard; entries are added only when a round ends, never mid-round — and a round abandoned because the foundation's results were shown wrong (see Verify) adds none, because its evidence is the thing in doubt. A blocked revision stays blocked — a new revision of the same model is a new candidate.

Every refresh is published in this repository. A refresh affects only governance rounds frozen after it — the pool a round uses is the pool as it stood at that round's freeze, and nothing changes mid-round.

## The governance round

```
1. Freeze & publish   exam + grading rules + candidate pool + incumbent margin,
                      pinned to IPFS and announced on-chain, before anything runs
2. Judge draw         a ledger that closes after the freeze picks this round's
                      judge by its hash; the judge is excluded from competing
3. Exam               every remaining candidate is deployed on Modal and scores
                      the corpus, each input three times
4. Disqualify         mechanical failures are removed, with recorded evidence
5. Grade              the drawn judge grades the survivors
6. Verify             sidecars re-run everything and attest on chain before any
                      result is published
7. Decide             the highest grade wins, subject to the incumbent margin
8. Roll out           manifest change, safe endpoint swap, one shadow-verified round
```

### 1. Freeze & publish

Before any candidate touches the exam, the foundation freezes and publishes:

- the exam corpus,
- the grading prompt — judge-independent, because any pool member may end up grading with it — together with the grading output schema and its parser, so grades are as mechanically checkable as scores; the governance counterparts of the scoring prompt and response parser the scoring pipeline already publishes,
- the candidate pool with pinned revisions and runtime profiles — the same pins an execution manifest carries for the scoring model,
- the request-adaptation rule: the corpus's production requests embed the then-serving model's name and chat-template settings, so no other candidate can replay them verbatim; the rule derives exactly those fields — and nothing else — from each candidate's frozen runtime profile, leaving every other byte of the request untouched, so anyone can reconstruct the identical per-candidate requests,
- the repeat count for the determinism check: three runs per input, frozen in the round manifest,
- the incumbent-replacement margin for this round (see Decide),
- the judge-draw procedure: which validated ledger's hash is used, how it maps to a challenger, and the redraw ordering applied if a drawn judge fails its own mechanical checks,
- the round's announcement formats, the set of output hashes sidecars commit to, and the commit and reveal windows — sized for exam workloads, which dwarf a scoring round's single inference.

The frozen candidate list is the maintained pool as it stands at freeze time, and it must contain **the incumbent plus at least two challengers** — with a single challenger, that challenger would always be drawn as judge, never get scored, and could never win. If the maintained pool cannot supply two challengers after its filters, no round freezes and the incumbent simply stays.

Publication points to what already exists: the historical input packages in the corpus are referenced by their existing `input_package_cid`s, never re-pinned. Only new artifacts — the constructed edge cases, the grading rules, and the round manifest listing everything — are pinned fresh. The CIDs are announced on-chain from the foundation publisher account, like a scoring-round announcement. After this point nothing can be tuned — changing anything, including pool membership, means abandoning the round and starting a new one. The exam is provably fixed before anyone takes it.

### 2. Judge draw

The round's judge is drawn from the pool by ledger randomness: the hash of the pre-specified validated ledger — one that closes only after the freeze announcement — maps to a challenger through the frozen procedure. The draw is unpredictable at freeze time and recomputable by anyone afterward, so no party, the foundation included, can influence which model judges the round.

Two standing rules: the drawn judge is excluded from that round's competition (it is not scored and cannot win, but competes again in later rounds), and the incumbent is never drawn — it always defends its seat.

The judge is held to the same mechanical bar as any candidate. If it fails to deploy and serve on its pinned profile, its output does not parse under the frozen grading schema, or its grading runs are not bit-identical across the frozen repeat count, the frozen redraw ordering applies: the same ledger hash maps to the next challenger, repeating as needed. A judge drawn by redraw has already sat the exam; its exam outputs stay in the round record but are discarded from the competition — like any judge, it cannot win. A failed judge sits out the rest of the round entirely and enters the blocklist when the round closes. If the redraws exhaust the challengers, the round is abandoned — the incumbent stays, and the failed judges' blocklist entries are the round's only outcome.

### 3. Exam

The corpus is real production work: historical frozen round input packages (`input_package_cid`), each containing the exact request (`inputs/model_request.json`) that production inference consumed — already public, already hash-pinned. On top of that, constructed edge cases the real rounds have not produced yet: heavily degraded validators, ties at the selection cutoff, adversarial-looking evidence. Constructed edge cases are built in the same request format as the production packages, so the request-adaptation rule covers the whole corpus uniformly.

The exam runs on Modal. Every pool member except the drawn judge — the incumbent included — is deployed there on its pinned deterministic runtime profile and scores the full corpus through its adapted requests. Every input runs three times. A candidate that fails to deploy and serve on its pinned profile is mechanically disqualified on the spot — which is why deployability needs no separate fallback later.

### 4. Disqualify

Mechanical pass/fail rules, applied to the exam results:

- every output parses with the unmodified production response parser;
- all three runs of the same input are bit-identical;
- it deployed and served on Modal on its pinned SGLang profile.

Failing any rule removes the candidate from grading and books its revision into the blocklist at round close. Every disqualification is recorded with its evidence in the round's published record, never silently.

### 5. Grade

The drawn judge grades each survivor's outputs against the frozen grading prompt — and that grade is the ranking. Cost and latency (GPU seconds, wall-clock per round) and operational feasibility (GPU class, cold-start behavior, the cost a validator operator bears to run it) are measured and published alongside the results for operators and future preference voting, but they are not combined into a weighted formula.

The judge runs with the same discipline as any pool member: pinned revision, pinned SGLang image, deterministic profile, grades emitted in the frozen output schema and parsed by the frozen grading parser. It grades only what cannot be checked mechanically — the quality of scoring reasoning. Because judging is deterministic and every artifact is pinned, anyone can re-run this round's grading and get identical grades — and can re-grade any past round under any judge offline, so judge rotation never erases cross-round comparability.

### 6. Verify

Sidecars re-run the judge draw, the exam, the disqualification, and the grading from the frozen inputs and commit/reveal their result hashes on chain — the same commit-reveal machinery scoring rounds already use, sealed the same way, over the hash set frozen at publication.

Nothing the round produces — exam outputs, disqualification evidence, grades, the decision — becomes public before the round's commit window closes. Sidecars must commit to what they computed themselves, exactly the output-withholding discipline scoring rounds enforce; publishing earlier would let a commitment echo the foundation's results instead of proving independent execution.

Since every step is deterministic, unanimity is expected; any divergence halts the round until its cause is established. A halt has exactly two exits, and nothing in between: the divergent verifier is shown wrong — its bug, its runtime — and the round proceeds unchanged from its frozen state, or the foundation's results are shown wrong and the round is abandoned and rerun from a fresh freeze. Nothing is ever corrected mid-round.

This is verification, not voting: converged hashes prove the published results are exactly what the frozen process produces, and that nothing was fabricated or cherry-picked. A preference layer — operators weighting the verified results by their own priorities — can be added once community validators run sidecars; it changes nothing about the verification contract.

### 7. Decide

The highest-graded survivor wins, subject to one standing rule:

- **Incumbent margin.** The incumbent stays unless a challenger beats it by the pre-declared margin. Model churn has real cost — redeployment, re-verification, operator disruption — and a marginal winner does not justify it. The margin also absorbs the round-to-round grading noise that judge rotation introduces.

The margin protects a healthy incumbent only. An incumbent that is itself mechanically disqualified loses that protection: the highest-graded surviving challenger wins outright, and the incumbent enters the blocklist like any other failed candidate. If no challenger survives either, the round closes without a replacement: the incumbent keeps serving by necessity — its blocklist entry takes effect once it is replaced, barring any return as a challenger — and rounds keep running until one produces a winner. Either way the failure is a production alarm, because it means the live scorer no longer upholds the determinism discipline scoring verification depends on.

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
