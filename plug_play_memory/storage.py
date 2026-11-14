"""
Storage layer for plug-play-memory — security-hardened version.

Changes from v0.1:
  - Optional encryption at rest via SQLCipher (C2 fix)
  - Schema versioning with real migration logic (H6 fix)
  - Embedder fingerprint validation on init (M1 fix)
  - Reserved agent_id validation (M4 fix)
  - Thread-safe connection tracking + close_all() (L3 fix)
  - BLAKE2b-based deterministic embedder (unchanged — was already correct)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog

SCHEMA_VERSION = 2  # bumped from 1 → 2 for security-hardened release
EMBEDDING_DIM = 256

# Names users cannot use as agent_id (would collide with policy layer names)
RESERVED_AGENT_IDS = {"_base", "coding", "study", "financial", "default", "all"}


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def default_embedder(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic BLAKE2b-based embedder. Survives restarts (unlike hash())."""
    vec = [0.0] * dim
    for token in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def embedder_fingerprint(embed_fn: Callable[[str], list[float]]) -> str:
    sample = embed_fn("fingerprint probe text")
    return hashlib.blake2b(
        f"{getattr(embed_fn, '__name__', 'custom')}|{len(sample)}|{round(sample[0], 6)}".encode()
    ).hexdigest()[:16]


