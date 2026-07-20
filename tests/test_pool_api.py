"""Public read endpoints against a real database."""

from governance_service.models import (
    CandidateDescriptor,
    CandidateEvaluation,
    IncumbentMember,
    RefreshResult,
    ReleaseEvaluation,
    ReleaseOutcome,
    SnapshotFile,
)
from governance_service.services.pool_refresh import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NO_VIABLE_POOL,
    create_refresh,
    persist_result,
)
from governance_service.services.record_publisher import (
    PUBLICATION_FAILED,
    PUBLICATION_SKIPPED,
    publish_record,
)

RELEASE = "2026_06_25"
OLDER_RELEASE = "2026_01_08"

INCUMBENT = IncumbentMember(
    hf_repo="Qwen/Qwen3.6-27B-FP8",
    revision="incumbent-rev",
    precision="fp8",
    weight_bytes=29_000_000_000,
    license="apache-2.0",
    gated=False,
    assigned_gpu="H100",
    livebench_key="qwen3.6-27b",
    display_name="Qwen3.6 27B",
    family="qwen",
    global_average=64.0,
    category_averages={"reasoning": 64.0},
)

SNAPSHOTS = [
    SnapshotFile(
        name=f"table_{RELEASE}.csv", sha256="a" * 64, size_bytes=10, content=b"csv"
    )
]


def challenger(
    key: str,
    family: str,
    average: float,
    release: str = RELEASE,
    in_pool: bool = True,
    exclusion_rule: str | None = None,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        descriptor=CandidateDescriptor(
            livebench_key=key,
            display_name=key.title(),
            organization=family.title(),
            family=family,
            global_average=average,
            category_averages={"reasoning": average},
            hf_repo=f"{family}/{key}",
            revision=f"rev-{key}",
            precision="fp8",
            weight_bytes=30_000_000_000,
            license="apache-2.0",
            gated=False,
            assigned_gpu="H100",
            release=release,
        ),
        in_pool=in_pool,
        exclusion_rule=exclusion_rule,
    )


def viable_result(release: str = RELEASE) -> RefreshResult:
    return RefreshResult(
        status=STATUS_COMPLETED,
        release_used=release,
        incumbent=INCUMBENT,
        releases=[
            ReleaseEvaluation(
                outcome=ReleaseOutcome(
                    release=release,
                    challenger_count=2,
                    viable=True,
                    fallback_reason=None,
                    unmapped=[],
                    skipped={},
                ),
                evaluations=[
                    challenger("challenger-a", "a", 75.0, release),
                    challenger("challenger-b", "b", 72.0, release),
                    challenger(
                        "too-big",
                        "c",
                        71.0,
                        release,
                        in_pool=False,
                        exclusion_rule="NO_SINGLE_GPU_FIT",
                    ),
                ],
            )
        ],
        snapshots=SNAPSHOTS,
    )


def no_viable_result() -> RefreshResult:
    return RefreshResult(
        status=STATUS_NO_VIABLE_POOL,
        release_used=None,
        incumbent=INCUMBENT,
        releases=[
            ReleaseEvaluation(
                outcome=ReleaseOutcome(
                    release=RELEASE,
                    challenger_count=1,
                    viable=False,
                    fallback_reason="1 of 2 required challengers survived the pool rules",
                    unmapped=["unmapped-model"],
                    skipped={},
                ),
                evaluations=[challenger("only-one", "a", 75.0)],
            )
        ],
        snapshots=SNAPSHOTS,
    )


def seed_refresh(connection, result: RefreshResult) -> int:
    refresh_id = create_refresh(connection)
    persist_result(connection, refresh_id, result)
    publish_record(connection, refresh_id, result)
    return refresh_id


class TestPool:
    def test_returns_404_on_empty_database(self, db, client):
        response = client.get("/api/governance/pool")
        assert response.status_code == 404
        assert "No completed pool refresh" in response.json()["error"]

    def test_serves_latest_completed_refresh(self, db, client):
        refresh_id = seed_refresh(db, viable_result())

        response = client.get("/api/governance/pool")
        assert response.status_code == 200
        body = response.json()
        assert body["refresh_id"] == refresh_id
        assert body["release_used"] == RELEASE
        assert body["completed_at"] is not None
        assert [m["hf_repo"] for m in body["pool"]] == [
            "Qwen/Qwen3.6-27B-FP8",
            "a/challenger-a",
            "b/challenger-b",
        ]
        assert body["pool"][0]["is_incumbent"] is True

    def test_pool_stands_after_no_viable_refresh(self, db, client):
        completed_id = seed_refresh(db, viable_result())
        seed_refresh(db, no_viable_result())

        response = client.get("/api/governance/pool")
        assert response.status_code == 200
        assert response.json()["refresh_id"] == completed_id
        assert response.json()["release_used"] == RELEASE


