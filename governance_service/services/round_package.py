"""The frozen round package: assembly, pinning, and persistence (G.5.2).

The freeze is the moment a governance round becomes tamper-proof: every
input the round will use — corpus references, fresh edge cases, pool pins,
the grading artifacts, the adaptation rule, the round parameters with the
judge-draw procedure and window durations — is gathered into one package,
canonically hashed under the scoring input-package bundle convention
(``bundle.json`` with per-file hashes; the package hash is the canonical
hash of the bundle), pinned to IPFS, and persisted for HTTPS serving.
After this point nothing can be tuned; changing anything means abandoning
the round.

The announcement formats join the package at G.5.3, when the governance
memo types exist; the roadmap assigns them to on-chain publishing.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import yaml

from governance_service.clients.ipfs import IPFSClient
from governance_service.clients.pinata import PinataClient
from governance_service.config import settings
from governance_service.models.runtime_profile import RuntimeProfile
from governance_service.scoring import canonical_json_bytes, canonical_json_hash
from governance_service.services import corpus as corpus_service
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.checker import RULES_PATH
from governance_service.services.exam_engine import REPEAT_COUNT
from governance_service.services.grade_formula import (
    ACROSS_SET_FRACTION,
    ACROSS_SET_KINDS,
    GRADE_FORMULA_VERSION,
    GRADE_STEP,
    SYSTEMIC_VALIDATOR_THRESHOLD,
)
from governance_service.services.grading import (
    GRADING_MAX_TOKENS,
    GRADING_PROMPT_PATH,
    GRADING_PROMPT_VERSION,
    KINDS_REQUIRING_VALIDATORS,
    SECTION_KINDS,
    SECTION_OUTCOMES,
)
from governance_service.services.pool_refresh import STATUS_COMPLETED
from governance_service.services.request_adaptation import (
    ADAPTATION_RULE_VERSION,
    ADAPTED_FIELDS,
)

logger = logging.getLogger(__name__)

PACKAGE_KIND = "governance_round"
PACKAGE_MANIFEST_VERSION = 1
BUNDLE_FILE_PATH = "bundle.json"

# The methodology's freeze eligibility rule: with fewer challengers the
# drawn judge could never be replaced and no challenger could win.
MIN_CHALLENGERS = 2

# The standing incumbent-replacement margin, frozen per round.
INCUMBENT_MARGIN_POINTS = 5

# The judge-draw procedure frozen in the package and implemented at G.5.4:
# the drawing ledger is the first validated ledger whose index is at least
# the announcement's validated ledger index plus this offset, giving the
# announcement time to settle beyond dispute before the draw becomes fixed.
DRAW_PROCEDURE_VERSION = 1
DRAW_LEDGER_OFFSET = 10

# The output hash set sidecars commit to, implemented at G.6.
HASH_SET_VERSION = 1
HASH_SET_MEMBERS = (
    "judge_draw",
    "exam_outputs",
    "disqualification_verdicts",
    "grading_defects",
    "final_grades",
)


class FreezeEligibilityError(RuntimeError):
    """The maintained pool cannot freeze a round; carries the evidence.

    The evidence is folded into the message so the round's recorded
    error_message is the complete public reason, not a summary of it.
    """

    def __init__(self, reason: str, evidence: dict[str, Any]):
        super().__init__(f"{reason} — evidence: {json.dumps(evidence, sort_keys=True)}")
        self.evidence = evidence


class FreezePinningError(RuntimeError):
    """The package could not be pinned anywhere; the freeze fails closed."""


@dataclass
class FrozenPool:
    """The maintained pool as it stands at freeze time, deployably pinned."""

    refresh_id: int
    incumbent: RuntimeProfile
    challengers: list[RuntimeProfile]


def load_frozen_pool(conn) -> FrozenPool:
    """The current pool from the newest COMPLETED refresh, profile-mapped.

    Every pool member must resolve to a deployable runtime profile whose
    pinned revision matches the pool record — a member that cannot deploy
    cannot sit the exam, so a missing or mismatched profile is freeze
    ineligibility, not a runtime surprise later.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id FROM pool_refreshes
        WHERE status = %s
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (STATUS_COMPLETED,),
    )
    refresh_row = cursor.fetchone()
    if refresh_row is None:
        cursor.close()
        raise FreezeEligibilityError(
            "No completed pool refresh — the pool is empty",
            {"completed_refreshes": 0},
        )
    refresh_id = refresh_row[0]

    cursor.execute(
        """
        SELECT hf_repo, revision, is_incumbent
        FROM pool_refresh_candidates
        WHERE refresh_id = %s AND in_pool
        ORDER BY hf_repo
        """,
        (refresh_id,),
    )
    rows = cursor.fetchall()
    cursor.close()

    incumbent: RuntimeProfile | None = None
    challengers: list[RuntimeProfile] = []
    for hf_repo, revision, is_incumbent in rows:
        profile = CURRENT_POOL_PROFILES.get(hf_repo)
        if profile is None:
            raise FreezeEligibilityError(
                f"Pool member {hf_repo} has no deployable runtime profile",
                {"refresh_id": refresh_id, "unmapped_member": hf_repo},
            )
        if profile.revision != revision:
            raise FreezeEligibilityError(
                f"Pool member {hf_repo} revision mismatch: pool pins "
                f"{revision}, deployable profile pins {profile.revision}",
                {
                    "refresh_id": refresh_id,
                    "member": hf_repo,
                    "pool_revision": revision,
                    "profile_revision": profile.revision,
                },
            )
        if is_incumbent:
            incumbent = profile
        else:
            challengers.append(profile)

    if incumbent is None:
        raise FreezeEligibilityError(
            "The pool has no incumbent",
            {"refresh_id": refresh_id, "members": len(rows)},
        )
    if len(challengers) < MIN_CHALLENGERS:
        raise FreezeEligibilityError(
            f"The pool holds {len(challengers)} challenger(s); freezing "
            f"requires at least {MIN_CHALLENGERS}",
            {"refresh_id": refresh_id, "challengers": len(challengers)},
        )
    # Codepoint order, not database collation: the frozen draw mapping
    # indexes this order, so it must be reproducible by any verifier.
    challengers.sort(key=lambda p: p.hf_repo)
    return FrozenPool(refresh_id=refresh_id, incumbent=incumbent, challengers=challengers)


