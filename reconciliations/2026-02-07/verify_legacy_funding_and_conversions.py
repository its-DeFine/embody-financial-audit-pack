#!/usr/bin/env python3
"""
Verify key legacy incentive-program funding flows and LPT conversion transactions on-chain (Arbitrum One).

This is complementary to:
- build_ledger_and_verify.py (partner payouts: TicketBroker + direct ETH transfers)
- verify_usdc_treasury.py (USDC treasury operations)

What this script verifies:
1) Phase 1 testing wallet returns (ETH transfers) -> gateway
2) Safe wallet ETH transfers (Safe execTransaction) -> gateway
3) Phase 3 treasury disbursements (native ETH transfers) -> gateway/backend/ops settlement
4) Aug 29, 2025 LPT conversion transactions (LPT -> USDC and LPT -> ETH-like via WETH) from treasury

Outputs (written next to this script):
- legacy_funding_flows.csv
- lpt_conversions_onchain.csv
- legacy_funding_and_conversions_summary.json
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

getcontext().prec = 60

RPC_URL = "https://arb1.arbitrum.io/rpc"

# Program actors (Arbitrum One)
TREASURY = "0x04e334ff13c71488094e24f4fab53a8fafe2f9bb"
GATEWAY = "0x8a8053c21696f27ed305a03bd1efc5d068d91d0e"
TESTING_WALLET = "0xa03113bab8d4ebe5695591f60011741233e8b82f"
SAFE_WALLET = "0xc34b3753c164fbc3fc066fc1a46b3eee8adb33e6"

# Tokens (Arbitrum One)
LPT = "0x289ba1701c2f088cf0faf8b3705246331cb8a839"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDR = "0x" + "0" * 40


def _lower_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    if not a:
        return a
    if a.startswith("0x") and len(a) == 42:
        return a
    if a.startswith("0x"):
        return "0x" + a[2:]
    return "0x" + a


def _decode_topic_addr(topic: str) -> str:
    t = topic.lower()
    if not t.startswith("0x") or len(t) != 66:
        raise ValueError(f"bad topic: {topic}")
    return "0x" + t[-40:]


def _dec(value: int, decimals: int) -> Decimal:
    return Decimal(value) / (Decimal(10) ** Decimal(decimals))


def _curl_rpc(method: str, params: list[Any], *, retries: int = 6) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload)
    last_err: Optional[str] = None
    for attempt in range(retries):
        try:
            out = subprocess.check_output(
                [
                    "curl",
                    "-sS",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    "60",
                    "-X",
                    "POST",
                    "-H",
                    "content-type: application/json",
                    "--data",
                    data,
                    RPC_URL,
                ],
                text=True,
                stderr=subprocess.STDOUT,
            )
            resp = json.loads(out)
            if "error" in resp:
                raise RuntimeError(json.dumps(resp["error"]))
            return resp["result"]
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(min(2.0**attempt, 12.0))
    raise RuntimeError(f"RPC failed after {retries} attempts: {method}: {last_err}")


def _get_tx(tx_hash: str) -> Dict[str, Any]:
    r = _curl_rpc("eth_getTransactionByHash", [tx_hash])
    if r is None:
        raise RuntimeError(f"tx not found: {tx_hash}")
    return r


def _get_receipt(tx_hash: str) -> Dict[str, Any]:
    r = _curl_rpc("eth_getTransactionReceipt", [tx_hash])
    if r is None:
        raise RuntimeError(f"receipt not found: {tx_hash}")
    return r


def _receipt_ok(receipt: Dict[str, Any]) -> bool:
    status = receipt.get("status")
    if status is None:
        return False
    if isinstance(status, str):
        return int(status, 16) == 1
    return status == 1


def _receipt_gas_fee_eth(receipt: Dict[str, Any]) -> Decimal:
    gas_used = int(receipt.get("gasUsed") or "0x0", 16)
    eff_price = int(receipt.get("effectiveGasPrice") or "0x0", 16)
    return Decimal(gas_used * eff_price) / Decimal(10**18)


def _erc20_transfers_in_receipt(receipt: Dict[str, Any], *, token: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for log in receipt.get("logs") or []:
        if _lower_addr(log.get("address") or "") != _lower_addr(token):
            continue
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC0:
            continue
        if len(topics) < 3:
            continue
        out.append(
            {
                "from": _decode_topic_addr(topics[1]),
                "to": _decode_topic_addr(topics[2]),
                "value": int(log.get("data") or "0x0", 16),
            }
        )
    return out


def _decode_safe_exec_transaction(input_hex: str) -> Dict[str, Any]:
    """
    Decode Safe v1.4.1 execTransaction calldata.

    We only need the first 2 ABI parameters:
    - to: address
    - value: uint256

    ABI layout:
    - 4 bytes function selector
    - 32 bytes `to` (address right-aligned)
    - 32 bytes `value`

    Returns: {"to": address, "value_wei": int}
    """
    h = (input_hex or "").strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) < 8 + 64 + 64:
        raise RuntimeError("unexpected execTransaction calldata length")

    # Skip function selector (4 bytes = 8 hex chars).
    to_word = h[8 : 8 + 64]
    value_word = h[8 + 64 : 8 + 64 + 64]

    to_addr = "0x" + to_word[-40:]
    value_wei = int(value_word, 16)
    return {"to": _lower_addr(to_addr), "value_wei": value_wei}


@dataclass(frozen=True)
class FlowRow:
    chain: str
    kind: str
    tx_hash: str
    block_number: int
    from_addr: str
    to_addr: str
    asset: str
    amount_raw: int
    decimals: int
    amount: str
    gas_fee_eth: str
    status: str
    note: str


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main() -> int:
    out_dir = Path(__file__).resolve().parent

    legacy_root = out_dir / "inputs" / "legacy_2025-11-05"

    # Inputs
    phase3_disbursements_json = legacy_root / "phase3_treasury_disbursements.json"
    safe_eth_csv = legacy_root / "safe_wallet_eth_transfers.csv"
    safe_lpt_csv = legacy_root / "safe_wallet_lpt_transfers.csv"
    testing_returns_csv = legacy_root / "testing_wallet_returns.csv"
    treasury_tx_csv = legacy_root / "eth_payouts_0x04e334ff13c71488094e24f4fab53a8fafe2f9bb.csv"

    funding_rows: List[Dict[str, Any]] = []

    # 1) Phase 1 testing wallet returns -> gateway (plain ETH transfers)
    if testing_returns_csv.exists():
        with testing_returns_csv.open(newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                txh = row["tx_hash"].strip().lower()
                exp_amt = Decimal(row["amount_eth"])
                exp_wei = int((exp_amt * Decimal(10**18)).to_integral_value())
                tx = _get_tx(txh)
                rec = _get_receipt(txh)
                ok = _receipt_ok(rec)
                got_from = _lower_addr(tx.get("from") or "")
                got_to = _lower_addr(tx.get("to") or "")
                got_val = int(tx.get("value") or "0x0", 16)
                if got_from != TESTING_WALLET or got_to != GATEWAY or got_val != exp_wei or not ok:
                    raise RuntimeError(f"testing return mismatch: {txh}")
                funding_rows.append(
                    FlowRow(
                        chain="arbitrum-one",
                        kind="testing_wallet_return",
                        tx_hash=txh,
                        block_number=int(tx.get("blockNumber") or "0x0", 16),
                        from_addr=got_from,
                        to_addr=got_to,
                        asset="ETH",
                        amount_raw=got_val,
                        decimals=18,
                        amount=str(_dec(got_val, 18)),
                        gas_fee_eth=str(_receipt_gas_fee_eth(rec)),
                        status="success",
                        note="Phase 1 testing wallet return to gateway",
                    ).__dict__
                )

    # 2) Safe wallet ETH transfers -> gateway (Safe execTransaction)
    if safe_eth_csv.exists():
        with safe_eth_csv.open(newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                txh = row["tx_hash"].strip().lower()
                exp_amt = Decimal(row["amount_eth"])
                exp_wei = int((exp_amt * Decimal(10**18)).to_integral_value())
                tx = _get_tx(txh)
                rec = _get_receipt(txh)
                ok = _receipt_ok(rec)
                if _lower_addr(tx.get("to") or "") != SAFE_WALLET or not ok:
                    raise RuntimeError(f"safe eth tx mismatch (to/status): {txh}")
                decoded = _decode_safe_exec_transaction(tx.get("input") or "0x")
                if decoded["to"] != GATEWAY or decoded["value_wei"] != exp_wei:
                    raise RuntimeError(f"safe execTransaction mismatch: {txh}")
                funding_rows.append(
                    FlowRow(
                        chain="arbitrum-one",
                        kind="safe_exec_eth_transfer",
                        tx_hash=txh,
                        block_number=int(tx.get("blockNumber") or "0x0", 16),
                        from_addr=SAFE_WALLET,
                        to_addr=GATEWAY,
                        asset="ETH",
                        amount_raw=exp_wei,
                        decimals=18,
                        amount=str(_dec(exp_wei, 18)),
                        gas_fee_eth=str(_receipt_gas_fee_eth(rec)),
                        status="success",
                        note="Safe execTransaction ETH transfer to gateway",
                    ).__dict__
                )

    # 3) Phase 3 treasury disbursements (plain ETH transfers)
    if phase3_disbursements_json.exists():
        j = json.loads(phase3_disbursements_json.read_text())
        for t in j.get("transactions") or []:
            txh = t["transaction_hash"].strip().lower()
            exp_to = _lower_addr(t["recipient"])
            exp_amt = Decimal(t["amount_eth"])
            exp_wei = int((exp_amt * Decimal(10**18)).to_integral_value())
            tx = _get_tx(txh)
            rec = _get_receipt(txh)
            ok = _receipt_ok(rec)
            got_from = _lower_addr(tx.get("from") or "")
            got_to = _lower_addr(tx.get("to") or "")
            got_val = int(tx.get("value") or "0x0", 16)
            if got_from != TREASURY or got_to != exp_to or got_val != exp_wei or not ok:
                raise RuntimeError(f"treasury disbursement mismatch: {txh}")
            funding_rows.append(
                FlowRow(
                    chain="arbitrum-one",
                    kind="treasury_eth_disbursement",
                    tx_hash=txh,
                    block_number=int(tx.get("blockNumber") or "0x0", 16),
                    from_addr=got_from,
                    to_addr=got_to,
                    asset="ETH",
                    amount_raw=got_val,
                    decimals=18,
                    amount=str(_dec(got_val, 18)),
                    gas_fee_eth=str(_receipt_gas_fee_eth(rec)),
                    status="success",
                    note="Phase 3 treasury disbursement",
                ).__dict__
            )

    # 4) Safe wallet LPT transfers -> gateway (ERC20 Transfer logs)
    if safe_lpt_csv.exists():
        with safe_lpt_csv.open(newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                txh = row["tx_hash"].strip().lower()
                exp_amt = Decimal(row["amount_lpt"])
                exp_raw = int((exp_amt * Decimal(10**18)).to_integral_value())
                rec = _get_receipt(txh)
                ok = _receipt_ok(rec)
                if not ok:
                    raise RuntimeError(f"safe lpt tx failed: {txh}")
                transfers = _erc20_transfers_in_receipt(rec, token=LPT)
                got = sum(t["value"] for t in transfers if _lower_addr(t["from"]) == SAFE_WALLET and _lower_addr(t["to"]) == GATEWAY)
                if got != exp_raw:
                    raise RuntimeError(f"safe lpt transfer mismatch: {txh}: got {got} expected {exp_raw}")
                funding_rows.append(
                    FlowRow(
                        chain="arbitrum-one",
                        kind="safe_lpt_transfer",
                        tx_hash=txh,
                        block_number=int(rec.get("blockNumber") or "0x0", 16),
                        from_addr=SAFE_WALLET,
                        to_addr=GATEWAY,
                        asset="LPT",
                        amount_raw=got,
                        decimals=18,
                        amount=str(_dec(got, 18)),
                        gas_fee_eth=str(_receipt_gas_fee_eth(rec)),
                        status="success",
                        note="Safe LPT transfer to gateway",
                    ).__dict__
                )

    # Write funding flows CSV.
    funding_rows.sort(key=lambda r: (r["block_number"], r["tx_hash"], r["kind"]))
    funding_csv = out_dir / "legacy_funding_flows.csv"
    _write_csv(
        funding_csv,
        funding_rows,
        fieldnames=[
            "chain",
            "kind",
            "tx_hash",
            "block_number",
            "from_addr",
            "to_addr",
            "asset",
            "amount_raw",
            "decimals",
            "amount",
            "gas_fee_eth",
            "status",
            "note",
        ],
    )

    # LPT conversions: parse Aug 29 treasury txs for LPT out + USDC in + WETH gross/burn.
    conversions: List[Dict[str, Any]] = []
    if treasury_tx_csv.exists():
        tx_rows: List[Dict[str, str]] = []
        with treasury_tx_csv.open(newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if row["iso_utc"].startswith("2025-08-29"):
                    tx_rows.append(row)

        router = "0x2905d7e4d048d29954f81b02171dd313f457a4a4"

        for row in tx_rows:
            txh = row["tx_hash"].strip().lower()
            tx = _get_tx(txh)
            rec = _get_receipt(txh)

            lpt_transfers = _erc20_transfers_in_receipt(rec, token=LPT)
            usdc_transfers = _erc20_transfers_in_receipt(rec, token=USDC)
            weth_transfers = _erc20_transfers_in_receipt(rec, token=WETH)

            lpt_out = sum(
                t["value"]
                for t in lpt_transfers
                if _lower_addr(t["from"]) == TREASURY and _lower_addr(t["to"]) == router
            )
            usdc_in = sum(t["value"] for t in usdc_transfers if _lower_addr(t["to"]) == TREASURY)

            weth_gross_in = sum(t["value"] for t in weth_transfers if _lower_addr(t["to"]) == router and _lower_addr(t["from"]) != router)
            weth_burn = sum(t["value"] for t in weth_transfers if _lower_addr(t["from"]) == router and _lower_addr(t["to"]) == ZERO_ADDR)
            weth_other_out = sum(
                t["value"]
                for t in weth_transfers
                if _lower_addr(t["from"]) == router and _lower_addr(t["to"]) not in (router, ZERO_ADDR)
            )

            if lpt_out == 0 and usdc_in == 0 and weth_gross_in == 0 and weth_burn == 0:
                continue

            conv_type = "unknown"
            if lpt_out > 0 and usdc_in > 0:
                conv_type = "LPT_to_USDC"
            elif lpt_out > 0 and weth_gross_in > 0:
                conv_type = "LPT_to_ETH_like"

            conversions.append(
                {
                    "date_utc": row["iso_utc"],
                    "tx_hash": txh,
                    "to": _lower_addr(tx.get("to") or ""),
                    "conversion_type": conv_type,
                    "lpt_out": str(_dec(lpt_out, 18)),
                    "usdc_in": str(_dec(usdc_in, 6)),
                    "weth_gross_in": str(_dec(weth_gross_in, 18)),
                    "weth_burn": str(_dec(weth_burn, 18)),
                    "weth_other_out": str(_dec(weth_other_out, 18)),
                    "gas_fee_eth": str(_receipt_gas_fee_eth(rec)),
                    "status": "success" if _receipt_ok(rec) else "failed",
                }
            )

    conversions.sort(key=lambda r: (r["date_utc"], r["tx_hash"]))
    conversions_csv = out_dir / "lpt_conversions_onchain.csv"
    _write_csv(
        conversions_csv,
        conversions,
        fieldnames=[
            "date_utc",
            "tx_hash",
            "to",
            "conversion_type",
            "lpt_out",
            "usdc_in",
            "weth_gross_in",
            "weth_burn",
            "weth_other_out",
            "gas_fee_eth",
            "status",
        ],
    )

    # Summary totals
    def _sum_dec(rows: Iterable[Dict[str, Any]], key: str) -> Decimal:
        return sum((Decimal(r.get(key) or "0") for r in rows), Decimal("0"))

    totals = {
        "rpc_url": RPC_URL,
        "funding_flows": {
            "row_count": len(funding_rows),
            "eth_testing_wallet_returns": str(
                _sum_dec((r for r in funding_rows if r["kind"] == "testing_wallet_return"), "amount")
            ),
            "eth_safe_exec_transfers": str(
                _sum_dec((r for r in funding_rows if r["kind"] == "safe_exec_eth_transfer"), "amount")
            ),
            "lpt_safe_transfers": str(
                _sum_dec((r for r in funding_rows if r["kind"] == "safe_lpt_transfer"), "amount")
            ),
            "eth_treasury_disbursements": str(
                _sum_dec((r for r in funding_rows if r["kind"] == "treasury_eth_disbursement"), "amount")
            ),
        },
        "lpt_conversions": {
            "row_count": len(conversions),
            "lpt_out_total": str(_sum_dec(conversions, "lpt_out")),
            "usdc_in_total": str(_sum_dec(conversions, "usdc_in")),
            "weth_gross_in_total": str(_sum_dec(conversions, "weth_gross_in")),
            "weth_burn_total": str(_sum_dec(conversions, "weth_burn")),
            "weth_other_out_total": str(_sum_dec(conversions, "weth_other_out")),
        },
    }

    summary_json = out_dir / "legacy_funding_and_conversions_summary.json"
    summary_json.write_text(json.dumps(totals, indent=2, sort_keys=True) + "\n")

    print(json.dumps(totals, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
