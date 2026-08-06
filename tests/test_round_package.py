"""The frozen round package: assembly, eligibility, pinning, persistence."""

from datetime import datetime, timezone

import pytest

from governance_service.config import settings
from governance_service.scoring import canonical_json_hash
from governance_service.services import edge_cases, round_package
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.corpus import CorpusResult
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)
from governance_service.services.pool_refresh import STATUS_COMPLETED
from governance_service.services.round_package import (
    BUNDLE_FILE_PATH,
    FreezeEligibilityError,
    FreezePinningError,
    FrozenPool,
    build_package,
    freeze_round,
    get_package_file,
    load_frozen_pool,
    pin_package,
)

INCUMBENT_REPO = "Qwen/Qwen3.6-27B-FP8"
CHALLENGER_REPOS = ("google/gemma-4-31B-it", "Qwen/Qwen3-32B-FP8")

FROZEN_AT = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _seed_refresh(db, members=None, incumbent=INCUMBENT_REPO) -> int:
    """A COMPLETED refresh whose in-pool members mirror the curated profiles."""
    members = members if members is not None else (INCUMBENT_REPO, *CHALLENGER_REPOS)
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO pool_refreshes (status, completed_at)
        VALUES (%s, NOW()) RETURNING id
        """,
        (STATUS_COMPLETED,),
    )
    refresh_id = cursor.fetchone()[0]
    for hf_repo in members:
        profile = CURRENT_POOL_PROFILES[hf_repo]
        cursor.execute(
            """
            INSERT INTO pool_refresh_candidates
                (refresh_id, hf_repo, revision, precision, weight_bytes,
                 gated, is_incumbent, in_pool)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (
                refresh_id,
                hf_repo,
                profile.revision,
                "fp8",
                1,
                False,
                hf_repo == incumbent,
            ),
        )
    db.commit()
    cursor.close()
    return refresh_id


def _corpus() -> CorpusResult:
    return CorpusResult(
        manifest={
            "manifest_version": 1,
            "environment": settings.environment,
            "policy": {"history_window_requested": 12, "history_rounds_found": 1},
            "historical": [
                {
                    "round_number": 42,
                    "input_package_cid": "QmTest",
                    "input_package_hash": "ab" * 32,
                    "input_frozen_at": "2026-07-01T00:00:00+00:00",
                    "verified_file_count": 5,
                }
            ],
        },
        constructed=edge_cases.build_all(),
    )


def _pool() -> FrozenPool:
    return FrozenPool(
        refresh_id=1,
        incumbent=CURRENT_POOL_PROFILES[INCUMBENT_REPO],
        challengers=[CURRENT_POOL_PROFILES[r] for r in CHALLENGER_REPOS],
    )


class TestEligibility:
    def test_empty_pool_is_ineligible(self, db):
        with pytest.raises(FreezeEligibilityError, match="No completed pool refresh"):
            load_frozen_pool(db)

    def test_missing_incumbent_is_ineligible(self, db):
        _seed_refresh(db, incumbent="none-of-them")
        with pytest.raises(FreezeEligibilityError, match="no incumbent"):
            load_frozen_pool(db)

    def test_single_challenger_is_ineligible(self, db):
        _seed_refresh(db, members=(INCUMBENT_REPO, CHALLENGER_REPOS[0]))
        with pytest.raises(FreezeEligibilityError, match="requires at least 2"):
            load_frozen_pool(db)

    def test_unmapped_member_is_ineligible(self, db):
        refresh_id = _seed_refresh(db)
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO pool_refresh_candidates
                (refresh_id, hf_repo, revision, precision, weight_bytes,
                 gated, is_incumbent, in_pool)
            VALUES (%s, 'org/unknown-model', %s, 'fp8', 1, FALSE, FALSE, TRUE)
            """,
            (refresh_id, "cd" * 20),
        )
        db.commit()
        cursor.close()
        with pytest.raises(FreezeEligibilityError, match="no deployable runtime profile"):
            load_frozen_pool(db)

    def test_revision_mismatch_is_ineligible(self, db):
        refresh_id = _seed_refresh(db, members=CHALLENGER_REPOS)
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO pool_refresh_candidates
                (refresh_id, hf_repo, revision, precision, weight_bytes,
                 gated, is_incumbent, in_pool)
            VALUES (%s, %s, %s, 'fp8', 1, FALSE, TRUE, TRUE)
            """,
            (refresh_id, INCUMBENT_REPO, "ef" * 20),
        )
        db.commit()
        cursor.close()
        with pytest.raises(FreezeEligibilityError, match="revision mismatch"):
            load_frozen_pool(db)

    def test_full_pool_loads_with_profiles(self, db):
        _seed_refresh(db)

        pool = load_frozen_pool(db)

        assert pool.incumbent.hf_repo == INCUMBENT_REPO
        assert [c.hf_repo for c in pool.challengers] == sorted(CHALLENGER_REPOS)

    def test_eligibility_error_carries_evidence(self, db):
        _seed_refresh(db, members=(INCUMBENT_REPO,))
        with pytest.raises(FreezeEligibilityError) as excinfo:
            load_frozen_pool(db)
        assert excinfo.value.evidence["challengers"] == 0


