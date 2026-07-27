"""Corpus assembly: history selection, package verification, and the manifest."""

import json

import httpx
import pytest

from governance_service.clients import scoring_api
from governance_service.config import settings
from governance_service.scoring import canonical_json_hash, canonical_sha256
from governance_service.services import corpus, edge_cases


def _make_package(round_number: int, network: str = "devnet") -> tuple[dict, dict, str]:
    """A synthetic input package: (files, bundle, input_package_hash).

    Includes an array-valued file: real packages carry raw evidence arrays
    (e.g. crawl_probes.json), which is why the file-hash rule accepts any
    JSON value and not just objects.
    """
    files = {
        "inputs/model_request.json": {"model": "test", "round": round_number},
        "inputs/validator_evidence.json": {"validators": [round_number]},
        "raw/crawl_probes.json": [],
    }
    bundle = {
        "bundle_version": 1,
        "package_kind": "input",
        "round_kind": "normal",
        "network": network,
        "round_number": round_number,
        "input_frozen_at": f"2026-07-0{round_number % 9 + 1}T00:00:00+00:00",
        "file_hashes": {path: canonical_json_hash(content) for path, content in files.items()},
    }
    return files, bundle, canonical_json_hash(bundle)


def _round_row(round_number: int, package_hash: str, status: str = "COMPLETE") -> dict:
    return {
        "round_number": round_number,
        "status": status,
        "input_package_cid": f"QmTest{round_number}",
        "input_package_hash": package_hash,
    }


