"""LiveBench client behavior against snapshot fixtures of the live site."""

import hashlib
import json
from pathlib import Path

import pytest

from governance_service.clients import livebench
from governance_service.clients.livebench import LiveBenchSchemaError

FIXTURES = Path(__file__).parent / "fixtures"
RELEASE = "2026_06_25"

EXPECTED_OPENWEIGHT_KEYS = {
    "glm-5.2",
    "deepseek-v4-pro",
    "kimi-k2.6-thinking",
    "kimi-k2.7-code",
    "deepseek-v4-flash",
    "qwen3.6-27b",
}


@pytest.fixture(scope="module")
def registry():
    return livebench.parse_registry((FIXTURES / "modelLinks.js").read_bytes())


@pytest.fixture(scope="module")
def categories():
    return livebench.parse_categories(
        (FIXTURES / f"categories_{RELEASE}.json").read_bytes()
    )


@pytest.fixture(scope="module")
def table_raw():
    return (FIXTURES / f"table_{RELEASE}.csv").read_bytes()


def test_registry_parses_full_model_history(registry):
    assert len(registry) >= 100
    qwen = registry["qwen3.6-27b"]
    assert qwen.openweight is True
    assert qwen.organization == "Alibaba"


def test_standings_reproduce_published_leaderboard(table_raw, categories, registry):
    standings = livebench.compute_standings(table_raw, categories, registry)
    by_key = {standing.model_key: standing for standing in standings}

    assert round(by_key["qwen3.6-27b"].global_average, 2) == 64.03

    open_standings = [s for s in standings if s.openweight]
    assert {s.model_key for s in open_standings} == EXPECTED_OPENWEIGHT_KEYS
    assert open_standings[0].model_key == "glm-5.2"
    assert round(open_standings[0].global_average, 2) == 73.18


def test_model_missing_entire_category_is_excluded(registry):
    categories = {"A": ["t1"], "B": ["t2"]}
    table = b"model,t1,t2\nqwen3.6-27b,50.0,\n"
    standings = livebench.compute_standings(table, categories, registry)
    assert standings == []


def test_model_absent_from_registry_is_skipped(registry):
    categories = {"A": ["t1"]}
    table = b"model,t1\nnot-a-registered-model,50.0\n"
    standings = livebench.compute_standings(table, categories, registry)
    assert standings == []


def test_unknown_task_column_raises(table_raw, registry):
    with pytest.raises(LiveBenchSchemaError, match="missing from score table"):
        livebench.compute_standings(table_raw, {"A": ["no_such_task"]}, registry)


def test_non_numeric_score_raises(registry):
    categories = {"A": ["t1"]}
    table = b"model,t1\nqwen3.6-27b,abc\n"
    with pytest.raises(LiveBenchSchemaError, match="Non-numeric score"):
        livebench.compute_standings(table, categories, registry)


def test_registry_shrunk_below_minimum_raises():
    with pytest.raises(LiveBenchSchemaError, match="format likely changed"):
        livebench.parse_registry(b'"one-model": {url: "https://x", openweight: true}')


def test_categories_must_be_nonempty_object():
    with pytest.raises(LiveBenchSchemaError):
        livebench.parse_categories(b"[]")
    with pytest.raises(LiveBenchSchemaError):
        livebench.parse_categories(b'{"Reasoning": []}')


def test_release_discovery_orders_table_releases():
    listing = (FIXTURES / "github_contents_public.json").read_bytes()

    class FakeClient:
        def get(self, url, headers=None):
            return FakeResponse(listing)

    class FakeResponse:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    releases = livebench.discover_releases(FakeClient())
    assert releases == ["2025_11_25", "2026_01_08", "2026_06_25"]


def test_snapshot_matches_content_hash(table_raw):
    snap = livebench.snapshot(f"table_{RELEASE}.csv", table_raw)
    assert snap.sha256 == hashlib.sha256(table_raw).hexdigest()
    assert snap.size_bytes == len(table_raw)


def test_fixture_categories_match_current_schema(categories):
    raw = json.loads((FIXTURES / f"categories_{RELEASE}.json").read_text())
    assert categories == raw
    assert len(categories) == 7
