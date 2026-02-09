# Embody Financial Audit Pack (Public, Single-Commit Snapshot)

This repository is a **public on-chain audit pack** (single-commit snapshot) that supports independent verification of key Embody incentive-program treasury operations on Arbitrum.

What this repo is:
- A reproducible, source-linked packet focused on **on-chain verifiability** (tx hashes, logs, and RPC-verifiable totals).

What this repo is not:
- A full internal accounting system.
- A recipient roster / incentives playbook. We intentionally avoid publishing partner-identifying tables and incentive strategy docs here.

## Start Here

- Report: `reconciliations/2026-02-07/report.md`
- Totals (as-of 2026-02-07): `reconciliations/2026-02-07/computed_totals.json`
- Verification scripts:
  - TicketBroker + direct ETH payout totals: `reconciliations/2026-02-07/verify_payout_totals.py`
  - Legacy funding + LPT conversions: `reconciliations/2026-02-07/verify_legacy_funding_and_conversions.py`
  - USDC treasury flows: `reconciliations/2026-02-07/verify_usdc_treasury.py`

## Notes On Redaction

- Recipient-level payout tables are intentionally **not** committed in this public pack.
- Verification is performed from on-chain receipts/logs and from a tx-hash list for native ETH transfers where logs do not exist.