def _profile_entry(profile: RuntimeProfile) -> dict[str, Any]:
    return {"profile": profile.model_dump(), "profile_hash": profile.content_hash()}


def build_package(
    round_number: int,
    corpus: corpus_service.CorpusResult,
    pool: FrozenPool,
    frozen_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assemble the package files and their bundle manifest.

    Returns ``(files, bundle)``: every frozen file keyed by package path,
    and the bundle whose canonical hash is the package hash. Deterministic
    for identical inputs — assembly itself introduces nothing variable.
    """
    files: dict[str, Any] = {"corpus/manifest.json": corpus.manifest}
    for name, request in sorted(corpus.constructed.items()):
        files[f"corpus/edge_cases/{name}.json"] = request

    files["pool/candidates.json"] = {
        "refresh_id": pool.refresh_id,
        "incumbent": _profile_entry(pool.incumbent),
        "challengers": [_profile_entry(p) for p in pool.challengers],
    }

    files["grading/prompt.json"] = {
        "version": GRADING_PROMPT_VERSION,
        "text": GRADING_PROMPT_PATH.read_text(encoding="utf-8"),
    }
    files["grading/defect_schema.json"] = {
        "section_kinds": {k: list(v) for k, v in SECTION_KINDS.items()},
        "section_outcomes": {k: list(v) for k, v in SECTION_OUTCOMES.items()},
        "kinds_requiring_validators": list(KINDS_REQUIRING_VALIDATORS),
        "max_tokens": GRADING_MAX_TOKENS,
    }
    files["grading/checker_rules.json"] = {
        "rules": yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")),
    }
    files["grading/grade_formula.json"] = {
        "version": GRADE_FORMULA_VERSION,
        "constants": {
            "grade_step": GRADE_STEP,
            "systemic_validator_threshold": SYSTEMIC_VALIDATOR_THRESHOLD,
            "across_set_fraction": str(ACROSS_SET_FRACTION),
            "across_set_kinds": list(ACROSS_SET_KINDS),
        },
    }

    files["rules/adaptation.json"] = {
        "version": ADAPTATION_RULE_VERSION,
        "derived_fields": list(ADAPTED_FIELDS),
        "description": (
            "Only the model name and the chat-template settings block are "
            "derived from each candidate's frozen runtime profile; every "
            "other byte of the production request is untouched."
        ),
    }

    files["round/parameters.json"] = {
        "repeat_count": REPEAT_COUNT,
        "incumbent_margin_points": INCUMBENT_MARGIN_POINTS,
        "commit_window_seconds": settings.round_commit_window_seconds,
        "reveal_window_seconds": settings.round_reveal_window_seconds,
        "draw_procedure": {
            "version": DRAW_PROCEDURE_VERSION,
            "source": "validated_ledger_hash",
            "ledger_offset": DRAW_LEDGER_OFFSET,
            "mapping": (
                "The drawing ledger is the first validated ledger whose index "
                "is at least the announcement transaction's validated ledger "
                "index plus ledger_offset. Its hash, read as a big-endian "
                "integer, modulo the challenger count indexes the challengers "
                "sorted ascending by hf_repo in Unicode codepoint order."
            ),
            "redraw": (
                "On a judge's mechanical failure the index advances by one, "
                "cyclically, skipping already-failed judges; exhausting the "
                "challengers abandons the round."
            ),
        },
        "hash_set": {
            "version": HASH_SET_VERSION,
            "members": list(HASH_SET_MEMBERS),
        },
    }

    bundle = {
        "package_kind": PACKAGE_KIND,
        "manifest_version": PACKAGE_MANIFEST_VERSION,
        "round_number": round_number,
        "network": settings.environment,
        "frozen_at": frozen_at.isoformat(),
        "file_hashes": {
            path: canonical_json_hash(content) for path, content in sorted(files.items())
        },
    }
    return files, bundle


def pin_package(files: dict[str, Any], bundle: dict[str, Any], round_number: int) -> str:
    """Pin the package directory, bundle included, and return its CID.

    Mirrors the repository's pin-with-fallback contract: the primary node
    pin is replicated to Pinata by CID, and when the primary pin fails
    Pinata's direct upload is the write fallback. A freeze without a pin
    is meaningless, so no available backend fails the freeze closed.
    """
    payload = {path: canonical_json_bytes(content) for path, content in files.items()}
    payload[BUNDLE_FILE_PATH] = canonical_json_bytes(bundle)
    pin_name = f"governance-round-{settings.environment}-{round_number}"

    if settings.ipfs_enabled:
        cid = IPFSClient().pin_directory(payload)
        if cid:
            if settings.pinata_enabled:
                PinataClient().pin_by_cid(cid, name=pin_name)
            return cid

    if settings.pinata_enabled:
        if settings.ipfs_enabled:
            logger.warning(
                "Primary IPFS pin failed for round %d — falling back to "
                "Pinata direct upload",
                round_number,
            )
        cid = PinataClient().pin_directory(payload, name=pin_name)
        if cid:
            return cid

    raise FreezePinningError(
        "Round package could not be pinned: no pinning backend succeeded "
        "(configure IPFS_API_URL and/or Pinata credentials)"
    )


def persist_package(
    conn,
    round_id: int,
    files: dict[str, Any],
    bundle: dict[str, Any],
    cid: str,
    frozen_at: datetime,
) -> str:
    """Store the package files and identity on the round; returns the hash.

    Delete-then-insert so a re-run persists exactly the new file set —
    a path an earlier attempt wrote never lingers as a served orphan.
    """
    bundle_hash = canonical_json_hash(bundle)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM governance_round_artifacts WHERE round_id = %s",
        (round_id,),
    )
    rows = [(path, content, bundle["file_hashes"][path]) for path, content in files.items()]
    rows.append((BUNDLE_FILE_PATH, bundle, bundle_hash))
    for path, content, content_hash in rows:
        cursor.execute(
            """
            INSERT INTO governance_round_artifacts (round_id, path, content, content_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (round_id, path, canonical_json_bytes(content).decode("utf-8"), content_hash),
        )
    cursor.execute(
        """
        UPDATE governance_rounds
        SET package_cid = %s, package_hash = %s, frozen_at = %s
        WHERE id = %s
        """,
        (cid, bundle_hash, frozen_at, round_id),
    )
    conn.commit()
    cursor.close()
    return bundle_hash


def _build_corpus_default() -> corpus_service.CorpusResult:
    with httpx.Client(timeout=settings.http_timeout_seconds) as client:
        return corpus_service.build_corpus(client)


def freeze_round(
    conn,
    round_id: int,
    round_number: int,
    *,
    corpus_builder: Callable[[], corpus_service.CorpusResult] | None = None,
    pin: Callable[[dict[str, Any], dict[str, Any], int], str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute the freeze: pool, corpus, package, pin, persist.

    Raises FreezeEligibilityError or FreezePinningError on failure; the
    orchestrator's stage discipline turns either into a FAILED round with
    the reason recorded. A crash between the persist commit and the
    orchestrator's FROZEN status write leaves a pinned, served package on
    a round startup cleanup then abandons — harmless: round numbers never
    reuse, and the artifacts stand as honest evidence of the attempt.
    """
    pool = load_frozen_pool(conn)
    # The pool read is done; corpus assembly and pinning take minutes, so
    # do not sit on an idle-in-transaction connection through them.
    conn.rollback()
    corpus = (corpus_builder or _build_corpus_default)()
    frozen_at = now or datetime.now(timezone.utc)

    files, bundle = build_package(round_number, corpus, pool, frozen_at)
    cid = (pin or pin_package)(files, bundle, round_number)
    bundle_hash = persist_package(conn, round_id, files, bundle, cid, frozen_at)

    logger.info(
        "Round %d frozen: package %s (hash %s, %d files)",
        round_number,
        cid,
        bundle_hash,
        len(files) + 1,
    )
    return {"package_cid": cid, "package_hash": bundle_hash, "files": len(files) + 1}


def get_package_file(conn, round_number: int, path: str) -> Any | None:
    """One persisted package file's content, or None when absent."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.content
        FROM governance_round_artifacts a
        JOIN governance_rounds r ON r.id = a.round_id
        WHERE r.round_number = %s AND a.path = %s
        """,
        (round_number, path),
    )
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row else None
