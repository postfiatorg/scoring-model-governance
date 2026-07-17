# Published pool-refresh records

Every completed candidate-pool refresh is published here automatically by
the governance service, one record per refresh under its environment
directory (`devnet/`, `testnet/`):

- `<date>-refresh-<id>.json` — the canonical machine-readable record: the
  release walk with every considered release's fallback reason, every
  evaluated candidate's rule outcome with pinned revisions and GPU
  assignments, the resulting pool (or the no-viable-pool finding that
  leaves the standing pool unchanged), and the content hashes and IPFS
  CID of the upstream LiveBench data files the refresh read.
- `<date>-refresh-<id>.md` — a short human-readable summary of the same
  refresh.

To re-verify a record: fetch the upstream files by their CID (or from
LiveBench directly, checking the recorded sha256 hashes), re-run the pool
rules in `governance_service/` against them, and compare the resulting
pool and exclusions with the record. The methodology behind the rules is
`docs/Methodology.md`.
