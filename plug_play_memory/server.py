"""
MCP server for plug-play-memory — security-hardened.

Changes from v0.1:
  - Size limits on inputs/outputs/raw (H1 fix) — 100KB max per event
  - Cap on recall/audit_recent limit parameter (H2 fix)
  - Rate limiting: 100 events/min per agent_id (H3 fix)
  - Removed dead "all" scope check (L4 fix)
  - agent_type passed as parameter to policy.check (M5 fix)
  - Audit chain verification tool added (C4 fix)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import AuthConfig, AuthContext, AuthError, ALL_SCOPES
from .consolidation import Consolidator
from .emitter import EventEmitter
from .judge import Judge
from .policy import PolicyEngine
from .storage import MemoryStore


# ─── Security limits (H1, H2, H3 fixes) ────────────────────────────────────

MAX_EVENT_SIZE = 100 * 1024          # 100 KB total per event (H1)
MAX_FIELD_SIZE = 10 * 1024           # 10 KB per individual field (H1)
MAX_RECALL_LIMIT = 1000              # cap on recall/audit_recent limit (H2)
MAX_QUERY_LENGTH = 1000              # cap on recall query length (H2)

RATE_LIMIT_WINDOW = 60               # seconds (H3)
RATE_LIMIT_MAX = 100                 # max events per agent per window (H3)


class RateLimiter:
    """Simple in-memory rate limiter, per agent_id (H3 fix)."""

    def __init__(self, max_calls: int = RATE_LIMIT_MAX, window_sec: int = RATE_LIMIT_WINDOW):
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._calls: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, agent_id: str) -> bool:
        """Returns True if call is allowed, False if rate-limited."""
        now = time.time()
        with self._lock:
            recent = [t for t in self._calls[agent_id] if now - t < self.window_sec]
            if len(recent) >= self.max_calls:
                self._calls[agent_id] = recent
                return False
            recent.append(now)
            self._calls[agent_id] = recent
            return True


# ─── Helpers ────────────────────────────────────────────────────────────────

def _require_scope(ctx: AuthContext, scope: str) -> None:
    """Raise PermissionError if ctx lacks the required scope (L4 fix: removed 'all' check)."""
    if scope not in ctx.scopes:
        raise PermissionError(
            f"missing required scope: {scope} (have: {ctx.scopes})"
        )


def _check_event_size(action: str, inputs: dict, outputs: dict, raw: str) -> str | None:
    """Returns error message if event exceeds size limits, else None (H1 fix)."""
    if len(str(action)) > MAX_FIELD_SIZE:
        return f"action exceeds {MAX_FIELD_SIZE} bytes"
    if len(str(raw)) > MAX_FIELD_SIZE:
        return f"raw exceeds {MAX_FIELD_SIZE} bytes"
    if len(json.dumps(inputs, default=str)) > MAX_FIELD_SIZE:
        return f"inputs exceed {MAX_FIELD_SIZE} bytes"
    if len(json.dumps(outputs, default=str)) > MAX_FIELD_SIZE:
        return f"outputs exceed {MAX_FIELD_SIZE} bytes"
    total = (len(str(action)) + len(str(raw)) +
             len(json.dumps(inputs, default=str)) +
             len(json.dumps(outputs, default=str)))
    if total > MAX_EVENT_SIZE:
        return f"total event size {total} exceeds {MAX_EVENT_SIZE} bytes"
    return None


# ─── Server builder ─────────────────────────────────────────────────────────

def build_server(
    db_path: str,
    policy_dir: str | None = None,
    custom_policy_dir: str | None = None,
    auth_config_path: str | None = None,
    encryption_key: str | None = None,
    audit_signing_key: str | None = None,
) -> FastMCP:
    """Build the MCP server with all tools registered."""

    store = MemoryStore(db_path, encryption_key=encryption_key)
    policy = PolicyEngine(custom_dir=custom_policy_dir)
    judge = Judge()
    consolidator = Consolidator(store)
    auth_config = AuthConfig(auth_config_path)
    rate_limiter = RateLimiter()

    if auth_config.default_agent_type:
        try:
            policy.set_agent_type(auth_config.default_agent_id,
                                  auth_config.default_agent_type)
        except Exception:
            pass  # invalid default — will be caught on first request

    server = FastMCP("plug-play-memory")

    def get_auth() -> AuthContext:
        return auth_config.authenticate(token=None)

    def get_emitter(ctx: AuthContext) -> EventEmitter:
        return EventEmitter(
            store=store, policy=policy, judge=judge,
            agent_id=ctx.agent_id, agent_type=ctx.agent_type,
        )

    # ─── TOOL: remember_event (with H1 size limits + H3 rate limiting) ──────
    @server.tool(description=(
        "Record a meaningful event for the current agent. The system judges "
        "whether the event is worth saving, checks policy, deduplicates, and "
        "writes to memory if appropriate.\n\n"
        "Size limits: 100KB total per event, 10KB per field.\n"
        "Rate limit: 100 events/min per agent."
    ))
    def remember_event(
        action: str,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        outcome: str = "success",
        raw: str = "",
    ) -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "memory:write")

        # H1: size check
        err = _check_event_size(action, inputs or {}, outputs or {}, raw)
        if err:
            return {"saved": False, "reason": f"size limit: {err}"}

        # H3: rate limit
        if not rate_limiter.check(ctx.agent_id):
            return {"saved": False, "reason": "rate limited (100/min)"}

        policy.set_agent_type(ctx.agent_id, ctx.agent_type)
        emitter = get_emitter(ctx)
        fact = emitter.emit(
            action=action,
            inputs=inputs or {},
            outputs=outputs or {},
            outcome=outcome,
            raw=raw,
        )
        if fact is None:
            return {
                "saved": False,
                "reason": "discarded by judge or blocked by policy or deduplicated",
            }
        return {
            "saved": True,
            "fact_id": fact.id,
            "tier": fact.tier,
            "key": fact.key,
            "importance": fact.importance,
            "salience_score": round(fact.salience_score, 3),
            "tagged": fact.tagged,
            "summary": fact.value,
        }

    # ─── TOOL: recall (H2: capped limit + query length) ─────────────────────
    @server.tool(description=(
        "Retrieve relevant memory for the current agent.\n\n"
        "  tier='core'    → always-injected facts (small, fast)\n"
        "  tier='recall'  → most recent conversation-style memories\n"
        "  tier='archival' → semantic search over long-term memory (default)\n\n"
        "Limit capped at 1000. Query length capped at 1000 chars."
    ))
    def recall(
        query: str = "",
        tier: str = "archival",
        limit: int = 5,
    ) -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "memory:read")
        policy.set_agent_type(ctx.agent_id, ctx.agent_type)

        # H2: cap limit and query length
        limit = max(1, min(limit, MAX_RECALL_LIMIT))
        query = (query or "")[:MAX_QUERY_LENGTH]

        core = [{"id": f.id, "key": f.key, "value": f.value,
                 "importance": f.importance}
                for f in store.get_core(ctx.agent_id)]

        if tier == "core":
            results = []
        elif tier == "recall":
            results = [{"id": f.id, "key": f.key, "value": f.value,
                        "importance": f.importance,
                        "created_at": f.created_at}
                       for f in store.recall_recent(ctx.agent_id, limit=limit)]
        else:
            facts = store.recall_semantic(ctx.agent_id, query, k=limit)
            for f in facts:
                consolidator.reinforce_on_recall(f.id)
            results = [{"id": f.id, "key": f.key, "value": f.value,
                        "importance": f.importance,
                        "salience_score": round(f.salience_score, 3),
                        "recall_count": f.recall_count}
                       for f in facts]

        store.audit.log(ctx.agent_id, "recall",
                        {"tier": tier, "query": query, "limit": limit,
                         "results_count": len(results)})
        return {"core": core, "results": results, "tier": tier}

    @server.tool(description=(
        "Check whether an action is allowed for the current agent BEFORE "
        "executing it. This is the hard enforcement gate."
    ))
    def check_permission(
        action: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "permission:check")
        # M5 fix: pass agent_type explicitly
        decision = policy.check(
            ctx.agent_id, action, context or {},
            agent_type=ctx.agent_type,
        )
        store.audit.log(
            ctx.agent_id, "permission_check",
            {"action": action, "context": context or {},
             "backend": "layered_yaml", "reason": decision.reason,
             "layers": decision.layers_evaluated},
            allowed=decision.allowed,
        )
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "layers_evaluated": decision.layers_evaluated,
        }

    @server.tool(description=(
        "Soft-delete a specific fact by id. Hard delete requires admin scope."
    ))
    def forget(fact_id: str, hard: bool = False) -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "memory:delete")
        if hard:
            _require_scope(ctx, "admin:consolidate")
        removed = store.forget(ctx.agent_id, fact_id, hard=hard)
        return {"removed": removed, "hard": hard}

    @server.tool(description=(
        "Return the most recent audit log entries for the current agent."
    ))
    def audit_recent(limit: int = 20) -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "admin:audit")
        limit = max(1, min(limit, MAX_RECALL_LIMIT))  # H2
        entries = store.audit.recent(ctx.agent_id, limit=limit)
        return {
            "agent_id": ctx.agent_id,
            "entries": [
                {"id": e.id, "op_type": e.op_type,
                 "detail": e.detail, "allowed": e.allowed,
                 "timestamp": e.timestamp}
                for e in entries
            ],
            "count": len(entries),
        }

    @server.tool(description=(
        "Return the coverage manifest for the current agent: which actions "
        "have been checked at least once, and the allow/deny split. This is "
        "deliberately about what HAS happened, not a safety claim."
    ))
    def coverage_manifest() -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "admin:audit")
        return store.audit.coverage_manifest(ctx.agent_id)

    @server.tool(description=(
        "Run the consolidation sweep: promote tagged facts that have been "
        "recalled, decay the rest, prune low-importance facts not recalled "
        "in 30 days. This is the brain's 'sleep' phase."
    ))
    def consolidate() -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "admin:consolidate")
        c_stats = consolidator.consolidate_tagged(ctx.agent_id)
        f_stats = consolidator.forget_unused(ctx.agent_id)
        return {"consolidation": c_stats, "forgetting": f_stats}


    # v0.3: rejudge_events + event_log_stats

    @server.tool(description="Re-evaluate ALL events in event_log with current judge rules. Events previously discarded can be surfaced. Zero data loss.")
    def rejudge_events() -> dict:
        ctx = get_auth()
        _require_scope(ctx, "admin:consolidate")
        stats = consolidator.rejudge(ctx.agent_id, judge)
        return {"judge_version": judge.VERSION, "stats": stats}

    @server.tool(description="Get stats about the event_log: total events, surfaced, discarded (rejudgable).")
    def event_log_stats() -> dict:
        ctx = get_auth()
        _require_scope(ctx, "admin:audit")
        return store.get_event_log_stats(ctx.agent_id)

    @server.tool(description=(
        "List all policy layers currently loaded for the system, plus the "
        "action whitelist for this agent's type."
    ))
    def list_policies() -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "admin:audit")
        return {
            "agent_id": ctx.agent_id,
            "agent_type": ctx.agent_type,
            "layers": policy.list_layers(),
            "allowed_actions": sorted(policy.list_allowed_actions(ctx.agent_type)),
        }

    @server.tool(description=(
        "Verify the integrity of the audit log chain (C4 fix). Returns "
        "counts of valid/invalid signatures and any chain breaks.\n\n"
        "Call this periodically to detect tampering. If invalid_signatures "
        "or chain_broken_at is non-empty, the audit log has been tampered with."
    ))
    def verify_audit_integrity() -> dict[str, Any]:
        ctx = get_auth()
        _require_scope(ctx, "admin:audit")
        return store.audit.verify_chain()

    return server


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Run the plug-play-memory MCP server (stdio transport).",
    )
    parser.add_argument("--db", default="memory.db",
                        help="Path to the SQLite memory file.")
    parser.add_argument("--policies", default=None,
                        help="Path to custom policy directory.")
    parser.add_argument("--config", default=None,
                        help="Path to auth config YAML.")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--agent-type", default=None)
    parser.add_argument("--encryption-key", default=None,
                        help="SQLCipher encryption key (recommended for production).")
    parser.add_argument("--audit-key", default=None,
                        help="HMAC signing key for audit log (recommended for production).")
    args = parser.parse_args()

    if args.agent_id:
        os.environ["AGENTMEMORY_AGENT_ID"] = args.agent_id
    if args.agent_type:
        os.environ["AGENTMEMORY_AGENT_TYPE"] = args.agent_type

    server = build_server(
        db_path=args.db,
        custom_policy_dir=args.policies,
        auth_config_path=args.config,
        encryption_key=args.encryption_key or os.environ.get("AGENTMEMORY_DB_KEY"),
        audit_signing_key=args.audit_key or os.environ.get("AGENTMEMORY_AUDIT_KEY"),
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    run()