class TestAssembly:
    def test_bundle_covers_every_file_and_excludes_itself(self):
        files, bundle = build_package(7, _corpus(), _pool(), FROZEN_AT)

        assert bundle["package_kind"] == "governance_round"
        assert bundle["round_number"] == 7
        assert BUNDLE_FILE_PATH not in files
        assert BUNDLE_FILE_PATH not in bundle["file_hashes"]
        assert set(bundle["file_hashes"]) == set(files)
        for path, digest in bundle["file_hashes"].items():
            assert digest == canonical_json_hash(files[path])

    def test_assembly_is_deterministic(self):
        first_files, first_bundle = build_package(7, _corpus(), _pool(), FROZEN_AT)
        second_files, second_bundle = build_package(7, _corpus(), _pool(), FROZEN_AT)

        assert first_files == second_files
        assert canonical_json_hash(first_bundle) == canonical_json_hash(second_bundle)

    def test_package_carries_the_frozen_round_inputs(self):
        files, _ = build_package(7, _corpus(), _pool(), FROZEN_AT)

        assert files["corpus/manifest.json"]["historical"][0]["input_package_cid"] == "QmTest"
        assert any(path.startswith("corpus/edge_cases/") for path in files)

        pool_file = files["pool/candidates.json"]
        assert pool_file["incumbent"]["profile"]["hf_repo"] == INCUMBENT_REPO
        assert len(pool_file["challengers"]) == 2

        assert files["grading/prompt.json"]["version"] == 2
        assert "### SYSTEM PROMPT ###" in files["grading/prompt.json"]["text"]
        assert files["grading/grade_formula.json"]["version"] == 1
        assert files["grading/checker_rules.json"]["rules"]
        assert files["rules/adaptation.json"]["version"] == 1

        parameters = files["round/parameters.json"]
        assert parameters["repeat_count"] == 3
        assert parameters["incumbent_margin_points"] == 5
        assert parameters["commit_window_seconds"] == settings.round_commit_window_seconds
        assert parameters["draw_procedure"]["ledger_offset"] == 10
        assert "judge_draw" in parameters["hash_set"]["members"]

    def test_frozen_at_changes_the_package_hash(self):
        _, first = build_package(7, _corpus(), _pool(), FROZEN_AT)
        later = datetime(2026, 8, 6, 13, 0, 0, tzinfo=timezone.utc)
        _, second = build_package(7, _corpus(), _pool(), later)

        assert canonical_json_hash(first) != canonical_json_hash(second)


class TestPinning:
    def _files_and_bundle(self):
        return build_package(7, _corpus(), _pool(), FROZEN_AT)

    def test_primary_pin_replicates_to_pinata(self, monkeypatch):
        files, bundle = self._files_and_bundle()
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test")
        monkeypatch.setattr(settings, "pinata_api_key", "key")
        monkeypatch.setattr(settings, "pinata_api_secret", "secret")
        replicated = {}
        monkeypatch.setattr(
            round_package.IPFSClient, "pin_directory", lambda self, payload: "QmPrimary"
        )
        monkeypatch.setattr(
            round_package.PinataClient,
            "pin_by_cid",
            lambda self, cid, name=None: replicated.update({"cid": cid, "name": name}) or True,
        )

        cid = pin_package(files, bundle, 7)

        assert cid == "QmPrimary"
        assert replicated["cid"] == "QmPrimary"

    def test_pinata_upload_is_the_write_fallback(self, monkeypatch):
        files, bundle = self._files_and_bundle()
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test")
        monkeypatch.setattr(settings, "pinata_api_key", "key")
        monkeypatch.setattr(settings, "pinata_api_secret", "secret")
        monkeypatch.setattr(
            round_package.IPFSClient, "pin_directory", lambda self, payload: None
        )
        monkeypatch.setattr(
            round_package.PinataClient,
            "pin_directory",
            lambda self, payload, name=None: "QmFallback",
        )

        assert pin_package(files, bundle, 7) == "QmFallback"

    def test_no_backend_fails_closed(self, monkeypatch):
        files, bundle = self._files_and_bundle()
        monkeypatch.setattr(settings, "ipfs_api_url", "")
        monkeypatch.setattr(settings, "pinata_api_key", "")
        monkeypatch.setattr(settings, "pinata_api_secret", "")

        with pytest.raises(FreezePinningError, match="no pinning backend"):
            pin_package(files, bundle, 7)

    def test_bundle_is_part_of_the_pinned_payload(self, monkeypatch):
        files, bundle = self._files_and_bundle()
        monkeypatch.setattr(settings, "ipfs_api_url", "http://ipfs.test")
        seen = {}
        monkeypatch.setattr(
            round_package.IPFSClient,
            "pin_directory",
            lambda self, payload: seen.update(payload) or "QmSeen",
        )

        pin_package(files, bundle, 7)

        assert BUNDLE_FILE_PATH in seen
        assert set(seen) == set(files) | {BUNDLE_FILE_PATH}


