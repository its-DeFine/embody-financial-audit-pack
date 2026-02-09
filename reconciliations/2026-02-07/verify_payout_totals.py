#!/usr/bin/env python3
"""
Public audit-pack verifier for payout totals (Arbitrum One).

This script recomputes the headline ETH payout totals in `computed_totals.json` using:
- TicketBroker `WinningTicketRedeemed` logs (event topic0)
- Native ETH transfers (phase 3 direct payments) enumerated by tx-hash list

This public pack intentionally omits recipient-level payout tables. This script verifies totals
without needing to publish partner-identifying CSVs.

Outputs (written next to this script):
- computed_totals.recomputed.json
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
from typing import Any, Dict, List, Optional, Tuple

getcontext().prec = 80


RPC_URL = "https://arb1.arbitrum.io/rpc"

# Livepeer TicketBroker (Arbitrum One).
TICKETBROKER = "0xa8bb618b1520e284046f3dfc448851a1ff26e41b"

# keccak256("WinningTicketRedeemed(address,address,uint256)")
WINNING_TICKET_REDEEMED_TOPIC0 = "0x8b87351a208c06e3ceee59d80725fd77a23b4129e1b51ca231fc89b40712649c"

# Program actors (Arbitrum One).
TREASURY = "0x04e334ff13c71488094e24f4fab53a8fafe2f9bb"
GATEWAY_SENDER = "0x8a8053c21696f27ed305a03bd1efc5d068d91d0e"  # TicketBroker sender used for Phase 1/2
BACKEND_PAYOUT_WALLET = "0x0c7ca5da3b10fa345c5713c5a14479a3af65ac37"  # direct payouts + one TicketBroker redeem
TICKET_SENDER_DEC31_JAN22 = "0xf2f5fccddf50c9e86c1bb171c07041ff0c612f2d"  # TicketBroker sender used for Dec31/Jan22

# TicketBroker payout tx hashes (post-snapshot runs).
MANUAL_TICKETBROKER_TXS = [
    # Dec 22, 2025 deterministic TicketBroker payout (0.012 ETH).
    "0x21378158d6bf9602fbffa0c296ef509aa30c2718f5ac91c781af8d9afa78ee89",
]

DEC31_TICKETBROKER_TXS = [
    # Dec 31, 2025: test + batch + makeup.
    "0x7f1fe966e79a4123309c1bc292a31e3f53a2bd4b60140eb26a8be34bd7f03281",
    "0x4a6ec583e2eb6d96b55cf569df5c721728852993a6b86b7a025d05755d2bdfa2",
    "0x7a5f0413c7791b67d6a36e12d5bf976001945b8a520cbb5a9cd7932861d39719",
]

JAN22_TICKETBROKER_TXS = [
    # Jan 22, 2026: test + batch + makeup.
    "0xbe47de7eb060393386b9edfd8aec9e2f02ec0fe6931e2df7faa205bc700459bf",
    "0x2ab0fb8b9009821c6173803e661376ce93db665d660a0b9fd5e9fd88ca68c463",
    "0xbefc71900f41af96674643d1bf807f5d6bae7ba6eb5a20012b9da19b75fec868",
]


def _topic_addr(addr: str) -> str:
    a = addr.lower()
    if not a.startswith("0x") or len(a) != 42:
        raise ValueError(f"bad address: {addr}")
    return "0x" + ("0" * 24) + a[2:]


def _lower_addr(addr: str) -> str:
    return (addr or "").strip().lower()


def _dec_wei_to_eth(value_wei: int) -> Decimal:
    return Decimal(value_wei) / Decimal(10**18)


def _curl_rpc(method: str, params: list[Any], *, retries: int = 8) -> Any:
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
            continue
    raise RuntimeError(f"RPC failed after {retries} attempts: {method}: {last_err}")


def _curl_rpc_batch(payloads: List[Dict[str, Any]], *, retries: int = 8) -> List[Dict[str, Any]]:
    """
    JSON-RPC batch request. Most Arbitrum RPC providers support this and it is much faster than
    doing thousands of single requests for large tx-hash lists.
    """
    data = json.dumps(payloads)

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
                    "120",
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
            if not isinstance(resp, list):
                raise RuntimeError(f"expected batch list response, got: {type(resp)}")
            for item in resp:
                if "error" in item:
                    raise RuntimeError(json.dumps(item["error"]))
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(min(2.0**attempt, 12.0))
            continue
    raise RuntimeError(f"RPC batch failed after {retries} attempts: {last_err}")


def _eth_block_number() -> int:
    r = _curl_rpc("eth_blockNumber", [])
    return int(r, 16)


def _eth_get_logs(address: str, topics: list[Any], from_block: int, to_block: int) -> List[Dict[str, Any]]:
    return _curl_rpc(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": address,
                "topics": topics,
            }
        ],
    )


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


def _sum_ticketbroker_redeems_in_receipt(receipt: Dict[str, Any]) -> Tuple[int, List[Tuple[str, str, int]]]:
    """
    Returns (sum_wei, [(sender, recipient, amount_wei), ...]) for WinningTicketRedeemed logs in the receipt.
    """
    total = 0
    rows: List[Tuple[str, str, int]] = []
    for log in receipt.get("logs") or []:
        if _lower_addr(log.get("address")) != _lower_addr(TICKETBROKER):
            continue
        topics = log.get("topics") or []
        if not topics or _lower_addr(topics[0]) != _lower_addr(WINNING_TICKET_REDEEMED_TOPIC0):
            continue
        if len(topics) < 3:
            continue
        sender = "0x" + topics[1][-40:].lower()
        recipient = "0x" + topics[2][-40:].lower()
        amount_wei = int(log.get("data") or "0x0", 16)
        total += amount_wei
        rows.append((sender, recipient, amount_wei))
    return total, rows


def _sum_ticketbroker_sender_logs(
    *,
    sender: str,
    from_block: int,
    to_block: int,
    chunk_size: int = 2_000_000,
) -> Tuple[int, int]:
    """
    Sum TicketBroker WinningTicketRedeemed amounts for a specific sender (topic1) across a block range.
    Returns (count, total_wei).
    """
    sender_topic = _topic_addr(sender)
    total_wei = 0
    count = 0

    cur = from_block
    end = to_block
    chunks = 0

    while cur <= end:
        hi = min(cur + chunk_size - 1, end)
        try:
            logs = _eth_get_logs(TICKETBROKER, [WINNING_TICKET_REDEEMED_TOPIC0, sender_topic], cur, hi)
        except RuntimeError as e:
            # If the provider complains about result size, back off the chunk size.
            msg = str(e).lower()
            if "more than" in msg or "limit" in msg or "response size" in msg:
                if chunk_size <= 10_000:
                    raise
                chunk_size = max(10_000, chunk_size // 2)
                continue
            raise

        for log in logs:
            amount_wei = int(log.get("data") or "0x0", 16)
            total_wei += amount_wei
            count += 1

        chunks += 1
        if chunks % 20 == 0:
            print(f"... scanned TicketBroker logs: {cur}-{hi} (count={count})", file=sys.stderr)

        cur = hi + 1

    return count, total_wei


def _load_phase3_direct_transfer_txhashes(path: Path) -> List[str]:
    txs: List[str] = []
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        if "tx_hash" not in (r.fieldnames or []):
            raise RuntimeError(f"missing tx_hash column: {path}")
        for row in r:
            h = (row.get("tx_hash") or "").strip()
            if h:
                txs.append(h)
    # De-dupe while preserving order.
    seen = set()
    out: List[str] = []
    for h in txs:
        k = h.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


@dataclass(frozen=True)
class DirectTransferResult:
    tx_count: int
    total_wei: int


def _sum_direct_eth_transfers_from_txhashes(tx_hashes: List[str]) -> DirectTransferResult:
    total_wei = 0
    ok_count = 0

    # Batch tx + receipt lookups to avoid thousands of HTTP round-trips.
    batch_size = 75  # 75 txs => 150 RPC calls; keep request bodies reasonably small.
    for start in range(0, len(tx_hashes), batch_size):
        chunk = tx_hashes[start : start + batch_size]
        payloads: List[Dict[str, Any]] = []
        id_map: Dict[int, Tuple[str, str]] = {}

        for j, tx_hash in enumerate(chunk):
            tx_id = j * 2
            rec_id = j * 2 + 1
            payloads.append(
                {"jsonrpc": "2.0", "id": tx_id, "method": "eth_getTransactionByHash", "params": [tx_hash]}
            )
            payloads.append(
                {"jsonrpc": "2.0", "id": rec_id, "method": "eth_getTransactionReceipt", "params": [tx_hash]}
            )
            id_map[tx_id] = ("tx", tx_hash)
            id_map[rec_id] = ("receipt", tx_hash)

        resp_items = _curl_rpc_batch(payloads)
        by_id: Dict[int, Dict[str, Any]] = {}
        for item in resp_items:
            by_id[int(item["id"])] = item

        for j, tx_hash in enumerate(chunk):
            tx_id = j * 2
            rec_id = j * 2 + 1
            tx_item = by_id.get(tx_id)
            rec_item = by_id.get(rec_id)
            if not tx_item or tx_item.get("result") is None:
                raise RuntimeError(f"tx not found: {tx_hash}")
            if not rec_item or rec_item.get("result") is None:
                raise RuntimeError(f"receipt not found: {tx_hash}")

            tx = tx_item["result"]
            receipt = rec_item["result"]
            if not _receipt_ok(receipt):
                raise RuntimeError(f"tx failed (status!=1): {tx_hash}")

            from_addr = _lower_addr(tx.get("from") or "")
            if from_addr != _lower_addr(BACKEND_PAYOUT_WALLET):
                raise RuntimeError(f"unexpected from for direct payout tx: {tx_hash}: {from_addr}")

            value_wei = int(tx.get("value") or "0x0", 16)
            total_wei += value_wei
            ok_count += 1

        if ok_count % 300 == 0 or ok_count == len(tx_hashes):
            print(f"... verified {ok_count}/{len(tx_hashes)} direct transfers", file=sys.stderr)

    return DirectTransferResult(tx_count=ok_count, total_wei=total_wei)


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    expected_path = out_dir / "computed_totals.json"
    recomputed_path = out_dir / "computed_totals.recomputed.json"

    expected: Dict[str, str] = {}
    if expected_path.exists():
        expected = json.loads(expected_path.read_text())

    latest_block = _eth_block_number()

    # 1) Phase 1+2 TicketBroker redemptions (sender = gateway). We start at the first known
    # redemption-era blocks to avoid scanning the full chain history.
    gateway_start_block = 337_000_000
    gateway_count, gateway_total_wei = _sum_ticketbroker_sender_logs(
        sender=GATEWAY_SENDER,
        from_block=gateway_start_block,
        to_block=latest_block,
    )

    # 2) Manual TicketBroker tx(s).
    manual_total_wei = 0
    for tx_hash in MANUAL_TICKETBROKER_TXS:
        rec = _get_receipt(tx_hash)
        if not _receipt_ok(rec):
            raise RuntimeError(f"manual TicketBroker tx failed: {tx_hash}")
        s, rows = _sum_ticketbroker_redeems_in_receipt(rec)
        # Sanity: these are expected to be backend-wallet sender.
        for sender, _recipient, _amt in rows:
            if _lower_addr(sender) != _lower_addr(BACKEND_PAYOUT_WALLET):
                raise RuntimeError(f"unexpected sender in manual TicketBroker tx {tx_hash}: {sender}")
        manual_total_wei += s

    # 3) Dec 31 TicketBroker payout run.
    dec31_total_wei = 0
    for tx_hash in DEC31_TICKETBROKER_TXS:
        rec = _get_receipt(tx_hash)
        if not _receipt_ok(rec):
            raise RuntimeError(f"Dec31 TicketBroker tx failed: {tx_hash}")
        s, rows = _sum_ticketbroker_redeems_in_receipt(rec)
        for sender, _recipient, _amt in rows:
            if _lower_addr(sender) != _lower_addr(TICKET_SENDER_DEC31_JAN22):
                raise RuntimeError(f"unexpected sender in Dec31 TicketBroker tx {tx_hash}: {sender}")
        dec31_total_wei += s

    # 4) Jan 22 TicketBroker payout run.
    jan22_total_wei = 0
    for tx_hash in JAN22_TICKETBROKER_TXS:
        rec = _get_receipt(tx_hash)
        if not _receipt_ok(rec):
            raise RuntimeError(f"Jan22 TicketBroker tx failed: {tx_hash}")
        s, rows = _sum_ticketbroker_redeems_in_receipt(rec)
        for sender, _recipient, _amt in rows:
            if _lower_addr(sender) != _lower_addr(TICKET_SENDER_DEC31_JAN22):
                raise RuntimeError(f"unexpected sender in Jan22 TicketBroker tx {tx_hash}: {sender}")
        jan22_total_wei += s

    # 5) Phase 3 direct transfers (native ETH transfers from backend wallet).
    phase3_list = out_dir / "inputs" / "phase3_direct_eth_transfers_txhashes.csv"
    phase3_hashes = _load_phase3_direct_transfer_txhashes(phase3_list)
    phase3 = _sum_direct_eth_transfers_from_txhashes(phase3_hashes)

    # Assemble recomputed totals (string decimals, wei-derived).
    phase12_eth = _dec_wei_to_eth(gateway_total_wei)
    manual_eth = _dec_wei_to_eth(manual_total_wei)
    dec31_eth = _dec_wei_to_eth(dec31_total_wei)
    jan22_eth = _dec_wei_to_eth(jan22_total_wei)
    phase3_eth = _dec_wei_to_eth(phase3.total_wei)
    total_eth = phase12_eth + manual_eth + dec31_eth + jan22_eth + phase3_eth

    out = {
        "phase1+2_ticket_eth": format(phase12_eth, "f"),
        "manual_ticket_eth": format(manual_eth, "f"),
        "dec31_ticket_eth": format(dec31_eth, "f"),
        "jan22_ticket_eth": format(jan22_eth, "f"),
        "phase3_transfer_eth": format(phase3_eth, "f"),
        "total_eth": format(total_eth, "f"),
        "meta": {
            "latest_block": latest_block,
            "phase1+2_ticketbroker_sender": GATEWAY_SENDER,
            "phase1+2_ticketbroker_event_count": gateway_count,
            "phase3_direct_transfer_tx_count": phase3.tx_count,
        },
    }

    recomputed_path.write_text(json.dumps(out, indent=2) + "\n")

    print("Recomputed totals written to:", recomputed_path)

    # Compare against committed expected totals (if present).
    if expected:
        mismatches: List[str] = []
        for k in [
            "phase1+2_ticket_eth",
            "phase3_transfer_eth",
            "manual_ticket_eth",
            "dec31_ticket_eth",
            "jan22_ticket_eth",
            "total_eth",
        ]:
            if k not in expected:
                continue
            if Decimal(str(expected[k])) != Decimal(str(out[k])):
                mismatches.append(f"{k}: expected {expected[k]} got {out[k]}")

        if mismatches:
            print("Mismatch vs committed computed_totals.json:", file=sys.stderr)
            for m in mismatches:
                print(" -", m, file=sys.stderr)
            return 2

    print("OK: totals match committed computed_totals.json (string-exact).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
