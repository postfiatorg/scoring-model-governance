"""Refresh execution and persistence against a real database.

Network-bound pieces (release discovery, sourcing, incumbent artifact
resolution) are monkeypatched with controlled per-release data; rule
evaluation, the release walk, and every database write run for real.
"""

from unittest.mock import MagicMock

import pytest

from governance_service.api import pool as pool_api
from governance_service.clients import huggingface, livebench
from governance_service.models import (
    BlocklistEntry,
    CandidateDescriptor,
    ModelArtifact,
    ModelGeometry,
    SourcingReport,
)
from governance_service.services import pool_refresh
from governance_service.services.pool_refresh import (
    RULE_BLOCKLISTED,
    RULE_IS_INCUMBENT,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NO_VIABLE_POOL,
    create_refresh,
    execute_refresh,
    get_current_pool,
)
from governance_service.services.record_publisher import PUBLICATION_SKIPPED

INCUMBENT_REPO = "Qwen/Qwen3.6-27B-FP8"
INCUMBENT_ARTIFACT = ModelArtifact(
    repo_id=INCUMBENT_REPO,
    revision="incumbent-rev",
    precision="fp8",
    weight_bytes=29_000_000_000,
    geometry=ModelGeometry(num_hidden_layers=48, num_key_value_heads=8, head_dim=128),
    license="apache-2.0",
    gated=False,
)


def descriptor(
    key: str,
    family: str,
    global_average: float,
    release: str,
    precision: str = "fp8",
    assigned_gpu: str | None = "H100",
    hf_repo: str | None = None,
) -> CandidateDescriptor:
    return CandidateDescriptor(
        livebench_key=key,
        display_name=key.title(),
        organization=family.title(),
        family=family,
        thinking="hybrid",
        global_average=global_average,
        category_averages={"reasoning": global_average},
        hf_repo=hf_repo or f"{family}/{key}",
        revision=f"rev-{key}",
        precision=precision,
        weight_bytes=30_000_000_000,
        license="apache-2.0",
        gated=False,
        assigned_gpu=assigned_gpu,
        release=release,
    )


def report(release: str, candidates: list[CandidateDescriptor]) -> SourcingReport:
    return SourcingReport(
        release=release,
        candidates=candidates,
        unmapped=[],
        skipped={},
        snapshots=[],
    )


@pytest.fixture()
def network(monkeypatch):
    """Wire the refresh to per-release reports instead of live upstreams."""

    state = {"releases": [], "reports": {}, "blocklist": []}

    def configure(reports_by_release: dict[str, SourcingReport], blocklist=None):
        state["releases"] = sorted(reports_by_release)
        state["reports"] = reports_by_release
        state["blocklist"] = blocklist or []

    monkeypatch.setattr(
        livebench, "discover_releases", lambda client: state["releases"]
    )
    monkeypatch.setattr(livebench, "fetch_registry", lambda client: b"registry")
    monkeypatch.setattr(
        pool_refresh,
        "source_candidates",
        lambda client, release, registry_raw=None, artifact_cache=None: state[
            "reports"
        ][release],
    )
    monkeypatch.setattr(
        huggingface,
        "fetch_artifact",
        lambda client, repo_id, revision=None: INCUMBENT_ARTIFACT,
    )
    monkeypatch.setattr(
        pool_refresh, "load_blocklist", lambda: state["blocklist"]
    )
    return configure


