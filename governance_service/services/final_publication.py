"""Output withholding and final publication (G.5.5).

Nothing a round produces becomes public before its commit window closes:
sidecars must commit on chain to results they computed themselves, so an
early publication would let a commitment echo the foundation's outputs
instead of proving independent execution. The hold half parks a graded
round in the fail-closed AWAITING_COMMIT_CLOSE state (refusing to park a
round with no recorded commit close — a NULL there never releases); the
state machine's release gate frees it only once the announced window has
passed.

The publication half runs after release and the decision: the round's
complete record — identity and anchors, exam runs with raw outputs,
disqualification verdicts, grading outputs — is bundled under the same
manifest convention as the frozen package, pinned with the shared
pin-with-fallback contract, anchored on chain by the round-close receipt
memo, and summarized as a human-readable record document committed to
the governance repository's records tree. The full raw outputs live in
the pinned bundle; the repository record carries the summary and the
content identifiers pointing at it. The decision section joins the
record with G.5.6.

Each publication step is idempotent from its persisted identity: a
resumed round never re-pins a pinned bundle, never re-emits a landed
receipt, and never recommits an already-published repository record.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from governance_service.clients.github_records import GitHubRecordsClient
from governance_service.clients.pftl import PFTLClient
from governance_service.config import settings
from governance_service.scoring import canonical_json_bytes, canonical_json_hash
from governance_service.services import round_package
from governance_service.services.announcement import (
    ROUND_RECEIPT_TYPE,
    receipt_payload,
)
from governance_service.services.exam_engine import get_run_outputs
from governance_service.services.grading_engine import get_grading_outputs

logger = logging.getLogger(__name__)

RECORD_KIND = "governance_round_record"
RECORD_MANIFEST_VERSION = 1

ROUND_RECORDS_BASE_PATH = "records/rounds"

# error_message is deliberately absent everywhere below: operational
# text stays internal, only protocol values enter the permanent record.
_ROUND_COLUMNS = (
    "round_number",
    "trigger_source",
    "package_cid",
    "package_hash",
    "frozen_at",
    "announcement_tx_hash",
    "announcement_ledger_index",
    "commit_opens_at",
    "commit_closes_at",
    "reveal_opens_at",
    "reveal_closes_at",
    "judge_hf_repo",
    "draw_ledger_index",
    "draw_ledger_hash",
    "decision",
    "winner_hf_repo",
    "decision_rationale",
    "decided_at",
)

_EXAM_RUN_COLUMNS = (
    "id",
    "hf_repo",
    "revision",
    "profile_hash",
    "corpus_hash",
    "status",
    "candidate_failure",
    "verdict",
    "verdict_evidence",
    "verdict_at",
    "started_at",
    "completed_at",
)

_GRADING_RUN_COLUMNS = (
    "id",
    "hf_repo",
    "revision",
    "profile_hash",
    "material_hash",
    "status",
    "judge_failure",
    "started_at",
    "completed_at",
)


class FinalPublicationError(RuntimeError):
    """The hold or the publication could not proceed."""


def commit_window_closed(commit_closes_at: datetime | None) -> bool:
    """The withholding predicate: outputs leave only past a recorded close.

    The single definition both publication and the API gate on — the
    rule must never diverge between the two.
    """
    return commit_closes_at is not None and commit_closes_at <= datetime.now(
        timezone.utc
    )


def hold_outputs(conn, round_id: int) -> None:
    """The fail-closed park precondition: a recorded commit close.

    A parked round with no commit_closes_at would never release, so a
    graded round without one is a protocol error, not a quiet park.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT commit_closes_at FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None or row[0] is None:
        raise FinalPublicationError(
            f"Round id {round_id} has no recorded commit close — refusing "
            "to park a round that could never release"
        )