class TestRefreshes:
    def test_list_is_paginated_newest_first(self, db, client):
        first = seed_refresh(db, viable_result(OLDER_RELEASE))
        second = seed_refresh(db, viable_result())
        third = seed_refresh(db, no_viable_result())

        response = client.get("/api/governance/refreshes?limit=2")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert body["limit"] == 2
        assert [r["id"] for r in body["refreshes"]] == [third, second]
        assert body["refreshes"][0]["status"] == STATUS_NO_VIABLE_POOL
        assert body["refreshes"][0]["publication_status"] == PUBLICATION_SKIPPED

        response = client.get("/api/governance/refreshes?limit=2&offset=2")
        assert [r["id"] for r in response.json()["refreshes"]] == [first]

    def test_detail_includes_walk_and_candidates(self, db, client):
        refresh_id = seed_refresh(db, no_viable_result())

        response = client.get(f"/api/governance/refreshes/{refresh_id}")
        assert response.status_code == 200
        body = response.json()

        refresh = body["refresh"]
        assert refresh["id"] == refresh_id
        assert refresh["status"] == STATUS_NO_VIABLE_POOL
        assert refresh["releases_considered"][0]["release"] == RELEASE
        assert refresh["releases_considered"][0]["unmapped"] == ["unmapped-model"]
        assert refresh["snapshots"][0]["sha256"] == "a" * 64
        assert "content" not in refresh["snapshots"][0]

        by_key = {c["livebench_key"]: c for c in body["candidates"]}
        assert by_key["only-one"]["in_pool"] is True
        incumbent = by_key["qwen3.6-27b"]
        assert incumbent["is_incumbent"] is True
        assert incumbent["revision"] == "incumbent-rev"

    def test_detail_exposes_exclusion_rules(self, db, client):
        refresh_id = seed_refresh(db, viable_result())

        response = client.get(f"/api/governance/refreshes/{refresh_id}")
        by_key = {c["livebench_key"]: c for c in response.json()["candidates"]}
        assert by_key["too-big"]["in_pool"] is False
        assert by_key["too-big"]["exclusion_rule"] == "NO_SINGLE_GPU_FIT"

    def test_detail_returns_404_for_unknown_refresh(self, db, client):
        response = client.get("/api/governance/refreshes/999999")
        assert response.status_code == 404
        assert "not found" in response.json()["error"]


class TestBlocklist:
    def test_empty_blocklist(self, db, client):
        response = client.get("/api/governance/blocklist")
        assert response.status_code == 200
        assert response.json() == {"blocklist": []}

    def test_populated_blocklist(self, db, client):
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO blocklist (hf_repo, revision, reason, round_reference)
            VALUES (%s, %s, %s, %s)
            """,
            ("org/model", "abc123", "Failed the repeat check.", "round-0001"),
        )
        db.commit()

        response = client.get("/api/governance/blocklist")
        entries = response.json()["blocklist"]
        assert len(entries) == 1
        assert entries[0]["hf_repo"] == "org/model"
        assert entries[0]["revision"] == "abc123"
        assert entries[0]["created_at"] is not None


class TestPipelineHealth:
    def test_no_refreshes_yet(self, db, client):
        response = client.get("/api/governance/health")
        assert response.status_code == 200
        body = response.json()
        assert body["latest_refresh"] == {
            "healthy": False,
            "detail": "no refreshes yet",
        }
        assert body["record_publication"] == {
            "healthy": True,
            "detail": "no publications yet",
        }

    def test_healthy_after_completed_refresh(self, db, client):
        refresh_id = seed_refresh(db, viable_result())

        body = client.get("/api/governance/health").json()
        assert body["latest_refresh"]["healthy"] is True
        assert f"refresh {refresh_id} {STATUS_COMPLETED}" in body["latest_refresh"]["detail"]
        assert body["record_publication"]["healthy"] is True
        assert PUBLICATION_SKIPPED in body["record_publication"]["detail"]

    def test_running_refresh_is_healthy(self, db, client):
        refresh_id = create_refresh(db)

        body = client.get("/api/governance/health").json()
        assert body["latest_refresh"]["healthy"] is True
        assert f"refresh {refresh_id} RUNNING" in body["latest_refresh"]["detail"]

    def test_publication_failure_is_independent_of_refresh_health(self, db, client):
        refresh_id = seed_refresh(db, viable_result())
        cursor = db.cursor()
        cursor.execute(
            "UPDATE pool_refreshes SET publication_status = %s WHERE id = %s",
            (PUBLICATION_FAILED, refresh_id),
        )
        db.commit()

        body = client.get("/api/governance/health").json()
        assert body["latest_refresh"]["healthy"] is True
        assert body["record_publication"]["healthy"] is False

    def test_unhealthy_after_failures(self, db, client):
        refresh_id = seed_refresh(db, viable_result())
        cursor = db.cursor()
        cursor.execute(
            "UPDATE pool_refreshes SET status = %s, publication_status = %s WHERE id = %s",
            (STATUS_FAILED, PUBLICATION_FAILED, refresh_id),
        )
        db.commit()

        body = client.get("/api/governance/health").json()
        assert body["latest_refresh"]["healthy"] is False
        assert body["record_publication"]["healthy"] is False
        assert (
            f"refresh {refresh_id} publication {PUBLICATION_FAILED}"
            in body["record_publication"]["detail"]
        )
