"""Check the vendored dynamic-unl-scoring copies against the upstream branch.

Fetches each vendored file from the upstream repository and verifies its
content hash is one this service supports. Drift is a warning on main
(vendored copies are updated deliberately, not automatically) and blocking
on environment branches, mirroring the validator-scoring-sidecar check.

Also verifies the mechanical grading checker's rules table: every row's
pinned instructions hash must equal the hash of the upstream scoring
prompt's rendered system section, so a curation typo or an upstream
prompt-file rewrite surfaces here instead of failing a governance exam.

Usage: python scripts/check_vendor_freshness.py --branch main --mode warning
"""

import argparse
import hashlib
import importlib.util
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Load the pins by file path: importing through the package would execute
# governance_service.scoring's __init__, which needs runtime dependencies
# this bare-interpreter workflow deliberately does not install.
_PINS_PATH = Path(__file__).resolve().parent.parent / "governance_service" / "scoring" / "pins.py"
_spec = importlib.util.spec_from_file_location("vendor_pins", _PINS_PATH)
_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pins)
SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES = _pins.SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES
SUPPORTED_PARSER_CONTENT_HASHES = _pins.SUPPORTED_PARSER_CONTENT_HASHES

UPSTREAM_RAW_URL = (
    "https://raw.githubusercontent.com/postfiatorg/dynamic-unl-scoring/{branch}/{path}"
)

CHECKED_MODULES = [
    (
        "commit_reveal",
        "scoring_service/services/commit_reveal.py",
        SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    ),
    (
        "response_parser",
        "scoring_service/services/response_parser.py",
        SUPPORTED_PARSER_CONTENT_HASHES,
    ),
]

# Kept in sync with governance_service/services/checker.py RULES_PATH;
# constructed independently because this workflow installs no deps.
RULES_PATH = Path(__file__).resolve().parent.parent / "governance_service" / "scoring_rules.yaml"
SYSTEM_MARKER = "### SYSTEM PROMPT ###"
USER_MARKER = "### USER PROMPT ###"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


def _fetch(branch: str, path: str) -> bytes:
    url = UPSTREAM_RAW_URL.format(branch=branch, path=path)
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def _rules_table_hashes() -> dict[str, str]:
    """The (version, pinned hash) pairs from the rules table, read with a
    minimal line parser so this workflow stays dependency-free."""
    pins: dict[str, str] = {}
    version = None
    for line in RULES_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not line.startswith(" ") and stripped.endswith(":") and not stripped.startswith("#"):
            version = stripped[:-1]
        elif version and stripped.startswith("instructions_sha256:"):
            pins[version] = stripped.split(":", 1)[1].strip()
    return pins


def _rendered_system_section(prompt_text: str) -> str:
    return prompt_text.split(USER_MARKER)[0].replace(SYSTEM_MARKER, "").strip()


def check_freshness(branch: str, mode: str) -> int:
    drifted = []
    for name, path, supported in CHECKED_MODULES:
        try:
            content = _fetch(branch, path)
        except urllib.error.URLError as exc:
            print(f"ERROR: could not fetch {path} from {branch}: {exc}")
            return EXIT_ERROR
        digest = hashlib.sha256(content).hexdigest()
        if digest in supported:
            print(f"OK: {name} on {branch} matches a supported content hash")
        else:
            drifted.append(name)
            print(
                f"DRIFT: {name} on {branch} has content hash {digest}, "
                f"not in supported set {sorted(supported)}"
            )

    rules_pins = _rules_table_hashes()
    if not rules_pins:
        print("ERROR: no instruction-hash pins parsed from scoring_rules.yaml")
        return EXIT_ERROR
    for version, pinned in sorted(rules_pins.items()):
        if len(pinned) != 64 or any(c not in "0123456789abcdef" for c in pinned):
            print(f"ERROR: scoring_rules {version} pin is not a SHA-256 hex digest: {pinned!r}")
            return EXIT_ERROR
        path = f"prompts/scoring_{version}.txt"
        try:
            prompt = _fetch(branch, path)
        except urllib.error.URLError as exc:
            print(f"ERROR: could not fetch {path} from {branch}: {exc}")
            return EXIT_ERROR
        rendered = _rendered_system_section(prompt.decode("utf-8"))
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if digest == pinned:
            print(f"OK: scoring_rules {version} matches the upstream prompt's system section")
        else:
            drifted.append(f"scoring_rules:{version}")
            print(
                f"DRIFT: scoring_rules {version} pins {pinned} but the upstream "
                f"prompt's system section hashes to {digest}"
            )

    if not drifted:
        return EXIT_OK
    if mode == "warning":
        print(f"Vendored code drift detected ({', '.join(drifted)}) — warning mode, not failing")
        return EXIT_OK
    print(f"Vendored code drift detected ({', '.join(drifted)}) — blocking mode")
    return EXIT_DRIFT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True, help="Upstream branch to compare against")
    parser.add_argument("--mode", choices=["warning", "blocking"], required=True)
    args = parser.parse_args()
    return check_freshness(args.branch, args.mode)


if __name__ == "__main__":
    sys.exit(main())
