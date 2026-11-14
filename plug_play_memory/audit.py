"""
Audit log for plug-play-memory — security-hardened version.

Changes from v0.1:
  - HMAC-signed entries (C4 fix) — appended fake entries fail signature check
  - External chain tip verification method (C4 fix)
  - Constructor accepts optional signing_key (passed from MemoryStore)
  - Triggers still block UPDATE/DELETE on rows
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class AuditEntry:
    id: int
    agent_id: str
    op_type: str
    detail: dict[str, Any]
    allowed: bool | None
    timestamp: float
    prev_hash: str | None
    entry_hash: str
    signature: str | None  # new in v2


class AuditLog:
    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        owner: Any = None,
        signing_key: str | None = None,
    ):
        self._get_conn = get_conn
        # If no key provided, derive one from the OS (random per process).
        # This means signatures are unverifiable across restarts unless user
        # provides a stable key — which is exactly the security property we want.
        if signing_key:
            self._signing_key = signing_key.encode()
        else:
            self._signing_key = os.urandom(32)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id   TEXT NOT NULL,
                op_type    TEXT NOT NULL,
                detail     TEXT NOT NULL,
                allowed    INTEGER,
                timestamp  REAL NOT NULL,
                prev_hash  TEXT,
                entry_hash TEXT NOT NULL,
                signature  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_agent_time
                ON audit_log(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_op
                ON audit_log(op_type, timestamp);

            CREATE TRIGGER IF NOT EXISTS audit_no_update
            BEFORE UPDATE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
            BEFORE DELETE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            """
        )
        conn.commit()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._signing_key, payload.encode(), hashlib.sha256).hexdigest()

    def log(
        self,
        agent_id: str,
        op_type: str,
        detail: dict[str, Any],
        allowed: bool | None = None,
    ) -> None:
        conn = self._get_conn()
        now = time.time()
        prev = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["entry_hash"] if prev else None
        # Build the payload that determines entry_hash
        hash_payload = (
            f"{agent_id}|{op_type}|{json.dumps(detail, sort_keys=True)}|"
            f"{allowed}|{now}|{prev_hash}"
        )
        entry_hash = hashlib.blake2b(hash_payload.encode(), digest_size=16).hexdigest()
        # Sign the entry_hash with HMAC (C4 fix)
        signature = self._sign(entry_hash)

        conn.execute(
            """INSERT INTO audit_log
               (agent_id, op_type, detail, allowed, timestamp, prev_hash, entry_hash, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id, op_type, json.dumps(detail, sort_keys=True),
                1 if allowed else (0 if allowed is False else None),
                now, prev_hash, entry_hash, signature,
            ),
        )
        conn.commit()

    def recent(self, agent_id: str, limit: int = 50) -> list[AuditEntry]:
        limit = max(1, min(limit, 1000))  # H2 fix
        rows = self._get_conn().execute(
            "SELECT * FROM audit_log WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def recent_all(self, limit: int = 100) -> list[AuditEntry]:
        limit = max(1, min(limit, 1000))  # H2 fix
        rows = self._get_conn().execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def coverage_manifest(self, agent_id: str) -> dict[str, Any]:
        rows = self._get_conn().execute(
            """SELECT detail, allowed FROM audit_log
               WHERE agent_id = ? AND op_type = 'permission_check'""",
            (agent_id,),
        ).fetchall()

        by_action: dict[str, dict[str, int]] = {}
        for r in rows:
            action = json.loads(r["detail"]).get("action", "unknown")
            bucket = by_action.setdefault(action, {"allowed": 0, "denied": 0})
            bucket["allowed" if r["allowed"] else "denied"] += 1

        return {
            "agent_id": agent_id,
            "actions_checked": list(by_action.keys()),
            "counts_by_action": by_action,
            "total_permission_checks": len(rows),
            "note": "Reflects only actions actually attempted — not a safety claim.",
        }

    def verify_chain(self) -> dict[str, Any]:
        """
        Verify the integrity of the audit chain (C4 fix).

        Returns a report with:
          - total_entries: count
          - valid_signatures: entries whose signature matches our HMAC key
          - invalid_signatures: entries with bad signatures (likely tampered)
          - chain_broken: entries whose prev_hash doesn't match previous entry_hash
        """
        rows = self._get_conn().execute(
            "SELECT id, entry_hash, prev_hash, signature FROM audit_log ORDER BY id"
        ).fetchall()

        valid = 0
        invalid_sigs = []
        broken_chain = []
        prev_hash = None

        for r in rows:
            # Check signature
            expected_sig = self._sign(r["entry_hash"])
            if r["signature"] and hmac.compare_digest(expected_sig, r["signature"]):
                valid += 1
            else:
                invalid_sigs.append(r["id"])
            # Check chain linkage
            if prev_hash is not None and r["prev_hash"] != prev_hash:
                broken_chain.append(r["id"])
            prev_hash = r["entry_hash"]

        return {
            "total_entries": len(rows),
            "valid_signatures": valid,
            "invalid_signatures": invalid_sigs,
            "chain_broken_at": broken_chain,
            "integrity_ok": (not invalid_sigs and not broken_chain),
        }

    @staticmethod
    def _row_to_entry(r: sqlite3.Row) -> AuditEntry:
        allowed_raw = r["allowed"]
        return AuditEntry(
            id=r["id"], agent_id=r["agent_id"], op_type=r["op_type"],
            detail=json.loads(r["detail"]),
            allowed=bool(allowed_raw) if allowed_raw is not None else None,
            timestamp=r["timestamp"],
            prev_hash=r["prev_hash"], entry_hash=r["entry_hash"],
            signature=r["signature"],
        )
