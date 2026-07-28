"""The exam runtime manager: reuse, redeploy, warm-up, cleanup, and failures."""

import json
import subprocess

import httpx
import pytest

from governance_service.models import RuntimeProfile
from governance_service.services import candidate_profiles, runtime_manager
from governance_service.services.runtime_manager import (
    APP_NAME_PREFIX,
    CandidateDeployError,
    EnsureResult,
    ExamRuntimeManager,
    InfrastructureError,
    ProfileError,
    candidate_app_name,
    validate_deployable,
)

INCUMBENT = candidate_profiles.CURRENT_POOL_PROFILES["Qwen/Qwen3.6-27B-FP8"]
CHALLENGER = candidate_profiles.CURRENT_POOL_PROFILES["google/gemma-4-31B-it"]


class FakeModal:
    """An injectable Modal boundary recording every interaction."""

    def __init__(self):
        self.deployed: dict[str, dict] = {}
        self.stopped: list[str] = []
        self.deleted_volumes: list[str] = []
        self.deploy_calls = 0
        self.fail_stderr: str | None = None
        self.endpoint_healthy = True

    def run_command(self, command, *, env=None, capture_output=True, text=True, timeout=0):
        if command[:2] == ["modal", "deploy"]:
            self.deploy_calls += 1
            if self.fail_stderr is not None:
                return subprocess.CompletedProcess(command, 1, "", self.fail_stderr)
            app_name = env["GOVEXAM_APP_NAME"]
            self.deployed[app_name] = {k: v for k, v in env.items() if k.startswith("GOVEXAM_")}
            return subprocess.CompletedProcess(command, 0, "deployed", "")
        if command[:3] == ["modal", "app", "stop"]:
            self.stopped.append(command[3])
            self.deployed.pop(command[3], None)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["modal", "volume", "delete"]:
            self.deleted_volumes.append(command[3])
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["modal", "app", "list"]:
            # Shape captured from modal 1.5: snake_case keys, name under `description`.
            listed = [
                {"app_id": f"ap-{i}", "description": name, "state": "deployed", "tasks": 0}
                for i, name in enumerate(self.deployed)
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(listed), "")
        raise AssertionError(f"unexpected command {command}")

    def url_resolver(self, app_name):
        if app_name not in self.deployed:
            return None
        return (
            f"https://test--{app_name}-serve.modal.run",
            f"https://test--{app_name}-profile.modal.run",
        )

    def http_get(self, url):
        if "-profile" in url:
            for app_name, config in self.deployed.items():
                if app_name in url:
                    return httpx.Response(
                        200,
                        json={
                            "profile_hash": config["GOVEXAM_PROFILE_HASH"],
                            "revision": config.get("GOVEXAM_MODEL_REVISION"),
                        },
                    )
            return httpx.Response(404)
        if url.endswith("/health"):
            return httpx.Response(200 if self.endpoint_healthy else 503)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _proxy_credentials(monkeypatch):
    monkeypatch.setattr(runtime_manager.settings, "modal_key", "test-key")
    monkeypatch.setattr(runtime_manager.settings, "modal_secret", "test-secret")


@pytest.fixture
def fake():
    return FakeModal()


@pytest.fixture
def manager(fake):
    return ExamRuntimeManager(
        run_command=fake.run_command,
        url_resolver=fake.url_resolver,
        http_get=fake.http_get,
        sleep=lambda seconds: None,
        monotonic=_ticker(),
    )


def _ticker(step: float = 100.0):
    state = {"now": 0.0}

    def monotonic():
        state["now"] += step
        return state["now"]

    return monotonic


def test_app_names_derive_from_candidate_identity():
    assert candidate_app_name("Qwen/Qwen3.6-27B-FP8") == (
        "governance-exam-qwen--qwen3.6-27b-fp8"
    )
    assert candidate_app_name("google/gemma-4-31B-it") == (
        "governance-exam-google--gemma-4-31b-it"
    )


def test_validate_deployable_rejects_incomplete_profiles():
    minimal = RuntimeProfile(hf_repo="org/model", extra_body={})
    with pytest.raises(ProfileError, match="revision"):
        validate_deployable(minimal)

    undigested = INCUMBENT.model_copy(update={"image": "lmsysorg/sglang:latest"})
    with pytest.raises(ProfileError, match="digest-pinned"):
        validate_deployable(undigested)

    lax = INCUMBENT.model_copy(
        update={"launch_args": ["--mem-fraction-static", "0.75"]}
    )
    with pytest.raises(ProfileError, match="enable-deterministic-inference"):
        validate_deployable(lax)


def test_current_pool_profiles_are_deployable():
    for profile in candidate_profiles.CURRENT_POOL_PROFILES.values():
        validate_deployable(profile)
        assert profile.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "--reasoning-parser" not in CHALLENGER.launch_args
    assert "--reasoning-parser" in INCUMBENT.launch_args


