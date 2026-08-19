"""Output withholding and final publication: hold, record, receipt, repo."""

from datetime import datetime, timedelta, timezone

import pytest

from governance_service.config import settings
from governance_service.scoring import canonical_json_hash
from governance_service.services.announcement import ROUND_RECEIPT_TYPE
from governance_service.services.final_publication import (
    FinalPublicationError,
    build_final_record,
    hold_outputs,
    publish_round_record,
    record_paths,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)

PACKAGE_HASH = "ab" * 32
# In the past so the publication path's withholding guard is satisfied.
COMMIT_CLOSES = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)


class FakeReceiptClient:
    def __init__(self, *, success: bool = True, sequence: list[str] | None = None):
        self.success = success
        self.submitted: list[dict] = []
        self.sequence = sequence

    def submit_memo(self, memo_data: str, memo_type: str):
        self.submitted.append({"memo_data": memo_data, "memo_type": memo_type})
        if self.sequence is not None:
            self.sequence.append("receipt")
        if not self.success:
            return False, None, None, "tec_FAILURE"
        return True, "RECEIPTTX", 6000, None


class FakeRecordsClient:
    def __init__(self, sequence: list[str] | None = None):
        self.published: list[tuple[str, str, str]] = []
        self.sequence = sequence

    def publish(self, file_path: str, content: str, commit_message: str) -> str:
        self.published.append((file_path, content, commit_message))
        if self.sequence is not None:
            self.sequence.append("repo")
        return f"https://github.com/example/commit/{len(self.published)}"


def _round(
    db,
    status: RoundState = RoundState.DECIDED,
    round_number: int = 1,
    commit_closes_at: datetime | None = COMMIT_CLOSES,
    package: bool = True,
) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, package_cid, package_hash,
             frozen_at, announcement_tx_hash, announcement_ledger_index,
             commit_opens_at, commit_closes_at, judge_hf_repo,
             draw_ledger_index, draw_ledger_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            round_number,
            status.value,
            TRIGGER_MANUAL,
            "QmPackage" if package else None,
            PACKAGE_HASH if package else None,
            COMMIT_CLOSES - timedelta(days=2) if package else None,
            "TXHASH123",
            5_000_000,
            COMMIT_CLOSES - timedelta(days=2),
            commit_closes_at,
            "Qwen/Qwen3-32B-FP8",
            5_000_010,
            "C" * 64,
        ),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