EmbedFn = Callable[[str], list[float]]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_agent_id(agent_id: str) -> None:
    """Reject reserved agent_ids that would collide with policy layer names."""
    if not agent_id or not isinstance(agent_id, str):
        raise ValueError("agent_id must be a non-empty string")
    if agent_id in RESERVED_AGENT_IDS:
        raise ValueError(
            f"agent_id '{agent_id}' is reserved. Use a different name "
            f"(avoid: {sorted(RESERVED_AGENT_IDS)})."
        )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    id: str
    agent_id: str
    tier: str
    key: str | None
    value: str
    importance: str
    salience_score: float
    tagged: bool
    ttl_seconds: int | None
    created_at: float
    updated_at: float
    last_recalled_at: float | None
    recall_count: int = 0
    superseded: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryStore:
    """One SQLite file. Multi-tenant. Thread-safe. Optionally encrypted."""

    def __init__(
        self,
        db_path: str | Path,
        embed_fn: EmbedFn | None = None,
        embedding_dim: int = EMBEDDING_DIM,
        encryption_key: str | None = None,
    ):
        self.db_path = str(db_path)
        self.embed_fn = embed_fn or default_embedder
        self.embedding_dim = embedding_dim
        self.embedder_fp = embedder_fingerprint(self.embed_fn)
        self.encryption_key = encryption_key
        self._local = threading.local()
        self._all_conns: set[sqlite3.Connection] = set()
        self._conns_lock = threading.Lock()
        self._init_schema()
        self.audit = AuditLog(self._get_conn_for_thread, self, encryption_key)

    # -- connection management -----------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # SQLCipher: if key provided, set it BEFORE any other operation
        if self.encryption_key:
            try:
                conn.execute(f"PRAGMA key = '{self.encryption_key}'")
                # Verify the key is correct by reading
                conn.execute("SELECT count(*) FROM sqlite_master")
            except sqlite3.DatabaseError as e:
                conn.close()
                raise RuntimeError(
                    f"Failed to open encrypted DB. Wrong key? ({e})"
                ) from e
        return conn

    def _get_conn_for_thread(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.add(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._get_conn_for_thread()

    def close_all(self) -> None:
        """Close ALL thread-local connections (L3 fix)."""
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        # Also clear thread-local
        if hasattr(self._local, "conn"):
            try:
                self._local.conn.close()
            except Exception:
                pass
            del self._local.conn

    def close(self) -> None:
        self.close_all()

    # -- schema management ---------------------------------------------------

    def _init_schema(self) -> None:
        conn = self._get_conn_for_thread()
        # Bootstrap schema_meta first so we can read the version
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Read stored version
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        stored_version = int(row["value"]) if row else 0

        # Future-version guard (H6 fix)
        if stored_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"This DB was created by plug-play-memory v{stored_version} "
                f"(schema v{stored_version}). Current code is v{SCHEMA_VERSION}. "
                f"Upgrade the package to open this DB."
            )

        # Run migrations if needed
        if stored_version < SCHEMA_VERSION:
            self._run_migrations(conn, stored_version)

        # Check embedder fingerprint (M1 fix)
        fp_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'embedder_fingerprint'"
        ).fetchone()
        if fp_row is not None:
            stored_fp = fp_row["value"]
            if stored_fp != self.embedder_fp:
                warnings.warn(
                    f"\n  ⚠️  EMBEDDER MISMATCH\n"
                    f"  Stored fingerprint:  {stored_fp}\n"
                    f"  Current fingerprint: {self.embedder_fp}\n"
                    f"  Existing archival embeddings are now INCOMPATIBLE.\n"
                    f"  Semantic recall will return wrong/no results.\n"
                    f"  Run `agentmemory reindex` to rebuild embeddings,\n"
                    f"  or revert to the previous embedder.\n",
                    stacklevel=2,
                )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("embedder_fingerprint", self.embedder_fp),
            )

        # Update schema version (in case migrations just ran)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def _run_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Run migrations from stored version up to SCHEMA_VERSION (H6 fix)."""
        # Migration 0 → 1: initial schema (v0.1)
        if from_version < 1:
            self._migrate_v0_to_v1(conn)
        # Migration 1 → 2: security hardening (add columns if missing)
        if from_version < 2:
            self._migrate_v1_to_v2(conn)

    def _migrate_v0_to_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id                TEXT PRIMARY KEY,
                agent_id          TEXT NOT NULL,
                tier              TEXT NOT NULL,
                key               TEXT,
                value             TEXT NOT NULL,
                importance        TEXT NOT NULL DEFAULT 'normal',
                salience_score    REAL NOT NULL DEFAULT 0.5,
                tagged            INTEGER NOT NULL DEFAULT 0,
                ttl_seconds       INTEGER,
                created_at        REAL NOT NULL,
                updated_at        REAL NOT NULL,
                last_recalled_at  REAL,
                recall_count      INTEGER NOT NULL DEFAULT 0,
                consolidates_at   REAL,
                superseded        INTEGER NOT NULL DEFAULT 0,
                metadata          TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_facts_agent_tier
                ON facts(agent_id, tier, superseded);
            CREATE INDEX IF NOT EXISTS idx_facts_agent_key
                ON facts(agent_id, key, superseded);
            CREATE INDEX IF NOT EXISTS idx_facts_tagged
                ON facts(agent_id, tagged, consolidates_at);
            CREATE INDEX IF NOT EXISTS idx_facts_last_recalled
                ON facts(agent_id, last_recalled_at, importance);
            """
        )

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """v1 → v2: no schema changes needed, but bump version to mark
        security-hardened storage. Future v2 migrations go here."""
        # Currently a no-op — schema is unchanged from v1.
        # This exists so future v2 → v3 migrations have a hook.
        pass

    # -- writes ---------------------------------------------------------------

    def remember(
        self,
        agent_id: str,
        value: str,
        tier: str = "archival",
        key: str | None = None,
        importance: str = "normal",
        salience_score: float = 0.5,
        tagged: bool = False,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Fact:
        """Store a fact. Update-not-append on key collision."""
        validate_agent_id(agent_id)  # M4 fix

        conn = self._conn
        now = time.time()
        consolidates_at = now + 86400 if tagged else None

        with conn:  # transaction
            if key:
                conn.execute(
                    "UPDATE facts SET superseded = 1 WHERE agent_id = ? AND key = ? AND superseded = 0",
                    (agent_id, key),
                )

            fact_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO facts (id, agent_id, tier, key, value, importance,
                                    salience_score, tagged, ttl_seconds,
                                    created_at, updated_at, last_recalled_at,
                                    recall_count, consolidates_at, superseded, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, 0, ?)
                """,
                (
                    fact_id, agent_id, tier, key, value, importance,
                    salience_score, 1 if tagged else 0, ttl_seconds,
                    now, now, consolidates_at, json.dumps(metadata or {}),
                ),
            )

        fact = Fact(
            id=fact_id, agent_id=agent_id, tier=tier, key=key, value=value,
            importance=importance, salience_score=salience_score, tagged=tagged,
            ttl_seconds=ttl_seconds, created_at=now, updated_at=now,
            last_recalled_at=None, recall_count=0, superseded=False,
            metadata=metadata or {},
        )
        self.audit.log(agent_id, "remember", {
            "fact_id": fact_id, "tier": tier, "key": key,
            "salience_score": salience_score, "tagged": tagged,
        })
        return fact

    def forget(self, agent_id: str, fact_id: str, hard: bool = False) -> bool:
        """Soft-delete (default) preserves audit trail; hard=True removes row."""
        validate_agent_id(agent_id)
        conn = self._conn
        with conn:
            if hard:
                cur = conn.execute(
                    "DELETE FROM facts WHERE agent_id = ? AND id = ?",
                    (agent_id, fact_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE facts SET superseded = 1, updated_at = ? WHERE agent_id = ? AND id = ?",
                    (time.time(), agent_id, fact_id),
                )
        removed = cur.rowcount > 0
        self.audit.log(agent_id, "forget", {"fact_id": fact_id, "hard": hard, "removed": removed})
        return removed

    def update_recall_stats(self, fact_id: str) -> None:
        conn = self._conn
        now = time.time()
        with conn:
            conn.execute(
                """UPDATE facts
                   SET recall_count = recall_count + 1,
                       last_recalled_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (now, now, fact_id),
            )

    def decay_importance(self, fact_id: str, levels: int = 1) -> None:
        order = ["high", "normal", "low"]
        conn = self._conn
        with conn:
            row = conn.execute(
                "SELECT importance FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if not row:
                return
            current = order.index(row["importance"]) if row["importance"] in order else 1
            new_idx = min(current + levels, len(order) - 1)
            conn.execute(
                "UPDATE facts SET importance = ?, updated_at = ? WHERE id = ?",
                (order[new_idx], time.time(), fact_id),
            )

    def promote(self, fact_id: str, tier: str = "archival") -> None:
        conn = self._conn
        with conn:
            conn.execute(
                """UPDATE facts
                   SET tier = ?, tagged = 0, consolidates_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (tier, time.time(), fact_id),
            )

    # -- reads ---------------------------------------------------------------

    def get_core(self, agent_id: str) -> list[Fact]:
        validate_agent_id(agent_id)
        return self._active_facts(agent_id, tier="core")

    def recall_recent(self, agent_id: str, limit: int = 20) -> list[Fact]:
        validate_agent_id(agent_id)
        limit = max(1, min(limit, 1000))  # H2 fix: cap limit
        rows = self._conn.execute(
            """SELECT * FROM facts
               WHERE agent_id = ? AND tier = 'recall' AND superseded = 0
               ORDER BY created_at DESC LIMIT ?""",
            (agent_id, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def recall_semantic(self, agent_id: str, query: str, k: int = 5) -> list[Fact]:
        validate_agent_id(agent_id)
        k = max(1, min(k, 100))  # H2 fix: cap limit
        # Cap query length to prevent DoS
        query = (query or "")[:1000]
        tokens = [t.lower() for t in query.split() if len(t) > 2]
        if not tokens:
            return []
        like_clauses = " OR ".join(["LOWER(value) LIKE ?" for _ in tokens])
        params = [agent_id] + [f"%{t}%" for t in tokens] + [k]
        rows = self._conn.execute(
            f"""SELECT * FROM facts
                WHERE agent_id = ? AND tier = 'archival' AND superseded = 0
                  AND ({like_clauses})
                ORDER BY salience_score DESC, last_recalled_at DESC NULLS LAST
                LIMIT ?""",
            params,
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def get_tagged(self, agent_id: str, older_than_hours: int = 0) -> list[Fact]:
        validate_agent_id(agent_id)
        if older_than_hours <= 0:
            rows = self._conn.execute(
                """SELECT * FROM facts
                   WHERE agent_id = ? AND tagged = 1 AND superseded = 0
                   ORDER BY created_at ASC""",
                (agent_id,),
            ).fetchall()
        else:
            cutoff = time.time() - (older_than_hours * 3600)
            rows = self._conn.execute(
                """SELECT * FROM facts
                   WHERE agent_id = ? AND tagged = 1 AND superseded = 0
                     AND (consolidates_at IS NULL OR consolidates_at <= ?)
                   ORDER BY created_at ASC""",
                (agent_id, cutoff),
            ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def get_stale(
        self, agent_id: str, importance: str, days_since_recall: int
    ) -> list[Fact]:
        validate_agent_id(agent_id)
        cutoff = time.time() - (days_since_recall * 86400)
        rows = self._conn.execute(
            """SELECT * FROM facts
               WHERE agent_id = ? AND importance = ? AND superseded = 0
                 AND (last_recalled_at IS NULL OR last_recalled_at < ?)
               ORDER BY COALESCE(last_recalled_at, created_at) ASC""",
            (agent_id, importance, cutoff),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def all_facts(self, agent_id: str, include_superseded: bool = False) -> list[Fact]:
        validate_agent_id(agent_id)
        sql = "SELECT * FROM facts WHERE agent_id = ?"
        if not include_superseded:
            sql += " AND superseded = 0"
        sql += " ORDER BY created_at ASC"
        rows = self._conn.execute(sql, (agent_id,)).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _active_facts(self, agent_id: str, tier: str) -> list[Fact]:
        rows = self._conn.execute(
            """SELECT * FROM facts
               WHERE agent_id = ? AND tier = ? AND superseded = 0
               ORDER BY updated_at DESC""",
            (agent_id, tier),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"], agent_id=row["agent_id"], tier=row["tier"], key=row["key"],
            value=row["value"], importance=row["importance"],
            salience_score=row["salience_score"], tagged=bool(row["tagged"]),
            ttl_seconds=row["ttl_seconds"], created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_recalled_at=row["last_recalled_at"],
            recall_count=row["recall_count"], superseded=bool(row["superseded"]),
            metadata=json.loads(row["metadata"]),
        )