def _client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _https_handler(rounds: list[dict], packages: dict[int, tuple[dict, dict, str]]):
    """Serve the scoring service routes the way dynamic-unl-scoring's
    scoring.py and audit_trail.py do."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/scoring/rounds":
            return httpx.Response(200, json={"rounds": rounds, "total": len(rounds)})
        parts = path.split("/")
        if len(parts) >= 7 and parts[5] == "input":
            round_number = int(parts[4])
            file_path = "/".join(parts[6:])
            files, bundle, _ = packages[round_number]
            if file_path == "bundle.json":
                return httpx.Response(200, json=bundle)
            if file_path in files:
                return httpx.Response(200, json=files[file_path])
            return httpx.Response(404, json={"error": f"not found: {file_path}"})
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return handler


def test_select_history_rounds_filters_and_takes_newest():
    rounds = [
        _round_row(5, "a" * 64),
        _round_row(9, "b" * 64),
        _round_row(8, "c" * 64, status="FAILED"),
        {"round_number": 7, "status": "COMPLETE", "input_package_cid": None,
         "input_package_hash": None},
        _round_row(6, "d" * 64),
    ]
    selected = corpus.select_history_rounds(rounds, window=2)
    assert [r["round_number"] for r in selected] == [9, 6]


def test_select_history_rounds_takes_fewer_when_history_is_short():
    rounds = [_round_row(1, "a" * 64)]
    assert len(corpus.select_history_rounds(rounds, window=12)) == 1
    assert corpus.select_history_rounds([], window=12) == []


def test_verify_input_package_accepts_a_valid_package():
    files, bundle, package_hash = _make_package(3)
    with _client_for(_https_handler([], {3: (files, bundle, package_hash)})) as client:
        item = corpus.verify_input_package(client, _round_row(3, package_hash))
    assert item.round_number == 3
    assert item.verified_file_count == len(files)
    assert item.input_package_hash == package_hash


def test_verify_input_package_rejects_bundle_hash_mismatch():
    files, bundle, package_hash = _make_package(3)
    with _client_for(_https_handler([], {3: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="bundle hash mismatch"):
            corpus.verify_input_package(client, _round_row(3, "f" * 64))


def test_verify_input_package_rejects_a_tampered_file():
    files, bundle, package_hash = _make_package(4)
    files["inputs/model_request.json"] = {"model": "tampered", "round": 4}
    with _client_for(_https_handler([], {4: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="hash mismatch"):
            corpus.verify_input_package(client, _round_row(4, package_hash))


def test_verify_input_package_rejects_traversal_paths():
    files, bundle, package_hash = _make_package(7)
    bundle["file_hashes"]["../outside.json"] = "a" * 64
    package_hash = canonical_json_hash(bundle)
    with _client_for(_https_handler([], {7: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="Invalid package file path"):
            corpus.verify_input_package(client, _round_row(7, package_hash))


def test_verify_input_package_rejects_bundle_listing_itself():
    files, bundle, package_hash = _make_package(8)
    bundle["file_hashes"]["bundle.json"] = "a" * 64
    package_hash = canonical_json_hash(bundle)
    with _client_for(_https_handler([], {8: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="bundle.json itself"):
            corpus.verify_input_package(client, _round_row(8, package_hash))


def test_verify_input_package_rejects_wrong_package_kind():
    files, bundle, package_hash = _make_package(9)
    bundle["package_kind"] = "final"
    package_hash = canonical_json_hash(bundle)
    with _client_for(_https_handler([], {9: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="expected input"):
            corpus.verify_input_package(client, _round_row(9, package_hash))


def test_verify_input_package_rejects_wrong_network(monkeypatch):
    monkeypatch.setattr(settings, "environment", "devnet")
    files, bundle, package_hash = _make_package(5, network="testnet")
    with _client_for(_https_handler([], {5: (files, bundle, package_hash)})) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="network"):
            corpus.verify_input_package(client, _round_row(5, package_hash))


def test_verify_input_package_falls_back_to_the_gateway(monkeypatch):
    monkeypatch.setattr(settings, "http_retry_base_delay", 0)
    files, bundle, package_hash = _make_package(6)
    cid = f"QmTest{6}"
    gateway_hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/scoring/"):
            return httpx.Response(500, text="service down")
        assert path.startswith(f"/ipfs/{cid}/")
        file_path = path[len(f"/ipfs/{cid}/") :]
        gateway_hits.append(file_path)
        if file_path == "bundle.json":
            return httpx.Response(200, json=bundle)
        return httpx.Response(200, json=files[file_path])

    with _client_for(handler) as client:
        item = corpus.verify_input_package(client, _round_row(6, package_hash))
    assert item.verified_file_count == len(files)
    assert "bundle.json" in gateway_hits


def test_build_corpus_produces_the_manifest(monkeypatch):
    monkeypatch.setattr(settings, "corpus_history_window", 2)
    files_a, bundle_a, hash_a = _make_package(2)
    rounds = [_round_row(2, hash_a)]
    packages = {2: (files_a, bundle_a, hash_a)}

    with _client_for(_https_handler(rounds, packages)) as client:
        result = corpus.build_corpus(client)

    manifest = result.manifest
    assert manifest["manifest_version"] == corpus.MANIFEST_VERSION
    assert manifest["policy"] == {
        "history_window_requested": 2,
        "history_rounds_found": 1,
        "catalogue_version": edge_cases.CATALOGUE_VERSION,
    }
    assert manifest["constructed_template"] == {
        "source_round": edge_cases.TEMPLATE_SOURCE_ROUND,
        "source_cid": edge_cases.TEMPLATE_SOURCE_CID,
    }
    assert [h["round_number"] for h in manifest["historical"]] == [2]
    assert manifest["historical"][0]["input_package_cid"] == "QmTest2"

    constructed = {entry["case_id"]: entry for entry in manifest["constructed"]}
    assert set(constructed) == set(edge_cases.CASE_BUILDERS)
    for case_id, request in result.constructed.items():
        assert constructed[case_id]["content_hash"] == canonical_sha256(request)

    # The manifest itself must be canonically hashable and JSON-serializable.
    assert canonical_sha256(manifest)
    json.dumps(manifest)


def test_fetch_rounds_paginates_past_ineligible_rounds(monkeypatch):
    monkeypatch.setattr(corpus, "ROUNDS_PAGE_LIMIT", 3)
    incomplete = [
        _round_row(number, "e" * 64, status="FAILED") for number in range(10, 7, -1)
    ]
    files, bundle, package_hash = _make_package(2)
    eligible = [_round_row(2, package_hash)]
    pages = {0: incomplete, 3: eligible}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/scoring/rounds":
            offset = int(request.url.params["offset"])
            return httpx.Response(
                200, json={"rounds": pages.get(offset, []), "total": 4}
            )
        return _https_handler([], {2: (files, bundle, package_hash)})(request)

    monkeypatch.setattr(settings, "corpus_history_window", 1)
    with _client_for(handler) as client:
        result = corpus.build_corpus(client)
    assert [h["round_number"] for h in result.manifest["historical"]] == [2]


def test_scoring_api_not_found_is_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, json={"error": "missing"})

    with _client_for(handler) as client:
        with pytest.raises(scoring_api.ScoringServiceNotFoundError):
            scoring_api.fetch_input_file(client, 1, "bundle.json")
    assert len(calls) == 1