def test_first_deploy_then_reuse(manager, fake):
    first = manager.ensure_deployed(INCUMBENT)
    assert first.reused is False
    assert fake.deploy_calls == 1
    deployed = fake.deployed[first.app_name]
    assert deployed["GOVEXAM_MODEL_REVISION"] == INCUMBENT.revision
    assert deployed["GOVEXAM_GPU"] == "H100"
    assert json.loads(deployed["GOVEXAM_LAUNCH_ARGS"]) == INCUMBENT.launch_args

    second = manager.ensure_deployed(INCUMBENT)
    assert second.reused is True
    assert fake.deploy_calls == 1
    assert second.endpoint_url == first.endpoint_url


def test_profile_drift_triggers_redeploy(manager, fake):
    manager.ensure_deployed(INCUMBENT)
    drifted = INCUMBENT.model_copy(
        update={"revision": "0" * 40}
    )
    result = manager.ensure_deployed(drifted)
    assert result.reused is False
    assert fake.deploy_calls == 2
    assert fake.deployed[result.app_name]["GOVEXAM_PROFILE_HASH"] == drifted.content_hash()


def test_every_deploy_stage_failure_is_infrastructure(manager, fake):
    """Deploy exercises nothing candidate-specific; ambiguity must not DQ."""
    for stderr in (
        "Error: token missing, not logged in to Modal",
        "Image build failed: layer exploded during model setup",
        "",
    ):
        fake.fail_stderr = stderr
        with pytest.raises(InfrastructureError):
            manager.ensure_deployed(INCUMBENT)


def test_serve_failure_evidence_carries_the_candidate_identity(manager, fake):
    result = manager.ensure_deployed(CHALLENGER)
    fake.endpoint_healthy = False
    with pytest.raises(CandidateDeployError) as excinfo:
        manager.verify_warmup(CHALLENGER, result, timeout_seconds=150)
    failure = excinfo.value.failure
    assert failure.hf_repo == CHALLENGER.hf_repo
    assert failure.revision == CHALLENGER.revision
    assert failure.stage == "serve"
    assert failure.as_dict()["profile_hash"] == CHALLENGER.content_hash()


def test_warmup_ready(manager, fake):
    result = manager.ensure_deployed(INCUMBENT)
    manager.verify_warmup(INCUMBENT, result, timeout_seconds=500)


def test_warmup_timeout_with_live_control_is_candidate_evidence(manager, fake):
    result = manager.ensure_deployed(CHALLENGER)
    fake.endpoint_healthy = False
    with pytest.raises(CandidateDeployError) as excinfo:
        manager.verify_warmup(CHALLENGER, result, timeout_seconds=150)
    assert excinfo.value.failure.stage == "serve"


def test_warmup_timeout_with_dead_control_is_infrastructure(manager, fake):
    result = manager.ensure_deployed(CHALLENGER)
    fake.endpoint_healthy = False
    fake.deployed.clear()
    with pytest.raises(InfrastructureError):
        manager.verify_warmup(CHALLENGER, result, timeout_seconds=150)


def test_teardown_stops_the_candidate_app(manager, fake):
    result = manager.ensure_deployed(INCUMBENT)
    stopped = manager.teardown(INCUMBENT.hf_repo)
    assert stopped == result.app_name
    assert fake.stopped == [result.app_name]
    assert fake.deleted_volumes == [f"{result.app_name}-model-weights"]


def test_cleanup_stops_only_departed_candidates(manager, fake):
    kept = manager.ensure_deployed(INCUMBENT)
    departed = manager.ensure_deployed(CHALLENGER)
    stopped = manager.cleanup_departed([INCUMBENT.hf_repo])
    assert stopped == [departed.app_name]
    assert kept.app_name not in fake.stopped


def test_cleanup_ignores_foreign_apps(manager, fake):
    fake.deployed["dynamic-unl-scoring-qwen36"] = {"GOVEXAM_PROFILE_HASH": "x", "GOVEXAM_MODEL_REVISION": "y"}
    stopped = manager.cleanup_departed([])
    assert stopped == []


def test_ensure_deployed_requires_resolvable_endpoints(fake):
    manager = ExamRuntimeManager(
        run_command=fake.run_command,
        url_resolver=lambda app_name: None,
        http_get=fake.http_get,
        sleep=lambda seconds: None,
        monotonic=_ticker(),
    )
    with pytest.raises(InfrastructureError, match="cannot be resolved"):
        manager.ensure_deployed(INCUMBENT)


def test_profile_content_hash_is_stable():
    assert INCUMBENT.content_hash() == INCUMBENT.content_hash()
    assert INCUMBENT.content_hash() != CHALLENGER.content_hash()
