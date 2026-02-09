#!/usr/bin/env python3
"""
On-chain verification for the incentive program's USDC treasury operations on Arbitrum One.

What this script does:
- Pulls ERC-20 Transfer logs for treasury inflows/outflows (native USDC + bridged USDC.e).
- Computes totals (in/out/net) and reconciles against current on-chain balance.
- Computes doc-snapshot totals as-of 2025-11-07 UTC (from the legacy retrospective).
- Exports a tx-level CSV plus an outflows-by-recipient CSV and a JSON summary.

Outputs (written next to this script):
- usdc_transfers.csv
- usdc_outflows_by_recipient.csv
- usdc_verification_summary.json
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

getcontext().prec = 60


RPC_URL = "https://arb1.arbitrum.io/rpc"

TREASURY = "0x04e334ff13c71488094e24f4fab53a8fafe2f9bb"

# Native USDC on Arbitrum One.
USDC = {
    "symbol": "USDC",
    "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    "decimals": 6,
}

# Bridged USDC (Circle bridge legacy) on Arbitrum One.
USDC_E = {
    "symbol": "USDC.e",
    "address": "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
    "decimals": 6,
}

TOKENS = [USDC, USDC_E]

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Legacy retrospective snapshot date we reconcile "as-of".
DOC_SNAPSHOT_UTC = datetime(2025, 11, 7, 23, 59, 59, tzinfo=UTC)


def _topic_addr(addr: str) -> str:
    a = addr.lower()
    if not a.startswith("0x") or len(a) != 42:
        raise ValueError(f"bad address: {addr}")
    return "0x" + ("0" * 24) + a[2:]


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

    # arb1.arbitrum.io occasionally returns transient gateway errors; retry.
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
            # Exponential-ish backoff, capped.
            time.sleep(min(2.0**attempt, 12.0))
            continue
    raise RuntimeError(f"RPC failed after {retries} attempts: {method}: {last_err}")


def _eth_block_number() -> int:
    r = _curl_rpc("eth_blockNumber", [])
    return int(r, 16)


def _eth_get_block_timestamp(block_number: int) -> int:
    r = _curl_rpc("eth_getBlockByNumber", [hex(block_number), False])
    return int(r["timestamp"], 16)


def _erc20_balance_of(token: str, owner: str) -> int:
    # balanceOf(address) selector = 0x70a08231
    owner_no0x = owner.lower()[2:]
    data = "0x70a08231" + ("0" * 24) + owner_no0x
    r = _curl_rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return int(r, 16)


@dataclass(frozen=True)
class Transfer:
    token_symbol: str
    token_address: str
    tx_hash: str
    block_number: int
    timestamp_utc: str
    log_index: int
    from_addr: str
    to_addr: str
    direction: str  # in | out | self
    amount_raw: int
    amount: str  # normalized string (decimal)


def _fetch_token_transfers(token: Dict[str, Any], treasury: str) -> List[Dict[str, Any]]:
    addr = token["address"]
    from_topic = _topic_addr(treasury)
    to_topic = _topic_addr(treasury)

    out_logs = _curl_rpc(
        "eth_getLogs",
        [
            {
                "fromBlock": "0x0",
                "toBlock": "latest",
                "address": addr,
                "topics": [TRANSFER_TOPIC0, from_topic],
            }
        ],
    )

    in_logs = _curl_rpc(
        "eth_getLogs",
        [
            {
                "fromBlock": "0x0",
                "toBlock": "latest",
                "address": addr,
                "topics": [TRANSFER_TOPIC0, None, to_topic],
            }
        ],
    )

    # De-dupe by (tx_hash, log_index) in case of self-transfers or overlapping filters.
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for l in out_logs:
        merged[(l["transactionHash"].lower(), l["logIndex"].lower())] = l
    for l in in_logs:
        merged[(l["transactionHash"].lower(), l["logIndex"].lower())] = l
    return list(merged.values())


def _load_doc_expected_values(repo_root: Path) -> Dict[str, Any]:
    expected: Dict[str, Any] = {}

    summary_json = repo_root / "incentives" / "financial_summary.json"
    if summary_json.exists():
        j = json.loads(summary_json.read_text())
        expected["financial_summary_json"] = {
            "snapshot_date": j.get("summary", {}).get("snapshot_date"),
            "usdc_spending": j.get("usdc_spending"),
        }

    retro_md = repo_root / "incentives" / "EMBODY_INCENTIVE_PROGRAM_COMPLETE_RETROSPECTIVE.md"
    if retro_md.exists():
        t = retro_md.read_text()
        m_in = re.search(r"\\*\\*Total USDC Inflow:\\*\\*\\s*([0-9.,]+)\\s*USDC", t)
        m_out = re.search(r"\\*\\*Total USDC Outflow:\\*\\*\\s*([0-9.,]+)\\s*USDC", t)
        m_net = re.search(r"\\*\\*Net USDC Balance:\\*\\*\\s*\\+?([0-9.,]+)\\s*USDC", t)
        if m_in and m_out and m_net:
            expected["retrospective_md"] = {
                "total_usdc_inflow": m_in.group(1).replace(",", ""),
                "total_usdc_outflow": m_out.group(1).replace(",", ""),
                "net_usdc_balance": m_net.group(1).replace(",", ""),
            }

    return expected


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    out_dir = Path(__file__).resolve().parent

    expected = _load_doc_expected_values(repo_root)

    latest_block = _eth_block_number()
    latest_ts = _eth_get_block_timestamp(latest_block)
    as_of_utc = datetime.fromtimestamp(latest_ts, tz=UTC).isoformat()

    transfers: List[Transfer] = []
    block_ts_cache: Dict[int, int] = {}

    for token in TOKENS:
        raw_logs = _fetch_token_transfers(token, TREASURY)
        decimals = int(token["decimals"])

        # Cache timestamps for blocks we touch.
        blocks = {int(l["blockNumber"], 16) for l in raw_logs}
        for b in sorted(blocks):
            if b not in block_ts_cache:
                block_ts_cache[b] = _eth_get_block_timestamp(b)

        for l in raw_logs:
            bn = int(l["blockNumber"], 16)
            ts = block_ts_cache[bn]
            dt = datetime.fromtimestamp(ts, tz=UTC).isoformat()
            tx_hash = l["transactionHash"].lower()
            log_index = int(l["logIndex"], 16)

            from_addr = _decode_topic_addr(l["topics"][1])
            to_addr = _decode_topic_addr(l["topics"][2])
            amount_raw = int(l["data"], 16)

            if from_addr == TREASURY and to_addr == TREASURY:
                direction = "self"
            elif from_addr == TREASURY:
                direction = "out"
            elif to_addr == TREASURY:
                direction = "in"
            else:
                # Shouldn't happen given the queries, but keep it explicit if it does.
                direction = "other"

            transfers.append(
                Transfer(
                    token_symbol=token["symbol"],
                    token_address=token["address"],
                    tx_hash=tx_hash,
                    block_number=bn,
                    timestamp_utc=dt,
                    log_index=log_index,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    direction=direction,
                    amount_raw=amount_raw,
                    amount=str(_dec(amount_raw, decimals)),
                )
            )

    # Sort consistently for stable diffs.
    transfers.sort(key=lambda t: (t.token_symbol, t.block_number, t.tx_hash, t.log_index))

    # Verify net == on-chain balance.
    summaries: Dict[str, Any] = {
        "as_of_block": latest_block,
        "as_of_utc": as_of_utc,
        "rpc_url": RPC_URL,
        "treasury": TREASURY,
        "doc_snapshot_utc": DOC_SNAPSHOT_UTC.isoformat(),
        "expected_sources": expected,
        "tokens": {},
    }

    # Export transfers CSV.
    transfers_csv = out_dir / "usdc_transfers.csv"
    with transfers_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "token_symbol",
                "token_address",
                "tx_hash",
                "block_number",
                "timestamp_utc",
                "log_index",
                "from",
                "to",
                "direction",
                "amount_raw",
                "amount",
            ],
        )
        w.writeheader()
        for t in transfers:
            w.writerow(
                {
                    "token_symbol": t.token_symbol,
                    "token_address": t.token_address,
                    "tx_hash": t.tx_hash,
                    "block_number": t.block_number,
                    "timestamp_utc": t.timestamp_utc,
                    "log_index": t.log_index,
                    "from": t.from_addr,
                    "to": t.to_addr,
                    "direction": t.direction,
                    "amount_raw": str(t.amount_raw),
                    "amount": t.amount,
                }
            )

    # Aggregate outflows by recipient (all-time + doc snapshot).
    outflows_by_recipient: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for token in TOKENS:
        token_symbol = token["symbol"]
        token_addr = token["address"]
        decimals = int(token["decimals"])

        tin_raw = 0
        tout_raw = 0
        tin_snapshot_raw = 0
        tout_snapshot_raw = 0
        out_count = 0
        out_count_snapshot = 0
        in_count = 0
        in_count_snapshot = 0

        for t in transfers:
            if t.token_symbol != token_symbol:
                continue

            dt = datetime.fromisoformat(t.timestamp_utc)
            is_snapshot = dt <= DOC_SNAPSHOT_UTC

            if t.direction == "in":
                in_count += 1
                tin_raw += t.amount_raw
                if is_snapshot:
                    in_count_snapshot += 1
                    tin_snapshot_raw += t.amount_raw
            elif t.direction == "out":
                out_count += 1
                tout_raw += t.amount_raw
                if is_snapshot:
                    out_count_snapshot += 1
                    tout_snapshot_raw += t.amount_raw

                key = (token_symbol, t.to_addr)
                r = outflows_by_recipient.setdefault(
                    key,
                    {
                        "token_symbol": token_symbol,
                        "token_address": token_addr,
                        "recipient": t.to_addr,
                        "tx_count_total": 0,
                        "amount_total_raw": 0,
                        "tx_count_upto_doc_snapshot": 0,
                        "amount_upto_doc_snapshot_raw": 0,
                    },
                )
                r["tx_count_total"] += 1
                r["amount_total_raw"] += t.amount_raw
                if is_snapshot:
                    r["tx_count_upto_doc_snapshot"] += 1
                    r["amount_upto_doc_snapshot_raw"] += t.amount_raw

        bal_raw = _erc20_balance_of(token_addr, TREASURY)
        net_raw = tin_raw - tout_raw
        net_ok = bal_raw == net_raw

        summaries["tokens"][token_symbol] = {
            "token_address": token_addr,
            "decimals": decimals,
            "current_balance_raw": str(bal_raw),
            "current_balance": str(_dec(bal_raw, decimals)),
            "inflow_count_total": in_count,
            "outflow_count_total": out_count,
            "inflow_total_raw": str(tin_raw),
            "outflow_total_raw": str(tout_raw),
            "net_raw": str(net_raw),
            "inflow_total": str(_dec(tin_raw, decimals)),
            "outflow_total": str(_dec(tout_raw, decimals)),
            "net": str(_dec(net_raw, decimals)),
            "balance_reconciles": net_ok,
            "doc_snapshot": {
                "inflow_count": in_count_snapshot,
                "outflow_count": out_count_snapshot,
                "inflow_total_raw": str(tin_snapshot_raw),
                "outflow_total_raw": str(tout_snapshot_raw),
                "net_raw": str(tin_snapshot_raw - tout_snapshot_raw),
                "inflow_total": str(_dec(tin_snapshot_raw, decimals)),
                "outflow_total": str(_dec(tout_snapshot_raw, decimals)),
                "net": str(_dec(tin_snapshot_raw - tout_snapshot_raw, decimals)),
            },
        }

    outflows_csv = out_dir / "usdc_outflows_by_recipient.csv"
    rows = list(outflows_by_recipient.values())
    rows.sort(
        key=lambda r: (
            r["token_symbol"],
            -int(r["amount_total_raw"]),
            r["recipient"],
        )
    )
    with outflows_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "token_symbol",
                "token_address",
                "recipient",
                "tx_count_total",
                "amount_total_raw",
                "tx_count_upto_doc_snapshot",
                "amount_upto_doc_snapshot_raw",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary_json = out_dir / "usdc_verification_summary.json"
    summary_json.write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")

    # Minimal stdout for human usage.
    print(json.dumps(summaries["tokens"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
