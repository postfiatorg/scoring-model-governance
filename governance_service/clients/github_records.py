"""GitHub client for publishing refresh records to the governance repository.

Commits each completed refresh's record files to the public governance
repository through the GitHub Contents API — no git binary, no SSH key,
no working tree — mirroring the dynamic-unl-scoring VL distribution
client. Each publish is a GET (current file SHA, for optimistic
concurrency) followed by a PUT (create or replace the file).

Edge cases handled:
    - 404 on the GET — file does not yet exist; PUT without a `sha`
      parameter and GitHub creates it.
    - 409 on the PUT — SHA mismatch from an interleaving commit; retry
      the full GET/PUT cycle with exponential backoff.
    - 5xx / network errors on either call — retry with exponential
      backoff up to `http_max_retries`.
    - 4xx other than 409/404 — fail-fast (auth failure, invalid path,
      unknown repo).
"""

import base64
import logging
import time

import httpx
from fastapi import status

from governance_service.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"


class GitHubRecordsError(RuntimeError):
    """Raised when a record publish fails after exhausting retries."""


class GitHubRecordsClient:
    """Commits record files to the governance repository via the Contents API."""

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        commit_author_name: str | None = None,
        commit_author_email: str | None = None,
    ):
        self._token = token or settings.records_github_token
        self._repo = repo or settings.records_github_repo
        self._branch = branch or settings.records_github_branch
        self._author_name = commit_author_name or settings.records_commit_author_name
        self._author_email = commit_author_email or settings.records_commit_author_email

        if not self._token:
            raise ValueError("RECORDS_GITHUB_TOKEN is required for record publication")
        if not self._repo:
            raise ValueError("RECORDS_GITHUB_REPO is required for record publication")

        logger.info(
            "GitHub records client initialized: repo=%s, branch=%s",
            self._repo,
            self._branch,
        )

    def _contents_url(self, file_path: str) -> str:
        return f"{GITHUB_API_BASE}/repos/{self._repo}/contents/{file_path}"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def publish(self, file_path: str, content: str, commit_message: str) -> str:
        """Commit `content` to `file_path` and return the commit HTML URL.

        Raises:
            GitHubRecordsError: When the Contents API rejects the publish
                after exhausting retries, or on fail-fast 4xx responses
                indicating a configuration problem.
        """
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        for attempt in range(1, settings.http_max_retries + 1):
            try:
                sha = self._fetch_sha(file_path)
                commit_url = self._put_content(
                    file_path, content_b64, commit_message, sha
                )
                logger.info(
                    "Record publish succeeded: repo=%s path=%s commit=%s",
                    self._repo,
                    file_path,
                    commit_url,
                )
                return commit_url

            except _ConflictError as exc:
                if attempt == settings.http_max_retries:
                    raise GitHubRecordsError(
                        f"Record publish failed after {settings.http_max_retries} "
                        f"attempts due to persistent SHA conflicts: {exc}"
                    ) from exc
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "Record PUT attempt %d/%d returned 409 (SHA conflict); "
                    "refetching SHA and retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    delay,
                )
                time.sleep(delay)

            except _TransientError as exc:
                if attempt == settings.http_max_retries:
                    raise GitHubRecordsError(
                        f"Record publish failed after {settings.http_max_retries} "
                        f"attempts due to transient errors: {exc}"
                    ) from exc
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "Record publish attempt %d/%d hit a transient error (%s); "
                    "retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)

        raise GitHubRecordsError("Record publish exhausted retries without a definitive outcome")

    def _fetch_sha(self, file_path: str) -> str | None:
        """Return the current file SHA, or None if the file does not exist."""
        params = {"ref": self._branch}

        try:
            with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                response = client.get(
                    self._contents_url(file_path), headers=self._headers, params=params
                )
        except httpx.HTTPError as exc:
            raise _TransientError(f"SHA fetch network error: {exc}") from exc

        if response.status_code == status.HTTP_200_OK:
            return response.json().get("sha")
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return None
        if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise _TransientError(f"SHA fetch 5xx: {response.status_code} {response.text}")
        raise GitHubRecordsError(
            f"Record SHA fetch failed fast with HTTP {response.status_code}: {response.text}"
        )

    def _put_content(
        self, file_path: str, content_b64: str, commit_message: str, sha: str | None
    ) -> str:
        """Commit the encoded file content and return the commit HTML URL."""
        payload: dict = {
            "message": commit_message,
            "content": content_b64,
            "branch": self._branch,
            "author": {"name": self._author_name, "email": self._author_email},
            "committer": {"name": self._author_name, "email": self._author_email},
        }
        if sha is not None:
            payload["sha"] = sha

        try:
            with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                response = client.put(
                    self._contents_url(file_path), headers=self._headers, json=payload
                )
        except httpx.HTTPError as exc:
            raise _TransientError(f"PUT network error: {exc}") from exc

        if response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED):
            commit_url = response.json().get("commit", {}).get("html_url", "")
            if not commit_url:
                raise GitHubRecordsError(
                    "Record PUT succeeded but response did not include commit.html_url"
                )
            return commit_url
        if response.status_code == status.HTTP_409_CONFLICT:
            raise _ConflictError(response.text)
        if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise _TransientError(f"PUT 5xx: {response.status_code} {response.text}")
        raise GitHubRecordsError(
            f"Record PUT failed fast with HTTP {response.status_code}: {response.text}"
        )


class _ConflictError(Exception):
    """Internal sentinel — raised on 409 Conflict so the caller retries the GET/PUT cycle."""


class _TransientError(Exception):
    """Internal sentinel — raised on retryable transport or 5xx failures."""