class TestFreezeRound:
    def _round(self, db, round_number=1) -> int:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (round_number, RoundState.CREATED.value, TRIGGER_MANUAL),
        )
        round_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()
        return round_id

    def test_freeze_persists_package_and_identity(self, db):
        _seed_refresh(db)
        round_id = self._round(db)

        result = freeze_round(
            db,
            round_id,
            1,
            corpus_builder=_corpus,
            pin=lambda files, bundle, n: "QmFrozen",
            now=FROZEN_AT,
        )

        cursor = db.cursor()
        cursor.execute(
            "SELECT package_cid, package_hash, frozen_at FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        package_cid, package_hash, frozen_at = cursor.fetchone()
        cursor.close()
        assert package_cid == "QmFrozen"
        assert package_hash == result["package_hash"]
        assert frozen_at == FROZEN_AT

        bundle = get_package_file(db, 1, BUNDLE_FILE_PATH)
        assert canonical_json_hash(bundle) == package_hash
        for path, digest in bundle["file_hashes"].items():
            assert canonical_json_hash(get_package_file(db, 1, path)) == digest

    def test_freeze_is_rerunnable_after_a_crash(self, db):
        _seed_refresh(db)
        round_id = self._round(db)
        kwargs = {
            "corpus_builder": _corpus,
            "pin": lambda files, bundle, n: "QmFrozen",
            "now": FROZEN_AT,
        }

        first = freeze_round(db, round_id, 1, **kwargs)
        second = freeze_round(db, round_id, 1, **kwargs)

        assert first["package_hash"] == second["package_hash"]
        cursor = db.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM governance_round_artifacts WHERE round_id = %s",
            (round_id,),
        )
        assert cursor.fetchone()[0] == first["files"]
        cursor.close()

    def test_orchestrator_runs_the_real_freeze(self, db, monkeypatch):
        _seed_refresh(db)
        monkeypatch.setattr(round_package, "_build_corpus_default", _corpus)
        monkeypatch.setattr(
            round_package, "pin_package", lambda files, bundle, n: "QmWired"
        )

        result = RoundOrchestrator().run_round(TRIGGER_MANUAL)

        # The freeze succeeds and the round fails at the next unbuilt stage.
        assert result["status"] == RoundState.FAILED.value
        assert "announcement" in result["error"]
        cursor = db.cursor()
        cursor.execute(
            "SELECT package_cid, error_message FROM governance_rounds WHERE round_number = %s",
            (result["round_number"],),
        )
        package_cid, error_message = cursor.fetchone()
        cursor.close()
        assert package_cid == "QmWired"
        assert error_message.startswith("FROZEN:")

    def test_ineligible_pool_fails_the_round_with_the_reason(self, db):
        result = RoundOrchestrator().run_round(TRIGGER_MANUAL)

        assert result["status"] == RoundState.FAILED.value
        cursor = db.cursor()
        cursor.execute(
            "SELECT error_message FROM governance_rounds WHERE round_number = %s",
            (result["round_number"],),
        )
        error_message = cursor.fetchone()[0]
        cursor.close()
        assert error_message.startswith("CREATED:")
        assert "No completed pool refresh" in error_message


class TestPackageRoutes:
    def _frozen_round(self, db):
        _seed_refresh(db)
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source)
            VALUES (1, %s, %s) RETURNING id
            """,
            (RoundState.FROZEN.value, TRIGGER_MANUAL),
        )
        round_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()
        freeze_round(
            db,
            round_id,
            1,
            corpus_builder=_corpus,
            pin=lambda files, bundle, n: "QmServed",
            now=FROZEN_AT,
        )

    def test_bundle_route_serves_the_manifest(self, db, client):
        self._frozen_round(db)

        response = client.get("/api/governance/rounds/1/package")

        assert response.status_code == 200
        body = response.json()
        assert body["package_kind"] == "governance_round"
        assert body["round_number"] == 1

    def test_file_route_serves_hash_matching_content(self, db, client):
        self._frozen_round(db)
        bundle = client.get("/api/governance/rounds/1/package").json()

        path, digest = next(iter(sorted(bundle["file_hashes"].items())))
        response = client.get(f"/api/governance/rounds/1/package/{path}")

        assert response.status_code == 200
        assert canonical_json_hash(response.json()) == digest

    def test_missing_file_and_round_return_404(self, db, client):
        self._frozen_round(db)

        assert client.get("/api/governance/rounds/1/package/no/such.json").status_code == 404
        assert client.get("/api/governance/rounds/2/package").status_code == 404
