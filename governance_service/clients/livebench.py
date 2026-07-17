"""LiveBench leaderboard client.

Fetches the leaderboard's versioned static data files (per-release score
table, category mapping, and the site model registry), validates them
strictly, and reproduces the site's own averaging computation. The data
contract is the site's internal format, so every parse failure raises
instead of guessing.
"""

import csv
import hashlib
import io
import json
import logging
import re
import time

import httpx

from governance_service.config import settings
from governance_service.models import LeaderboardStanding, RegistryEntry, SnapshotFile

logger = logging.getLogger(__name__)

RELEASE_TABLE_PATTERN = re.compile(r"^table_(\d{4}_\d{2}_\d{2})\.csv$")
REGISTRY_ENTRY_PATTERN = re.compile(r'"([^"]+)":\s*\{([^}]*)\}')
REGISTRY_FIELD_PATTERNS = {
    "url": re.compile(r'url:\s*"([^"]*)"'),
    "organization": re.compile(r'organization:\s*"([^"]*)"'),
    "display_name": re.compile(r'displayName:\s*"([^"]*)"'),
}
# The registry carries every model the site has ever listed; a result far
# smaller than history means the format shifted under the parser.
MIN_REGISTRY_ENTRIES = 50

MODEL_COLUMN = "model"


class LiveBenchRequestError(RuntimeError):
    """Raised when LiveBench data cannot be fetched."""


class LiveBenchSchemaError(RuntimeError):
    """Raised when fetched LiveBench data does not match the known format."""


def _get(client: httpx.Client, url: str, headers: dict | None = None) -> bytes:
    """GET with exponential backoff; raises LiveBenchRequestError on failure."""
    last_error: Exception | None = None
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < settings.http_max_retries:
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "LiveBench request attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise LiveBenchRequestError(f"LiveBench request failed: {url} — {last_error}")


def discover_releases(client: httpx.Client) -> list[str]:
    """List available leaderboard releases, oldest first."""
    headers = {}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    raw = _get(client, settings.livebench_releases_api_url, headers=headers)
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise LiveBenchSchemaError(f"Release listing is not JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise LiveBenchSchemaError("Release listing did not return a file list")

    releases = []
    for entry in entries:
        match = RELEASE_TABLE_PATTERN.match(entry.get("name", ""))
        if match:
            releases.append(match.group(1))
    if not releases:
        raise LiveBenchSchemaError("No release tables found in the site repository")
    return sorted(releases)


def fetch_release_files(client: httpx.Client, release: str) -> tuple[bytes, bytes]:
    """Fetch one release's score table and category mapping."""
    table = _get(client, f"{settings.livebench_base_url}/table_{release}.csv")
    categories = _get(client, f"{settings.livebench_base_url}/categories_{release}.json")
    return table, categories


def fetch_registry(client: httpx.Client) -> bytes:
    """Fetch the site model registry (modelLinks.js)."""
    return _get(client, settings.livebench_registry_url)


def parse_registry(raw: bytes) -> dict[str, RegistryEntry]:
    """Parse the JS model registry into per-model entries.

    The regex reads each entry up to its first closing brace and treats
    absent fields as empty; that tolerance is bounded by the minimum-entry
    check below, which catches wholesale format drift.
    """
    text = raw.decode("utf-8")
    entries: dict[str, RegistryEntry] = {}
    for key, body in REGISTRY_ENTRY_PATTERN.findall(text):
        fields = {}
        for name, pattern in REGISTRY_FIELD_PATTERNS.items():
            match = pattern.search(body)
            fields[name] = match.group(1) if match else ""
        entries[key] = RegistryEntry(
            openweight="openweight: true" in body,
            organization=fields["organization"],
            display_name=fields["display_name"],
            url=fields["url"],
        )
    if len(entries) < MIN_REGISTRY_ENTRIES:
        raise LiveBenchSchemaError(
            f"Registry parsed only {len(entries)} entries; format likely changed"
        )
    return entries


def parse_categories(raw: bytes) -> dict[str, list[str]]:
    """Parse and validate the category-to-tasks mapping."""
    try:
        categories = json.loads(raw)
    except ValueError as exc:
        raise LiveBenchSchemaError(f"Category mapping is not JSON: {exc}") from exc
    if not isinstance(categories, dict) or not categories:
        raise LiveBenchSchemaError("Category mapping is not a non-empty object")
    for category, tasks in categories.items():
        if not isinstance(tasks, list) or not tasks or not all(
            isinstance(task, str) for task in tasks
        ):
            raise LiveBenchSchemaError(f"Category '{category}' has no valid task list")
    return categories


def _parse_score(model_key: str, task: str, cell: str) -> float | None:
    if cell == "":
        return None
    try:
        return float(cell)
    except ValueError as exc:
        raise LiveBenchSchemaError(
            f"Non-numeric score for model '{model_key}', task '{task}': {cell!r}"
        ) from exc


def compute_standings(
    table_raw: bytes,
    categories: dict[str, list[str]],
    registry: dict[str, RegistryEntry],
) -> list[LeaderboardStanding]:
    """Compute per-model standings exactly as the leaderboard site does.

    Category average is the mean of the category's non-missing task scores;
    the global average is the mean of the category averages. Like the site,
    a model missing an entire category has no global average and is
    excluded, and a model absent from the registry is not shown.
    """
    reader = csv.DictReader(io.StringIO(table_raw.decode("utf-8")))
    header = reader.fieldnames or []
    if MODEL_COLUMN not in header:
        raise LiveBenchSchemaError(f"Score table has no '{MODEL_COLUMN}' column")
    for category, tasks in categories.items():
        missing = [task for task in tasks if task not in header]
        if missing:
            raise LiveBenchSchemaError(
                f"Category '{category}' tasks missing from score table: {missing}"
            )

    standings = []
    for row in reader:
        model_key = row[MODEL_COLUMN]
        entry = registry.get(model_key)
        if entry is None:
            continue

        category_averages = {}
        for category, tasks in categories.items():
            scores = [
                score
                for task in tasks
                if (score := _parse_score(model_key, task, row.get(task) or "")) is not None
            ]
            if not scores:
                category_averages = {}
                break
            category_averages[category] = sum(scores) / len(scores)
        if not category_averages:
            continue

        standings.append(
            LeaderboardStanding(
                model_key=model_key,
                global_average=sum(category_averages.values()) / len(category_averages),
                category_averages=category_averages,
                organization=entry.organization,
                display_name=entry.display_name,
                openweight=entry.openweight,
            )
        )

    standings.sort(key=lambda standing: standing.global_average, reverse=True)
    return standings


def snapshot(name: str, raw: bytes) -> SnapshotFile:
    """Content-hash one raw upstream file for the audit record."""
    return SnapshotFile(
        name=name,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        content=raw,
    )
