"""Record rendering and publication state against a real database."""

import json

import pytest

from governance_service.clients.github_records import GitHubRecordsError
from governance_service.config import settings
from governance_service.models import (
    CandidateDescriptor,
    CandidateEvaluation,
    IncumbentMember,
    RefreshResult,
    ReleaseEvaluation,
    ReleaseOutcome,
    SnapshotFile,
)
from governance_service.services import record_publisher
from governance_service.services.pool_refresh import (
    STATUS_COMPLETED,
    STATUS_NO_VIABLE_POOL,
    create_refresh,
    get_current_pool,
    persist_result,
)
from governance_service.services.record_publisher import (
    PUBLICATION_FAILED,
    PUBLICATION_PUBLISHED,
    PUBLICATION_SKIPPED,
    publish_record,
    record_paths,
    render_record,
    render_summary,
)

RELEASE = "2026_06_25"

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
        name=f"table_{RELEASE}.csv",
        sha256="a" * 64,
        size_bytes=10,
        content=b"csv-bytes!",
    ),
    SnapshotFile(
        name="modelLinks.js",
        sha256="b" * 64,
        size_bytes=8,
        content=b"registry",
    ),
]


def challenger(key: str, family: str, average: float) -> CandidateEvaluation:
    return CandidateEvaluation(
        descriptor=CandidateDescriptor(
            livebench_key=key,
            display_name=key.title(),
            organization=family.title(),
            family=family,
            thinking="hybrid",
            global_average=average,
            category_averages={"reasoning": average},
            hf_repo=f"{family}/{key}",
            revision=f"rev-{key}",
            precision="fp8",
            weight_bytes=30_000_000_000,
            license="apache-2.0",
            gated=False,
            assigned_gpu="H100",
            release=RELEASE,
        ),
        in_pool=True,
    )


def viable_result() -> RefreshResult:
    evaluations = [challenger("challenger-a", "a", 75.0), challenger("challenger-b", "b", 72.0)]
    return RefreshResult(
        status=STATUS_COMPLETED,
        release_used=RELEASE,
        incumbent=INCUMBENT,
        releases=[
            ReleaseEvaluation(
                outcome=ReleaseOutcome(
                    release=RELEASE,
                    challenger_count=2,
                    viable=True,
                    fallback_reason=None,
                    unmapped=["some-unmapped"],
                    skipped={},
                ),
                evaluations=evaluations,
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
                    unmapped=[],
                    skipped={},
                ),
                evaluations=[challenger("only-one", "a", 75.0)],
            )
        ],
        snapshots=SNAPSHOTS,
    )


