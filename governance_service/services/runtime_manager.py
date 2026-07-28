"""Candidate runtime management: any pool candidate, deployed like production.

The manager adapts the wheels the organization already trusts — the
production endpoint's serving profile and the validator sidecar's
templated-app deploy pattern — into an idempotent per-candidate contract:

- Apps are named by candidate identity, never by round, so a candidate
  recurring across governance rounds keeps one app.
- ``ensure_deployed`` asks the live app what it serves (the CPU ``profile``
  control endpoint reports the deployed profile's content hash) and reuses
  it on a match; absence or drift triggers a redeploy that replaces the
  app in place.
- ``verify_warmup`` proves the inference endpoint actually serves before
  anything trusts it, with the sidecar's health-probe discipline.
- ``teardown``/``cleanup_departed`` stop apps whose candidates left the
  pool, so exam apps never accumulate.

Failure handling is two-sided by design: infrastructure failures (auth,
quota, billing, platform outages) raise ``InfrastructureError`` — abort
and retry, never round state — while a candidate's own failure to serve
on its pinned profile raises ``CandidateDeployError`` carrying the
structured evidence mechanical disqualification requires. The deploy
command exercises nothing candidate-specific (every candidate shares the
same digest-pinned image, and weights are untouched until serving), so
every deploy-stage failure is infrastructure; candidate evidence comes
from the serve stage. Ambiguity fails toward infrastructure: a retry
costs minutes, an unfair disqualification costs a candidate.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from governance_service.config import settings
from governance_service.models.runtime_profile import (
    DETERMINISTIC_INFERENCE_FLAG,
    RuntimeProfile,
)

APP_NAME_PREFIX = "governance-exam"
EXAM_APP_MODULE = Path(__file__).resolve().parent.parent / "_exam_modal_app.py"
ENDPOINT_CLASS_NAME = "ExamCandidateEndpoint"
ENDPOINT_WEB_METHOD = "serve"
PROFILE_FUNCTION_NAME = "profile"

DEPLOY_TIMEOUT_SECONDS = 30 * 60
CLI_TIMEOUT_SECONDS = 300
# Above the app's own 35-minute startup allowance so a legitimately slow
# cold start is never booked as candidate evidence while still in budget.
WARMUP_TIMEOUT_SECONDS = 2400.0
WARMUP_PROBE_INTERVAL_SECONDS = 15.0

STAGE_SERVE = "serve"

_APP_NAME_SANITIZER = re.compile(r"[^a-z0-9.-]+")

# The modal CLI styles --json output with ANSI codes even when piped.
_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


class InfrastructureError(RuntimeError):
    """A platform-side failure: retryable, never evidence against a candidate."""


class ProfileError(ValueError):
    """The profile is not deployable — an operator error, not a failure of anyone."""


@dataclass
class CandidateDeployFailure:
    """Structured evidence of a candidate failing its pinned profile."""

    hf_repo: str
    revision: str
    profile_hash: str
    stage: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateDeployError(RuntimeError):
    """Raised when the candidate itself cannot deploy and serve."""

    def __init__(self, failure: CandidateDeployFailure):
        super().__init__(
            f"Candidate {failure.hf_repo}@{failure.revision[:12]} failed at "
            f"{failure.stage}: {failure.detail}"
        )
        self.failure = failure


@dataclass
class EnsureResult:
    """Outcome of one ensure-deployed call."""

    app_name: str
    profile_hash: str
    reused: bool
    endpoint_url: str | None = None
    profile_url: str | None = None



def candidate_app_name(hf_repo: str) -> str:
    """Stable per-candidate Modal app name: identity, never round."""
    slug = _APP_NAME_SANITIZER.sub("-", hf_repo.lower().replace("/", "--")).strip("-")
    return f"{APP_NAME_PREFIX}-{slug}"


def validate_deployable(profile: RuntimeProfile) -> None:
    """The deploy-time gate: every pin present and disciplined."""
    missing = profile.missing_deployment_fields()
    if missing:
        raise ProfileError(f"Profile for {profile.hf_repo} has no {', '.join(missing)}")
    if "@sha256:" not in (profile.image or ""):
        raise ProfileError(f"Profile image for {profile.hf_repo} is not digest-pinned")
    if DETERMINISTIC_INFERENCE_FLAG not in (profile.launch_args or []):
        raise ProfileError(
            f"Profile launch args for {profile.hf_repo} lack {DETERMINISTIC_INFERENCE_FLAG}"
        )


def proxy_auth_headers() -> dict[str, str] | None:
    """Modal proxy-auth headers, or None when credentials are not configured."""
    if settings.modal_key and settings.modal_secret:
        return {"Modal-Key": settings.modal_key, "Modal-Secret": settings.modal_secret}
    return None


class ExamRuntimeManager:
    """Idempotent per-candidate deployment on the templated exam app.

    The Modal boundary is injectable for tests: ``run_command`` executes
    the ``modal`` CLI, ``url_resolver`` maps an app name to its live
    ``(endpoint_url, profile_url)`` or ``None`` when the app is absent,
    and ``http_get`` performs the probe requests.
    """

    def __init__(
        self,
        *,
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        url_resolver: Callable[[str], tuple[str, str] | None] | None = None,
        http_get: Callable[[str], httpx.Response] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self._run_command = run_command
        self._url_resolver = url_resolver or _resolve_app_urls
        self._http_get = http_get or _http_get
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

    # -- ensure-deployed ---------------------------------------------------

    def ensure_deployed(self, profile: RuntimeProfile) -> EnsureResult:
        """The candidate's app, live on exactly this profile — reused or deployed."""
        validate_deployable(profile)
        if proxy_auth_headers() is None:
            raise InfrastructureError(
                "MODAL_KEY / MODAL_SECRET are not configured — the exam apps "
                "require proxy auth, so probes and warm-up cannot succeed"
            )
        app_name = candidate_app_name(profile.hf_repo)
        expected_hash = profile.content_hash()

        live = self._live_profile(app_name)
        if (
            live is not None
            and live[0].get("profile_hash") == expected_hash
            and live[0].get("revision") == profile.revision
        ):
            endpoint_url, profile_url = live[1]
            return EnsureResult(
                app_name=app_name,
                profile_hash=expected_hash,
                reused=True,
                endpoint_url=endpoint_url,
                profile_url=profile_url,
            )

        self._deploy(profile, app_name, expected_hash)
        urls = self._url_resolver(app_name)
        if urls is None:
            raise InfrastructureError(
                f"App {app_name} deployed but its endpoints cannot be resolved"
            )
        return EnsureResult(
            app_name=app_name,
            profile_hash=expected_hash,
            reused=False,
            endpoint_url=urls[0],
            profile_url=urls[1],
        )

    def _live_profile(self, app_name: str) -> tuple[dict, tuple[str, str]] | None:
        """(reported profile, urls) of the live app, or None when absent/unreadable."""
        urls = self._url_resolver(app_name)
        if urls is None:
            return None
        try:
            response = self._http_get(urls[1])
            if response.status_code != 200:
                return None
            reported = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(reported, dict):
            return None
        return reported, urls

    def _deploy(self, profile: RuntimeProfile, app_name: str, profile_hash: str) -> None:
        environment = {
            **os.environ,
            "MODAL_ENVIRONMENT": settings.exam_modal_environment,
            "GOVEXAM_APP_NAME": app_name,
            "GOVEXAM_IMAGE": profile.image,
            "GOVEXAM_GPU": profile.gpu,
            "GOVEXAM_MODEL_REPO_ID": profile.hf_repo,
            "GOVEXAM_MODEL_REVISION": profile.revision,
            "GOVEXAM_TENSOR_PARALLELISM": str(profile.tensor_parallelism),
            "GOVEXAM_LAUNCH_ARGS": json.dumps(profile.launch_args),
            "GOVEXAM_ENVIRONMENT": json.dumps(profile.environment),
            "GOVEXAM_PROFILE_HASH": profile_hash,
            "GOVEXAM_SCALEDOWN_MINUTES": str(settings.exam_modal_scaledown_minutes),
        }
        if settings.hf_token:
            environment["HF_TOKEN"] = settings.hf_token
        try:
            completed = self._run_command(
                ["modal", "deploy", str(EXAM_APP_MODULE)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=DEPLOY_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError("modal CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(f"modal deploy timed out for {app_name}") from exc
        if completed.returncode != 0:
            detail = ((completed.stderr or "") or (completed.stdout or "")).strip()[-2000:]
            raise InfrastructureError(f"modal deploy failed: {detail or 'no output'}")

    # -- warm-up -----------------------------------------------------------

    def verify_warmup(
        self,
        profile: RuntimeProfile,
        result: EnsureResult,
        *,
        timeout_seconds: float = WARMUP_TIMEOUT_SECONDS,
        probe_interval_seconds: float = WARMUP_PROBE_INTERVAL_SECONDS,
    ) -> None:
        """Prove the endpoint serves, or classify why it does not.

        The GPU health probe is the readiness signal. When it never turns
        ready, the CPU control endpoint discriminates the failure side:
        control reachable means the platform works and the candidate's
        server did not come up (candidate evidence); control unreachable
        means the platform itself is failing (infrastructure).
        """
        if not result.endpoint_url:
            raise InfrastructureError(f"App {result.app_name} has no endpoint URL")
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            if self._probe_ready(result.endpoint_url):
                return
            self._sleep(probe_interval_seconds)

        control_ok = result.profile_url is not None and self._probe_ready(
            result.profile_url, direct=True
        )
        if control_ok:
            raise CandidateDeployError(
                CandidateDeployFailure(
                    hf_repo=profile.hf_repo,
                    revision=profile.revision or "",
                    profile_hash=result.profile_hash,
                    stage=STAGE_SERVE,
                    detail=(
                        f"Endpoint did not become healthy within {timeout_seconds:.0f}s "
                        "while the app's control endpoint stayed reachable"
                    ),
                )
            )
        raise InfrastructureError(
            f"App {result.app_name} unreachable end to end within {timeout_seconds:.0f}s"
        )

    def _probe_ready(self, url: str, *, direct: bool = False) -> bool:
        """Probe the URL as-is (direct) or its derived /health route."""
        base = url.rstrip("/")
        target = base if direct else f"{base.removesuffix('/v1')}/health"
        try:
            response = self._http_get(target)
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    # -- teardown and cleanup ---------------------------------------------

    def teardown(self, hf_repo: str) -> str:
        """Stop the candidate's app and delete its weight volume."""
        app_name = candidate_app_name(hf_repo)
        self._stop_app(app_name)
        self._delete_volume(app_name)
        return app_name

    def cleanup_departed(self, pool_hf_repos: list[str]) -> list[str]:
        """Stop every exam app whose candidate is no longer in the pool."""
        keep = {candidate_app_name(repo) for repo in pool_hf_repos}
        stopped = []
        for app_name in self._list_exam_apps():
            if app_name not in keep:
                self._stop_app(app_name)
                self._delete_volume(app_name)
                stopped.append(app_name)
        return stopped

    def _stop_app(self, app_name: str) -> None:
        try:
            completed = self._run_command(
                ["modal", "app", "stop", app_name, "--yes"],
                env=self._modal_environ(),
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError("modal CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(f"modal app stop timed out for {app_name}") from exc
        if completed.returncode != 0:
            raise InfrastructureError(
                f"modal app stop failed for {app_name}: {(completed.stderr or '').strip()}"
            )

    def _delete_volume(self, app_name: str) -> None:
        """Delete the candidate's weight volume; a missing volume is fine."""
        volume_name = f"{app_name}-model-weights"
        try:
            completed = self._run_command(
                ["modal", "volume", "delete", volume_name, "--yes"],
                env=self._modal_environ(),
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError("modal CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(f"modal volume delete timed out for {volume_name}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").lower()
            if "not found" in stderr or "does not exist" in stderr:
                return
            raise InfrastructureError(
                f"modal volume delete failed for {volume_name}: {(completed.stderr or '').strip()}"
            )

    def _list_exam_apps(self) -> list[str]:
        try:
            completed = self._run_command(
                ["modal", "app", "list", "--json"],
                env=self._modal_environ(),
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise InfrastructureError("modal CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError("modal app list timed out") from exc
        if completed.returncode != 0:
            raise InfrastructureError(
                f"modal app list failed: {(completed.stderr or '').strip()}"
            )
        try:
            apps = json.loads(_ANSI_ESCAPES.sub("", completed.stdout or "[]"))
        except ValueError as exc:
            raise InfrastructureError("modal app list returned unparseable output") from exc
        # modal 1.5 emits snake_case column keys; the app name is `description`.
        return [
            entry["description"]
            for entry in apps
            if isinstance(entry, dict)
            and str(entry.get("description", "")).startswith(f"{APP_NAME_PREFIX}-")
            and str(entry.get("state", "")).lower() == "deployed"
        ]

    @staticmethod
    def _modal_environ() -> dict[str, str]:
        return {**os.environ, "MODAL_ENVIRONMENT": settings.exam_modal_environment}


def _resolve_app_urls(app_name: str) -> tuple[str, str] | None:
    """Live (endpoint_url, profile_url) via the Modal API; None when absent."""
    try:
        import modal

        environment = settings.exam_modal_environment
        endpoint = modal.Cls.from_name(
            app_name, ENDPOINT_CLASS_NAME, environment_name=environment
        )()
        endpoint_url = getattr(endpoint, ENDPOINT_WEB_METHOD).get_web_url()
        profile_url = modal.Function.from_name(
            app_name, PROFILE_FUNCTION_NAME, environment_name=environment
        ).get_web_url()
    except Exception:
        return None
    if not endpoint_url or not profile_url:
        return None
    return endpoint_url, profile_url


def _http_get(url: str) -> httpx.Response:
    headers = proxy_auth_headers()
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        return client.get(url, headers=headers)