def candidate_rows(connection, refresh_id: int) -> list[tuple]:
    """(release, hf_repo, in_pool, exclusion_rule, is_incumbent, revision)."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT release, hf_repo, in_pool, exclusion_rule, is_incumbent, revision
        FROM pool_refresh_candidates
        WHERE refresh_id = %s
        """,
        (refresh_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def row_for(rows: list[tuple], release: str | None, hf_repo: str, in_pool: bool):
    matches = [
        r for r in rows if r[0] == release and r[1] == hf_repo and r[2] == in_pool
    ]
    assert len(matches) == 1, f"expected one row for {hf_repo}, got {matches}"
    return matches[0]


def refresh_row(connection, refresh_id: int) -> tuple:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT status, release_used, releases_considered, error_message,
               completed_at
        FROM pool_refreshes
        WHERE id = %s
        """,
        (refresh_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def test_refresh_completes_on_viable_latest_release(db, network):
    latest = "2026_06_25"
    network(
        {
            latest: report(
                latest,
                [
                    descriptor("challenger-a", "a", 75.0, latest),
                    descriptor("challenger-b", "b", 72.0, latest),
                    descriptor(
                        "qwen3.6-27b", "qwen", 64.0, latest, hf_repo=INCUMBENT_REPO
                    ),
                ],
            )
        }
    )

    refresh_id = create_refresh(db)
    result = execute_refresh(db, refresh_id)

    assert result.status == STATUS_COMPLETED
    assert result.release_used == latest

    status, release_used, considered, error, completed_at = refresh_row(db, refresh_id)
    assert status == STATUS_COMPLETED
    assert release_used == latest
    assert error is None
    assert completed_at is not None
    assert len(considered) == 1
    assert considered[0]["viable"] is True

    rows = candidate_rows(db, refresh_id)
    assert row_for(rows, latest, "a/challenger-a", in_pool=True)[3] is None
    assert row_for(rows, latest, "b/challenger-b", in_pool=True)[3] is None

    member = row_for(rows, latest, INCUMBENT_REPO, in_pool=True)
    assert member[4] is True
    assert member[5] == "incumbent-rev"
    evaluation = row_for(rows, latest, INCUMBENT_REPO, in_pool=False)
    assert evaluation[3] == RULE_IS_INCUMBENT

    pool = get_current_pool(db)
    assert [m["hf_repo"] for m in pool] == [
        INCUMBENT_REPO,
        "a/challenger-a",
        "b/challenger-b",
    ]

    # Records are unconfigured in tests, so publication is visibly skipped.
    cursor = db.cursor()
    cursor.execute(
        "SELECT publication_status FROM pool_refreshes WHERE id = %s", (refresh_id,)
    )
    assert cursor.fetchone()[0] == PUBLICATION_SKIPPED
    cursor.close()


def test_refresh_falls_back_to_older_release(db, network):
    latest, older = "2026_06_25", "2026_01_08"
    network(
        {
            latest: report(
                latest,
                [
                    descriptor("only-one", "a", 75.0, latest),
                    descriptor("too-big", "b", 74.0, latest, assigned_gpu=None),
                ],
            ),
            older: report(
                older,
                [
                    descriptor("challenger-a", "a", 71.0, older),
                    descriptor("challenger-b", "b", 70.0, older),
                ],
            ),
        }
    )

    refresh_id = create_refresh(db)
    result = execute_refresh(db, refresh_id)

    assert result.status == STATUS_COMPLETED
    assert result.release_used == older

    _, release_used, considered, _, _ = refresh_row(db, refresh_id)
    assert release_used == older
    assert [c["release"] for c in considered] == [latest, older]
    assert considered[0]["viable"] is False
    assert (
        considered[0]["fallback_reason"]
        == "1 of 2 required challengers survived the pool rules"
    )
    assert considered[1]["viable"] is True

    rows = candidate_rows(db, refresh_id)
    assert row_for(rows, latest, "b/too-big", in_pool=False)[3] == "NO_SINGLE_GPU_FIT"
    assert row_for(rows, older, "a/challenger-a", in_pool=True)[3] is None

    # The pool is only what survived on the release used: survivors from
    # the rejected newest release must not leak in.
    pool = get_current_pool(db)
    assert [m["hf_repo"] for m in pool] == [
        INCUMBENT_REPO,
        "a/challenger-a",
        "b/challenger-b",
    ]


def test_refresh_consumes_blocklist_and_passes_slot(db, network):
    latest = "2026_06_25"
    blocklist = [
        BlocklistEntry(
            hf_repo="a/blocked-model",
            revision="rev-blocked-model",
            reason="Runs were not bit-identical across the repeat count.",
            round_reference="round-0001",
        )
    ]
    network(
        {
            latest: report(
                latest,
                [
                    descriptor("blocked-model", "a", 80.0, latest),
                    descriptor("family-sibling", "a", 76.0, latest),
                    descriptor("challenger-b", "b", 72.0, latest),
                ],
            )
        },
        blocklist=blocklist,
    )

    refresh_id = create_refresh(db)
    result = execute_refresh(db, refresh_id)

    assert result.status == STATUS_COMPLETED
    rows = candidate_rows(db, refresh_id)
    assert row_for(rows, latest, "a/blocked-model", in_pool=False)[3] == RULE_BLOCKLISTED
    assert row_for(rows, latest, "a/family-sibling", in_pool=True)[3] is None

    cursor = db.cursor()
    cursor.execute("SELECT hf_repo, revision, reason FROM blocklist")
    synced = cursor.fetchall()
    cursor.close()
    assert synced == [
        (
            "a/blocked-model",
            "rev-blocked-model",
            "Runs were not bit-identical across the repeat count.",
        )
    ]


def test_no_viable_pool_leaves_current_pool_unchanged(db, network):
    latest = "2026_06_25"
    network(
        {
            latest: report(
                latest,
                [
                    descriptor("challenger-a", "a", 75.0, latest),
                    descriptor("challenger-b", "b", 72.0, latest),
                ],
            )
        }
    )
    first_id = create_refresh(db)
    assert execute_refresh(db, first_id).status == STATUS_COMPLETED
    established_pool = get_current_pool(db)
    assert len(established_pool) == 3

    newer = "2026_07_20"
    network(
        {
            newer: report(
                newer,
                [descriptor("only-one", "a", 75.0, newer)],
            ),
            latest: report(
                latest,
                [descriptor("also-only-one", "b", 70.0, latest)],
            ),
        }
    )
    second_id = create_refresh(db)
    result = execute_refresh(db, second_id)

    assert result.status == STATUS_NO_VIABLE_POOL
    assert result.release_used is None

    status, release_used, considered, _, _ = refresh_row(db, second_id)
    assert status == STATUS_NO_VIABLE_POOL
    assert release_used is None
    assert len(considered) == 2
    assert all(c["viable"] is False for c in considered)

    assert get_current_pool(db) == established_pool

    rows = candidate_rows(db, second_id)
    assert row_for(rows, newer, "a/only-one", in_pool=True)[3] is None
    assert row_for(rows, latest, "b/also-only-one", in_pool=True)[3] is None


def test_upstream_failure_marks_refresh_failed(db, network, monkeypatch):
    network({})
    monkeypatch.setattr(
        livebench,
        "discover_releases",
        lambda client: (_ for _ in ()).throw(
            livebench.LiveBenchRequestError("upstream down")
        ),
    )

    refresh_id = create_refresh(db)
    result = execute_refresh(db, refresh_id)

    assert result is None
    status, _, _, error, completed_at = refresh_row(db, refresh_id)
    assert status == STATUS_FAILED
    assert "upstream down" in error
    assert completed_at is not None
    assert get_current_pool(db) == []


def test_unexpected_failure_marks_refresh_failed_after_aborted_transaction(
    db, monkeypatch
):
    """A persistence error mid-transaction must not strand the row RUNNING."""

    def poisoned_execute(connection, refresh_id):
        cursor = connection.cursor()
        try:
            cursor.execute("INSERT INTO pool_refreshes (no_such_column) VALUES (1)")
        except Exception:
            pass
        finally:
            cursor.close()
        raise RuntimeError("boom")

    monkeypatch.setattr(pool_api, "execute_refresh", poisoned_execute)
    monkeypatch.setattr(pool_api, "release_refresh_lock", MagicMock())

    refresh_id = create_refresh(db)
    pool_api._run_refresh_in_background(MagicMock(), refresh_id)

    status, _, _, error, completed_at = refresh_row(db, refresh_id)
    assert status == STATUS_FAILED
    assert error == "UNEXPECTED: boom"
    assert completed_at is not None