def publication_row(connection, refresh_id: int) -> tuple:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT publication_status, publication_error, snapshots_cid,
               record_commit_urls
        FROM pool_refreshes
        WHERE id = %s
        """,
        (refresh_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def persisted_refresh(connection, result: RefreshResult) -> int:
    refresh_id = create_refresh(connection)
    persist_result(connection, refresh_id, result)
    return refresh_id


class TestRendering:
    def test_viable_record_carries_pool_and_audit(self, db):
        refresh_id = persisted_refresh(db, viable_result())
        record = render_record(
            refresh_id, viable_result(), *_times(db, refresh_id), "bafy-cid"
        )

        assert record["status"] == STATUS_COMPLETED
        assert record["release_used"] == RELEASE
        assert [m["role"] for m in record["pool"]] == [
            "incumbent",
            "challenger",
            "challenger",
        ]
        assert record["pool"][0]["hf_repo"] == "Qwen/Qwen3.6-27B-FP8"
        assert record["pool"][1]["precision"] == "fp8"

        considered = record["releases_considered"]
        assert len(considered) == 1
        assert considered[0]["unmapped"] == ["some-unmapped"]
        assert considered[0]["candidates"][0]["in_pool"] is True

        snapshots = record["upstream_snapshots"]
        assert snapshots["cid"] == "bafy-cid"
        assert all("content" not in f for f in snapshots["files"])
        assert snapshots["files"][0]["sha256"] == "a" * 64

    def test_no_viable_record_has_no_pool(self, db):
        refresh_id = persisted_refresh(db, no_viable_result())
        record = render_record(
            refresh_id, no_viable_result(), *_times(db, refresh_id), None
        )

        assert record["status"] == STATUS_NO_VIABLE_POOL
        assert record["pool"] is None
        assert record["release_used"] is None
        assert record["upstream_snapshots"]["cid"] is None

    def test_summaries_render_both_outcomes(self, db):
        refresh_id = persisted_refresh(db, viable_result())
        viable_summary = render_summary(
            render_record(refresh_id, viable_result(), *_times(db, refresh_id), "cid"),
            "records/pool-refreshes/local/record.json",
        )
        assert "| incumbent |" in viable_summary
        assert "Qwen/Qwen3.6-27B-FP8@incumbent-re" in viable_summary
        assert "| 2026_06_25 | yes | 2 | - |" in viable_summary
        assert "`records/pool-refreshes/local/record.json`" in viable_summary

        no_viable_summary = render_summary(
            render_record(refresh_id, no_viable_result(), *_times(db, refresh_id), None),
            "records/pool-refreshes/local/record.json",
        )
        assert "standing pool is unchanged" in no_viable_summary
        assert "1 of 2 required challengers survived" in no_viable_summary

    def test_summary_escapes_upstream_controlled_cells(self, db):
        refresh_id = persisted_refresh(db, viable_result())
        result = viable_result()
        result.releases[0].evaluations[0].descriptor.display_name = "Bad|Name\nHere"
        summary = render_summary(
            render_record(refresh_id, result, *_times(db, refresh_id), None),
            "record.json",
        )
        assert "Bad\\|Name Here" in summary

    def test_record_paths_carry_environment_date_and_id(self, db):
        refresh_id = persisted_refresh(db, viable_result())
        _, completed_at = _times(db, refresh_id)
        json_path, md_path = record_paths(refresh_id, completed_at)
        date = completed_at.date().isoformat()
        assert json_path == (
            f"records/pool-refreshes/local/{date}-refresh-{refresh_id}.json"
        )
        assert md_path == f"records/pool-refreshes/local/{date}-refresh-{refresh_id}.md"


class TestPublication:
    def test_skipped_without_records_token(self, db):
        refresh_id = persisted_refresh(db, viable_result())
        publish_record(db, refresh_id, viable_result())

        status, error, cid, urls = publication_row(db, refresh_id)
        assert status == PUBLICATION_SKIPPED
        assert error is None and cid is None and urls is None

    def test_success_publishes_both_files(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test:5001")

        published = []

        class FakeRecordsClient:
            def publish(self, path, content, message):
                published.append((path, content, message))
                return f"https://github.com/commit/{len(published)}"

        class FakeIPFSClient:
            def pin_directory(self, files):
                assert files == {snap.name: snap.content for snap in SNAPSHOTS}
                return "bafy-snapshots"

        monkeypatch.setattr(
            record_publisher, "GitHubRecordsClient", lambda: FakeRecordsClient()
        )
        monkeypatch.setattr(record_publisher, "IPFSClient", lambda: FakeIPFSClient())

        refresh_id = persisted_refresh(db, viable_result())
        publish_record(db, refresh_id, viable_result())

        status, error, cid, urls = publication_row(db, refresh_id)
        assert status == PUBLICATION_PUBLISHED
        assert error is None
        assert cid == "bafy-snapshots"
        assert urls == ["https://github.com/commit/1", "https://github.com/commit/2"]

        json_path, json_content, _ = published[0]
        assert json_path.endswith(f"-refresh-{refresh_id}.json")
        assert json.loads(json_content)["upstream_snapshots"]["cid"] == "bafy-snapshots"
        md_path, md_content, _ = published[1]
        assert md_path.endswith(f"-refresh-{refresh_id}.md")
        assert md_content.startswith(f"# Pool refresh {refresh_id}")

    def test_github_failure_is_visible_and_pool_stands(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")

        class FailingRecordsClient:
            def publish(self, path, content, message):
                raise GitHubRecordsError("contents API rejected the publish")

        monkeypatch.setattr(
            record_publisher, "GitHubRecordsClient", lambda: FailingRecordsClient()
        )

        refresh_id = persisted_refresh(db, viable_result())
        pool_before = get_current_pool(db)
        publish_record(db, refresh_id, viable_result())

        status, error, _, urls = publication_row(db, refresh_id)
        assert status == PUBLICATION_FAILED
        assert "contents API rejected" in error
        assert urls is None
        assert get_current_pool(db) == pool_before

    def test_pin_failure_marks_publication_failed(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test:5001")

        class FailingIPFSClient:
            def pin_directory(self, files):
                return None

        monkeypatch.setattr(
            record_publisher, "IPFSClient", lambda: FailingIPFSClient()
        )

        refresh_id = persisted_refresh(db, viable_result())
        publish_record(db, refresh_id, viable_result())

        status, error, cid, _ = publication_row(db, refresh_id)
        assert status == PUBLICATION_FAILED
        assert "pinning failed" in error
        assert cid is None

    def test_primary_pin_failure_falls_back_to_pinata(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test:5001")
        monkeypatch.setattr(settings, "pinata_api_key", "key")
        monkeypatch.setattr(settings, "pinata_api_secret", "secret")

        class FailingIPFSClient:
            def pin_directory(self, files):
                return None

        class FallbackPinataClient:
            def pin_directory(self, files, name=None):
                assert name == "pool-refresh-local-{}-snapshots".format(refresh_id)
                return "bafy-pinata-fallback"

        class FakeRecordsClient:
            def publish(self, path, content, message):
                return "https://github.com/commit/1"

        monkeypatch.setattr(
            record_publisher, "IPFSClient", lambda: FailingIPFSClient()
        )
        monkeypatch.setattr(
            record_publisher, "PinataClient", lambda: FallbackPinataClient()
        )
        monkeypatch.setattr(
            record_publisher, "GitHubRecordsClient", lambda: FakeRecordsClient()
        )

        refresh_id = persisted_refresh(db, viable_result())
        publish_record(db, refresh_id, viable_result())

        status, error, cid, _ = publication_row(db, refresh_id)
        assert status == PUBLICATION_PUBLISHED
        assert error is None
        assert cid == "bafy-pinata-fallback"

    def test_publishes_no_viable_pool_record(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")

        published = []

        class FakeRecordsClient:
            def publish(self, path, content, message):
                published.append((path, content))
                return f"https://github.com/commit/{len(published)}"

        monkeypatch.setattr(
            record_publisher, "GitHubRecordsClient", lambda: FakeRecordsClient()
        )

        refresh_id = persisted_refresh(db, no_viable_result())
        publish_record(db, refresh_id, no_viable_result())

        status, error, _, urls = publication_row(db, refresh_id)
        assert status == PUBLICATION_PUBLISHED
        assert error is None
        assert len(urls) == 2
        assert json.loads(published[0][1])["pool"] is None
        assert "standing pool is unchanged" in published[1][1]

    def test_failure_preserves_pinned_cid(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "test-token")
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test:5001")

        class WorkingIPFSClient:
            def pin_directory(self, files):
                return "bafy-pinned-before-failure"

        class FailingRecordsClient:
            def publish(self, path, content, message):
                raise GitHubRecordsError("publish rejected")

        monkeypatch.setattr(
            record_publisher, "IPFSClient", lambda: WorkingIPFSClient()
        )
        monkeypatch.setattr(
            record_publisher, "GitHubRecordsClient", lambda: FailingRecordsClient()
        )

        refresh_id = persisted_refresh(db, viable_result())
        publish_record(db, refresh_id, viable_result())

        status, error, cid, urls = publication_row(db, refresh_id)
        assert status == PUBLICATION_FAILED
        assert "publish rejected" in error
        assert cid == "bafy-pinned-before-failure"
        assert urls is None


def _times(connection, refresh_id: int):
    cursor = connection.cursor()
    cursor.execute(
        "SELECT started_at, completed_at FROM pool_refreshes WHERE id = %s",
        (refresh_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row[0], row[1] or row[0]