def _seed_runs(db, round_id: int) -> tuple[int, int]:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO exam_runs
            (hf_repo, revision, profile_hash, corpus_hash, status, round_id,
             verdict, verdict_evidence)
        VALUES ('org/model-a', %s, 'p1', 'c1', 'COMPLETED', %s,
                'SURVIVED', '{"rules": "all pass"}')
        RETURNING id
        """,
        ("aa" * 20, round_id),
    )
    exam_run_id = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO governance_round_exam_runs (round_id, hf_repo, run_id)
        VALUES (%s, 'org/model-a', %s)
        """,
        (round_id, exam_run_id),
    )
    cursor.execute(
        """
        INSERT INTO exam_outputs
            (run_id, item_id, attempt, response_hash, raw_response, latency_seconds)
        VALUES (%s, 'edge:alpha', 1, %s, '{"v001": {"score": 91}}', 1.5)
        """,
        (exam_run_id, "dd" * 32),
    )
    cursor.execute(
        """
        INSERT INTO grading_runs
            (hf_repo, revision, profile_hash, material_hash, status, round_id)
        VALUES ('org/judge', %s, 'p2', 'm1', 'COMPLETED', %s)
        RETURNING id
        """,
        ("bb" * 20, round_id),
    )
    grading_run_id = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO governance_round_grading_runs (round_id, run_id, hf_repo, outcome)
        VALUES (%s, %s, 'org/judge', 'PASSED')
        """,
        (round_id, grading_run_id),
    )
    cursor.execute(
        """
        INSERT INTO grading_outputs
            (run_id, item_id, answer_hash, attempt, response_hash, raw_response,
             latency_seconds)
        VALUES (%s, 'edge:alpha', %s, 1, %s, '{"defects": []}', 0.8)
        """,
        (grading_run_id, "ee" * 32, "ff" * 32),
    )
    db.commit()
    cursor.close()
    return exam_run_id, grading_run_id


class TestHold:
    def test_hold_requires_a_recorded_commit_close(self, db):
        round_id = _round(db, status=RoundState.GRADED, commit_closes_at=None)

        with pytest.raises(FinalPublicationError, match="never release"):
            hold_outputs(db, round_id)

    def test_hold_accepts_a_round_with_commit_close(self, db):
        round_id = _round(db, status=RoundState.GRADED)

        hold_outputs(db, round_id)

    def test_resumed_graded_round_parks(self, db):
        _round(db, status=RoundState.GRADED)

        results = RoundOrchestrator().resume_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.AWAITING_COMMIT_CLOSE.value

    def test_graded_round_without_commit_close_fails_to_park(self, db):
        _round(db, status=RoundState.GRADED, commit_closes_at=None)

        results = RoundOrchestrator().resume_rounds()

        assert results[0]["status"] == RoundState.FAILED.value
        assert "never release" in results[0]["error"]


class TestRecordAssembly:
    def test_record_covers_round_runs_and_raw_outputs(self, db):
        round_id = _round(db)
        exam_run_id, grading_run_id = _seed_runs(db, round_id)

        files, bundle = build_final_record(db, round_id, 1)

        assert bundle["package_kind"] == "governance_round_record"
        assert bundle["round_number"] == 1
        assert set(bundle["file_hashes"]) == set(files)
        for path, digest in bundle["file_hashes"].items():
            assert digest == canonical_json_hash(files[path])

        assert files["round.json"]["package_cid"] == "QmPackage"
        assert files["round.json"]["judge_hf_repo"] == "Qwen/Qwen3-32B-FP8"
        assert files["exam/runs.json"]["runs"][0]["verdict"] == "SURVIVED"
        outputs = files[f"exam/outputs/run-{exam_run_id}.json"]["outputs"]
        assert outputs[0]["raw_response"] == '{"v001": {"score": 91}}'
        grading = files[f"grading/outputs/run-{grading_run_id}.json"]["outputs"]
        assert grading[0]["raw_response"] == '{"defects": []}'

    def test_assembly_is_deterministic(self, db):
        round_id = _round(db)
        _seed_runs(db, round_id)

        _, first = build_final_record(db, round_id, 1)
        _, second = build_final_record(db, round_id, 1)

        assert canonical_json_hash(first) == canonical_json_hash(second)

    def test_runs_from_other_rounds_are_excluded(self, db):
        round_id = _round(db)
        other_round = _round(db, round_number=2)
        _seed_runs(db, other_round)

        files, _ = build_final_record(db, round_id, 1)

        assert files["exam/runs.json"] == {"runs": []}
        assert files["grading/runs.json"] == {"runs": []}

    def test_record_pins_through_the_real_serializer(self, db, monkeypatch):
        round_id = _round(db)
        _seed_runs(db, round_id)
        files, bundle = build_final_record(db, round_id, 1)

        from governance_service.services import round_package

        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test")
        pinned = {}
        monkeypatch.setattr(
            round_package.IPFSClient,
            "pin_directory",
            lambda self, payload: pinned.update(payload) or "QmReal",
        )

        assert round_package.pin_package(files, bundle, "record-pin") == "QmReal"
        assert set(pinned) == set(files) | {round_package.BUNDLE_FILE_PATH}


class TestPublishRoundRecord:
    def test_publish_pins_receipts_and_records(self, db, monkeypatch):
        monkeypatch.setattr(settings, "records_github_token", "tok")
        round_id = _round(db)
        _seed_runs(db, round_id)
        sequence: list[str] = []
        receipt_client = FakeReceiptClient(sequence=sequence)
        records_client = FakeRecordsClient(sequence=sequence)
        pins: list[str] = []

        result = publish_round_record(
            db,
            round_id,
            1,
            pin=lambda files, bundle, name: pins.append(name) or "QmRecord",
            pftl_client=receipt_client,
            records_client=records_client,
        )

        assert result["final_record_cid"] == "QmRecord"
        assert result["receipt_tx_hash"] == "RECEIPTTX"
        assert result["repo_record_skipped"] is False
        assert pins == [f"governance-round-record-{settings.environment}-1"]

        submission = receipt_client.submitted[0]
        assert submission["memo_type"] == ROUND_RECEIPT_TYPE
        assert '"final_record_cid":"QmRecord"' in submission["memo_data"]

        json_path, md_path = record_paths(1)
        assert [p[0] for p in records_client.published] == [json_path, md_path]
        assert "QmRecord" in records_client.published[0][1]
        # The receipt anchors on chain before the repo record references it,
        # and no raw model output ever reaches the git record.
        assert sequence == ["receipt", "repo", "repo"]
        for _, content, _ in records_client.published:
            assert "raw_response" not in content
            assert '{"v001"' not in content

        cursor = db.cursor()
        cursor.execute(
            """
            SELECT final_record_cid, receipt_tx_hash, record_commit_url
            FROM governance_rounds WHERE id = %s
            """,
            (round_id,),
        )
        cid, receipt, commit_url = cursor.fetchone()
        cursor.close()
        assert (cid, receipt) == ("QmRecord", "RECEIPTTX")
        assert commit_url is not None

    def test_publication_is_skipped_without_records_token(self, db):
        round_id = _round(db)
        result = publish_round_record(
            db,
            round_id,
            1,
            pin=lambda files, bundle, name: "QmRecord",
            pftl_client=FakeReceiptClient(),
        )

        assert result["repo_record_skipped"] is True
        assert result["record_commit_url"] is None

    def test_pinned_record_is_not_repinned_on_resume(self, db):
        round_id = _round(db)
        cursor = db.cursor()
        cursor.execute(
            "UPDATE governance_rounds SET final_record_cid = 'QmAlready' WHERE id = %s",
            (round_id,),
        )
        db.commit()
        cursor.close()
        pins: list[str] = []

        result = publish_round_record(
            db,
            round_id,
            1,
            pin=lambda files, bundle, name: pins.append(name) or "QmNew",
            pftl_client=FakeReceiptClient(),
        )

        assert pins == []
        assert result["final_record_cid"] == "QmAlready"

    def test_landed_receipt_is_not_reemitted_on_resume(self, db):
        round_id = _round(db)
        cursor = db.cursor()
        cursor.execute(
            """
            UPDATE governance_rounds
            SET final_record_cid = 'QmAlready', receipt_tx_hash = 'OLDRECEIPT'
            WHERE id = %s
            """,
            (round_id,),
        )
        db.commit()
        cursor.close()
        receipt_client = FakeReceiptClient()

        result = publish_round_record(
            db,
            round_id,
            1,
            pin=lambda files, bundle, name: "QmNew",
            pftl_client=receipt_client,
        )

        assert receipt_client.submitted == []
        assert result["receipt_tx_hash"] == "OLDRECEIPT"

    def test_receipt_failure_raises(self, db):
        round_id = _round(db)

        with pytest.raises(FinalPublicationError, match="tec_FAILURE"):
            publish_round_record(
                db,
                round_id,
                1,
                pin=lambda files, bundle, name: "QmRecord",
                pftl_client=FakeReceiptClient(success=False),
            )

    def test_unfrozen_round_cannot_publish(self, db):
        round_id = _round(db, package=False)

        with pytest.raises(FinalPublicationError, match="no frozen package identity"):
            publish_round_record(
                db, round_id, 1, pftl_client=FakeReceiptClient()
            )


class TestPublicationPipeline:
    def test_parked_round_publishes_after_decide(self, db, monkeypatch):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        round_id = _round(
            db, status=RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=past
        )
        _seed_runs(db, round_id)

        from governance_service.services import final_publication

        monkeypatch.setattr(
            final_publication, "PFTLClient", lambda: FakeReceiptClient()
        )
        monkeypatch.setattr(
            final_publication.round_package,
            "pin_package",
            lambda files, bundle, name: "QmPipeline",
        )

        class _DecideOnly(RoundOrchestrator):
            def _decide(self, conn, round_ctx):
                pass

        results = _DecideOnly().publish_due_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.COMPLETE.value
        cursor = db.cursor()
        cursor.execute(
            "SELECT final_record_cid, receipt_tx_hash, status FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        cid, receipt, status = cursor.fetchone()
        cursor.close()
        assert (cid, receipt, status) == (
            "QmPipeline",
            "RECEIPTTX",
            RoundState.COMPLETE.value,
        )

    def test_publication_failure_leaves_the_round_retryable(self, db, monkeypatch):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        round_id = _round(
            db, status=RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=past
        )

        from governance_service.services import final_publication

        monkeypatch.setattr(
            final_publication, "PFTLClient", lambda: FakeReceiptClient(success=False)
        )
        monkeypatch.setattr(
            final_publication.round_package,
            "pin_package",
            lambda files, bundle, name: "QmPipeline",
        )

        class _DecideOnly(RoundOrchestrator):
            def _decide(self, conn, round_ctx):
                pass

        results = _DecideOnly().publish_due_rounds()

        # The receipt failed after the pin landed: the round stays in its
        # publication state for the next tick instead of going FAILED.
        assert results[0]["status"] == RoundState.DECIDED.value
        assert "tec_FAILURE" in results[0]["error"]
        cursor = db.cursor()
        cursor.execute(
            "SELECT status, final_record_cid FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        status, cid = cursor.fetchone()
        cursor.close()
        assert status == RoundState.DECIDED.value
        assert cid == "QmPipeline"