def _rows(cursor, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {name: _jsonable(value) for name, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_final_record(
    conn, round_id: int, round_number: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assemble the complete record bundle from the round's persisted data.

    Deterministic for identical round data: the bundle references only
    persisted values, never assembly-time clocks. Returns ``(files,
    bundle)`` under the frozen-package manifest convention. The decision
    section joins with G.5.6.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(_ROUND_COLUMNS)} FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    round_rows = _rows(cursor, _ROUND_COLUMNS)
    if not round_rows:
        cursor.close()
        conn.rollback()
        raise FinalPublicationError(f"Round id {round_id} does not exist")
    # Every file is a JSON object — lists are wrapped — because the
    # canonical serializer pins objects only, like the frozen package.
    files: dict[str, Any] = {"round.json": round_rows[0]}

    cursor.execute(
        f"""
        SELECT {', '.join(_EXAM_RUN_COLUMNS)} FROM exam_runs
        WHERE round_id = %s ORDER BY id
        """,
        (round_id,),
    )
    exam_runs = _rows(cursor, _EXAM_RUN_COLUMNS)
    files["exam/runs.json"] = {"runs": exam_runs}

    cursor.execute(
        f"""
        SELECT {', '.join(_GRADING_RUN_COLUMNS)} FROM grading_runs
        WHERE round_id = %s ORDER BY id
        """,
        (round_id,),
    )
    grading_runs = _rows(cursor, _GRADING_RUN_COLUMNS)
    files["grading/runs.json"] = {"runs": grading_runs}
    cursor.close()

    for run in exam_runs:
        files[f"exam/outputs/run-{run['id']}.json"] = {
            "outputs": get_run_outputs(conn, run["id"])
        }
    for run in grading_runs:
        files[f"grading/outputs/run-{run['id']}.json"] = {
            "outputs": get_grading_outputs(conn, run["id"])
        }
    conn.rollback()

    bundle = {
        "package_kind": RECORD_KIND,
        "manifest_version": RECORD_MANIFEST_VERSION,
        "round_number": round_number,
        "network": settings.environment,
        "commit_closes_at": files["round.json"]["commit_closes_at"],
        "file_hashes": {
            path: canonical_json_hash(content)
            for path, content in sorted(files.items())
        },
    }
    return files, bundle


def record_paths(round_number: int) -> tuple[str, str]:
    """Repository paths of one round's JSON record and summary."""
    stem = f"{ROUND_RECORDS_BASE_PATH}/{settings.environment}/round-{round_number}"
    return f"{stem}.json", f"{stem}.md"


def _render_repo_record(
    round_data: dict[str, Any],
    round_number: int,
    final_record_cid: str,
    receipt_tx_hash: str | None,
) -> tuple[str, str]:
    """The human-readable repository record: summary plus CID references."""
    summary = {
        "record_version": RECORD_MANIFEST_VERSION,
        "network": settings.environment,
        "round_number": round_number,
        "round": round_data,
        "final_record_cid": final_record_cid,
        "receipt_tx_hash": receipt_tx_hash,
    }
    lines = [
        f"# Governance round {round_number} ({settings.environment})",
        "",
        "The complete record — raw exam outputs, disqualification evidence, "
        "and grading outputs — lives in the pinned bundle; this document "
        "carries the round identity and the pointers to verify it.",
        "",
        f"- **Decision:** {round_data['decision'] or 'pending'}",
        f"- **Winner:** {round_data['winner_hf_repo'] or 'pending'}",
        f"- **Judge:** {round_data['judge_hf_repo'] or 'not drawn'}",
        f"- **Package CID:** `{round_data['package_cid']}`",
        f"- **Package hash:** `{round_data['package_hash']}`",
        f"- **Announcement:** tx `{round_data['announcement_tx_hash']}` "
        f"(ledger {round_data['announcement_ledger_index']})",
        f"- **Draw ledger:** {round_data['draw_ledger_index']} "
        f"(`{round_data['draw_ledger_hash']}`)",
        f"- **Commit window:** {round_data['commit_opens_at']} → "
        f"{round_data['commit_closes_at']}",
        f"- **Final record CID:** `{final_record_cid}`",
        f"- **Receipt:** tx `{receipt_tx_hash}`",
        "",
    ]
    # The summary is never hashed, so it stays pretty-printed and
    # diffable like the pool-refresh records.
    json_text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    return json_text, "\n".join(lines)


def _load_publication_state(conn, round_id: int) -> dict[str, Any]:
    columns = (
        "package_hash",
        "commit_closes_at",
        "final_record_cid",
        "receipt_tx_hash",
        "record_commit_url",
    )
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(columns)} FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None:
        raise FinalPublicationError(f"Round id {round_id} does not exist")
    return dict(zip(columns, row))


def _persist(conn, round_id: int, **fields: Any) -> None:
    set_clause = ", ".join(f"{name} = %s" for name in fields)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE governance_rounds SET {set_clause} WHERE id = %s",
        (*fields.values(), round_id),
    )
    conn.commit()
    cursor.close()


