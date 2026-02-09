# Embody Financial Audit Pack (Public, Single-Commit Snapshot)

This repository is a **public on-chain audit pack** (single-commit snapshot) that supports independent verification of key Embody incentive-program treasury operations on Arbitrum.

## Coverage Window

This audit pack is **generated as-of 2026-02-07**, but it is designed to cover the **full incentive program from inception (first on-chain payouts in 2025-05-15) through that date**, including:
- Phase 1/2 TicketBroker payouts (on-chain event logs, sender `0x8a80…1d0e`)
- Phase 3 direct ETH payouts (native transfers, sender `0x0c7c…ac37`, verified from a tx-hash list)
- Later payout runs (Dec 22, Dec 31, 2025; Jan 22, 2026)
- Legacy funding flows + LPT conversions + USDC treasury reconciliation

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
