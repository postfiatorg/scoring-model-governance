"""Check the vendored dynamic-unl-scoring copies against the upstream branch.

Fetches each vendored file from the upstream repository and verifies its
content hash is one this service supports. Drift is a warning on main
(vendored copies are updated deliberately, not automatically) and blocking
on environment branches, mirroring the validator-scoring-sidecar check.

Usage: python scripts/check_vendor_freshness.py --branch main --mode warning
"""

import argparse
import hashlib
import sys
import urllib.error
import urllib.request

from governance_service.scoring import SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES

UPSTREAM_RAW_URL = (
    "https://raw.githubusercontent.com/postfiatorg/dynamic-unl-scoring/{branch}/{path}"
)

CHECKED_MODULES = [
    (
        "commit_reveal",
        "scoring_service/services/commit_reveal.py",
        SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    ),
]

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


def _fetch(branch: str, path: str) -> bytes:
    url = UPSTREAM_RAW_URL.format(branch=branch, path=path)
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


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