def publish_round_record(
    conn,
    round_id: int,
    round_number: int,
    *,
    pin: Callable[[dict[str, Any], dict[str, Any], str], str] | None = None,
    pftl_client: PFTLClient | None = None,
    records_client: GitHubRecordsClient | None = None,
) -> dict[str, Any]:
    """Pin the complete record, anchor the receipt, publish the repo record.

    Runs strictly after the commit window closed and the decision landed.
    Each step short-circuits from its persisted identity, so a resumed
    round continues exactly where publication stopped. Repository
    publication is skipped (with the skip recorded in the result) when no
    records token is configured — local and CI runs never push to GitHub.
    """
    state = _load_publication_state(conn, round_id)
    if state["package_hash"] is None:
        raise FinalPublicationError(
            f"Round id {round_id} has no frozen package identity to publish"
        )
    # Last line of defense for the withholding invariant: the state
    # machine's release gate is the authority, but publishing before the
    # recorded commit close would break verification at the root.
    if not commit_window_closed(state["commit_closes_at"]):
        raise FinalPublicationError(
            f"Round id {round_id} commit window has not closed — outputs stay withheld"
        )

    final_record_cid = state["final_record_cid"]
    if final_record_cid is None:
        files, bundle = build_final_record(conn, round_id, round_number)
        pin_name = f"governance-round-record-{settings.environment}-{round_number}"
        final_record_cid = (pin or round_package.pin_package)(files, bundle, pin_name)
        _persist(conn, round_id, final_record_cid=final_record_cid)

    receipt_tx_hash = state["receipt_tx_hash"]
    if receipt_tx_hash is None:
        payload = receipt_payload(
            network=settings.environment,
            round_number=round_number,
            package_hash=state["package_hash"],
            final_record_cid=final_record_cid,
        )
        memo_data = canonical_json_bytes(payload).decode("utf-8")
        client = pftl_client or PFTLClient()
        success, receipt_tx_hash, _ledger_index, error = client.submit_memo(
            memo_data, memo_type=ROUND_RECEIPT_TYPE
        )
        if not success:
            raise FinalPublicationError(f"Receipt memo submission failed: {error}")
        if not receipt_tx_hash:
            raise FinalPublicationError(
                "Receipt memo validated without a transaction hash — refusing "
                "to persist an empty idempotency key"
            )
        _persist(conn, round_id, receipt_tx_hash=receipt_tx_hash)
    else:
        logger.info(
            "Round %d receipt already on chain (tx %s) — not re-emitting",
            round_number,
            receipt_tx_hash,
        )

    record_commit_url = state["record_commit_url"]
    repo_record_skipped = False
    if not settings.records_enabled:
        repo_record_skipped = True
        logger.warning(
            "Records publication disabled — round %d repository record skipped",
            round_number,
        )
    elif record_commit_url is not None:
        logger.info(
            "Round %d repository record already published (%s) — not recommitting",
            round_number,
            record_commit_url,
        )
    else:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(_ROUND_COLUMNS)} FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        round_data = _rows(cursor, _ROUND_COLUMNS)[0]
        cursor.close()
        conn.rollback()
        json_text, md_text = _render_repo_record(
            round_data, round_number, final_record_cid, receipt_tx_hash
        )
        json_path, md_path = record_paths(round_number)
        client = records_client or GitHubRecordsClient()
        message = f"Publish governance round {round_number} record ({settings.environment})"
        client.publish(json_path, json_text, message)
        record_commit_url = client.publish(md_path, md_text, message)
        _persist(conn, round_id, record_commit_url=record_commit_url)

    logger.info(
        "Round %d published: record %s, receipt %s%s",
        round_number,
        final_record_cid,
        receipt_tx_hash,
        " (repository record skipped)" if repo_record_skipped else "",
    )
    return {
        "final_record_cid": final_record_cid,
        "receipt_tx_hash": receipt_tx_hash,
        "record_commit_url": record_commit_url,
        "repo_record_skipped": repo_record_skipped,
    }
