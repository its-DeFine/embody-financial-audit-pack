# Embody Incentive Program — On-Chain Audit Pack

**As of:** 2026-02-07  
**Chain:** Arbitrum One

This packet is a public, single-commit snapshot intended to support **independent verification** of key treasury operations for the Embody incentive program:

- **ETH payouts (headline totals)** via:
  - TicketBroker `WinningTicketRedeemed` logs (on-chain events)
  - Native ETH transfers (no logs), verified by a tx-hash list
- **LPT conversions** (LPT -> USDC and LPT -> “ETH-like” via WETH flows)
- **USDC treasury reconciliation** (Transfer logs + `balanceOf` checks)

## Coverage Window (2025-05-15 → 2026-02-07)

Although this packet is generated **as-of 2026-02-07**, it covers the **full incentive program from inception through 2026-02-07**, including:
- Phase 1/2 payouts via TicketBroker `WinningTicketRedeemed` logs (sender `0x8a80…1d0e`)
  - Earliest redemption example (Phase 1 testing): 2025-05-15 tx `0xd305a04c5a57fa86167202d266c44281f077c3cb07d1ed06e1c9c139b1cd1ed0`
- Phase 3 payouts via direct ETH transfers from the backend payout wallet `0x0c7c…ac37` (verified from a tx-hash list)
- Post-snapshot payout events included in the totals:
  - 2025-12-22 (manual TicketBroker payout)
  - 2025-12-31 (TicketBroker payout run)
  - 2026-01-22 (TicketBroker payout run)

## Scope + Redaction Policy

This public audit pack intentionally does **not** publish:
- recipient rosters / join-date tables / partner-identifying payout summaries
- incentive strategy docs (rates, runway, profitability assumptions)

The goal is to keep the work **auditable** without publishing a ready-made “poaching list” or incentives playbook.

## Key Addresses

- TicketBroker (Arbitrum One): `0xa8bb618b1520e284046f3dfc448851a1ff26e41b`
- Treasury: `0x04e334ff13c71488094e24f4fab53a8fafe2f9bb`
- Legacy gateway sender (TicketBroker payouts for Phase 1/2): `0x8a8053c21696f27ed305a03bd1efc5d068d91d0e`
- Backend payout wallet (direct ETH payouts; also one TicketBroker redeem): `0x0c7ca5da3b10fa345c5713c5a14479a3af65ac37`
- TicketBroker sender used for Dec 31, 2025 and Jan 22, 2026 payout runs: `0xf2f5fccddf50c9e86c1bb171c07041ff0c612f2d`

## Headline Totals (Committed Snapshot)

Snapshot totals are recorded in:
- `computed_totals.json`

And can be recomputed from chain via:
- `verify_payout_totals.py`

## How To Verify

### 1) ETH payout totals (TicketBroker + direct ETH transfers)

Run:

```bash
python3 reconciliations/2026-02-07/verify_payout_totals.py
```

Notes:
- This script calls Arbitrum RPC (`https://arb1.arbitrum.io/rpc`).
- It validates TicketBroker payouts from on-chain logs.
- For native ETH transfers (Phase 3 direct payouts), it verifies each tx listed in:
  - `reconciliations/2026-02-07/inputs/phase3_direct_eth_transfers_txhashes.csv`
  This list contains **tx hashes only** (no recipient table).

Output:
- `reconciliations/2026-02-07/computed_totals.recomputed.json`

### 2) Legacy funding flows + LPT conversions (on-chain)

Run:

```bash
python3 reconciliations/2026-02-07/verify_legacy_funding_and_conversions.py
```

This verifies:
- Phase 1 testing wallet returns -> gateway
- Safe -> gateway contributions (decoded from Safe `execTransaction` calldata)
- Phase 3 treasury disbursements
- 2025-08-29 LPT conversions (LPT out, USDC in, WETH flows)

Outputs (written next to the script):
- `legacy_funding_flows.csv`
- `lpt_conversions_onchain.csv`
- `legacy_funding_and_conversions_summary.json`

### 3) USDC treasury reconciliation (on-chain)

Run:

```bash
python3 reconciliations/2026-02-07/verify_usdc_treasury.py
```

This verifies USDC/USDC.e `Transfer` logs involving the treasury and reconciles net flows against on-chain `balanceOf`.

Outputs (written next to the script):
- `usdc_transfers.csv` (generated locally; gitignored in this public pack)
- `usdc_outflows_by_recipient.csv` (generated locally; gitignored)
- `usdc_verification_summary.json`
