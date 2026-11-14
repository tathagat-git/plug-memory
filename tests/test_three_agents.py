"""
End-to-end test: 3 agents with different use cases.

Agent 1: "coder-1"  — coding agent (file edits, tests, deploys, errors)
Agent 2: "priya"    — study agent (JEE prep tutor)
Agent 3: "finance-1" — financial agent (payments, budgets)

Each agent goes through:
  1. Realistic event sequence over multiple "sessions"
  2. Memory writes through judge + policy gate
  3. Recall queries that reinforce useful memories
  4. Sleep consolidation sweep
  5. Active forgetting sweep
  6. Final memory state inspection
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from plug_play_memory import (
    MemoryStore, PolicyEngine, EventEmitter, Judge, Consolidator,
)


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def show_memory(store: MemoryStore, agent_id: str, label: str) -> None:
    print(f"\n  Memory for {label} ({agent_id}):")
    core = store.get_core(agent_id)
    recall = store.recall_recent(agent_id, limit=20)
    archival = [f for f in store.all_facts(agent_id) if f.tier == "archival"]

    print(f"  CORE ({len(core)} facts — always injected):")
    for f in core:
        tag = " [TAGGED]" if f.tagged else ""
        print(f"    • [{f.importance.upper()}] {f.key or '-'}: {f.value}{tag}")

    print(f"  RECALL ({len(recall)} facts — recent context):")
    for f in recall[:10]:
        tag = " [TAGGED]" if f.tagged else ""
        print(f"    • [{f.importance.upper()}] {f.value}{tag}")

    print(f"  ARCHIVAL ({len(archival)} facts — long-term searchable):")
    for f in archival[:15]:
        tag = " [TAGGED]" if f.tagged else ""
        print(f"    • [{f.importance.upper()}] (salience={f.salience_score:.2f}, recalled={f.recall_count}) {f.value}{tag}")


# =====================================================================
# AGENT 1: CODING AGENT
# =====================================================================

def run_coding_agent(store: MemoryStore, policy: PolicyEngine, judge: Judge) -> None:
    banner("AGENT 1: CODING AGENT ('coder-1')")
    policy.set_agent_type("coder-1", "coding")
    emitter = EventEmitter(store, policy, judge, agent_id="coder-1", agent_type="coding")

    section("Session 1: Planning a refactor")
    # Decision — should be saved as high-importance archival
    emitter.decision_made(
        topic="auth module",
        choice="extract rate limiting into middleware",
        reason="current implementation duplicates logic across 3 routes",
    )
    # Small edit — should be discarded (routine)
    emitter.file_edited(
        path="src/auth/middleware.py",
        diff_summary="added a comment",
        lines_changed=2,
    )
    # Big edit — heuristic should escalate to decision
    emitter.file_edited(
        path="src/auth/middleware.py",
        diff_summary="refactored entire auth module to extract RateLimiter class with sliding window",
        lines_changed=340,
    )
    # Routine test pass — should be discarded
    emitter.test_run(suite="tests/auth/", passed=12, failed=0)
    # Test failure — should be saved as error
    emitter.test_run(
        suite="tests/auth/",
        passed=11, failed=1,
        failures=["test_rate_limit_edge_case: expected 429, got 200"],
    )

    section("Session 2: Migration failure (recurring!)")
    # Migration failure — should be saved with high importance
    emitter.migration_run(
        name="0142_add_rate_limit_table",
        success=False,
        error="column already exists",
    )
    # Same migration fails AGAIN — heuristic should flag recurring
    emitter.migration_run(
        name="0142_add_rate_limit_table",
        success=False,
        error="column already exists — recurring failure",
    )

    section("Session 3: Successful deploy")
    emitter.git_pushed(
        branch="main", sha="abc1234",
        repo="acme/api", message="ship rate limiting"
    )
    emitter.deployed(
        target="staging", version="v1.2.3",
        success=True, notes="rate limiting live on /api/*"
    )

    section("Recall queries (reinforces matching facts)")
    results = store.recall_semantic("coder-1", "auth rate limiting", k=3)
    print(f"  Recall 'auth rate limiting' → {len(results)} hits")
    for f in results:
        print(f"    → {f.value}")
    # Reinforce: call consolidate to update recall_count
    consolidator = Consolidator(store)
    for f in results:
        consolidator.reinforce_on_recall(f.id)

    results = store.recall_semantic("coder-1", "migration error", k=3)
    print(f"  Recall 'migration error' → {len(results)} hits")
    for f in results:
        consolidator.reinforce_on_recall(f.id)

    # Attempted operation that should be policy-blocked: modify_ci_config
    section("Policy test: try to modify CI config (should be DENIED)")
    decision = policy.check("coder-1", "modify_ci_config", {})
    print(f"  modify_ci_config → allowed={decision.allowed}, reason='{decision.reason}'")

    show_memory(store, "coder-1", "Coding Agent")


# =====================================================================
# AGENT 2: STUDY AGENT
# =====================================================================

def run_study_agent(store: MemoryStore, policy: PolicyEngine, judge: Judge) -> None:
    banner("AGENT 2: STUDY AGENT ('priya' — JEE prep tutor)")
    policy.set_agent_type("priya", "study")
    emitter = EventEmitter(store, policy, judge, agent_id="priya", agent_type="study")

    section("Session 1: Getting to know the student")
    emitter.fact_observed("exam_target", "JEE Advanced 2026",
                          raw="Student is preparing for JEE Advanced 2026")
    emitter.fact_observed("preferred_style", "step-by-step derivations",
                          raw="prefers step-by-step derivations over intuitions")
    emitter.fact_observed("weak_area", "thermodynamics",
                          raw="self-reported weak area: thermodynamics")

    section("Session 1: Studying thermodynamics")
    # Question asked — should be discarded
    emitter.question_asked(
        topic="thermodynamics",
        question="Why does entropy always increase in isolated systems?",
        difficulty="hard",
    )
    # Wrong answer with misconception — should be saved as high-importance error
    emitter.answer_attempted(
        topic="thermodynamics",
        correct=False,
        misconception="confuses entropy direction with heat flow direction",
        question_id="q-thermo-1",
        student_answer="because heat flows from hot to cold, increasing disorder",
    )
    # Knowledge gap — should be saved as high-importance
    emitter.knowledge_gap_found(
        topic="second_law_of_thermodynamics",
        gap="Student confuses entropy increase with heat flow direction",
        severity="high",
    )
    # Concept mastered after remediation — should be saved as recall achievement
    emitter.concept_mastered(
        topic="entropy", mastery_level=0.85, attempts=3,
    )

    section("Session 2: Goal setting")
    emitter.goal_set(
        description="Finish thermodynamics chapter by Sunday",
        deadline="2026-11-03", priority="high",
    )

    section("Recall queries (reinforces)")
    results = store.recall_semantic("priya", "thermodynamics", k=3)
    print(f"  Recall 'thermodynamics' → {len(results)} hits")
    consolidator = Consolidator(store)
    for f in results:
        consolidator.reinforce_on_recall(f.id)
        print(f"    → {f.value}")

    # Try to delete memory — should be policy-blocked
    section("Policy test: try to delete_memory (should be DENIED for study agents)")
    decision = policy.check("priya", "delete_memory", {})
    print(f"  delete_memory → allowed={decision.allowed}, reason='{decision.reason}'")

    # Try to execute_code — should be policy-blocked
    decision = policy.check("priya", "execute_code", {})
    print(f"  execute_code → allowed={decision.allowed}, reason='{decision.reason}'")

    show_memory(store, "priya", "Study Agent")


# =====================================================================
# AGENT 3: FINANCIAL AGENT
# =====================================================================

def run_financial_agent(store: MemoryStore, policy: PolicyEngine, judge: Judge) -> None:
    banner("AGENT 3: FINANCIAL AGENT ('finance-1')")
    policy.set_agent_type("finance-1", "financial")
    emitter = EventEmitter(store, policy, judge, agent_id="finance-1", agent_type="financial")

    section("Session 1: User profile + budget")
    emitter.fact_observed("monthly_income", "85000",
                          raw="User monthly income is ₹85,000")
    emitter.fact_observed("savings_goal", "20% of income",
                          raw="User wants to save 20% of income")
    emitter.set_budget_limit(
        category="food", limit=8000, period="monthly",
    )

    section("Session 2: Payments (testing tiered authorization)")
    # Small payment — should be allowed with approved_by_user
    decision = policy.check("finance-1", "make_payment", {
        "approved_by_user": True, "amount": 500,
    })
    print(f"  make_payment ₹500 with approval → allowed={decision.allowed}")
    if decision.allowed:
        emitter.make_payment(
            amount=500, recipient="swiggy", success=True,
            notes="dinner",
        )

    # Large payment with second factor — should be allowed
    decision = policy.check("finance-1", "make_payment", {
        "approved_by_user": True, "second_factor_verified": True, "amount": 5000,
    })
    print(f"  make_payment ₹5000 with approval+2FA → allowed={decision.allowed}")
    if decision.allowed:
        emitter.make_payment(
            amount=5000, recipient="landlord", success=True,
            notes="rent",
        )

    # Very large payment — should be DENIED (above 10000 default)
    decision = policy.check("finance-1", "make_payment", {
        "approved_by_user": True, "second_factor_verified": True, "amount": 50000,
    })
    print(f"  make_payment ₹50,000 with approval+2FA → allowed={decision.allowed} (above cap)")

    # Without approval — should be DENIED
    decision = policy.check("finance-1", "make_payment", {"amount": 500})
    print(f"  make_payment ₹500 WITHOUT approval → allowed={decision.allowed}")

    section("Session 3: Error")
    emitter.error_encountered(
        message="Bank API timeout — could not fetch balance",
        context={"bank": "hdfc", "endpoint": "/v2/balance"},
    )

    section("Recall queries (reinforces)")
    results = store.recall_semantic("finance-1", "budget income", k=3)
    print(f"  Recall 'budget income' → {len(results)} hits")
    consolidator = Consolidator(store)
    for f in results:
        consolidator.reinforce_on_recall(f.id)
        print(f"    → {f.value}")

    # Try to execute_trade — should be policy-blocked
    section("Policy test: try to execute_trade (should be DENIED)")
    decision = policy.check("finance-1", "execute_trade", {})
    print(f"  execute_trade → allowed={decision.allowed}, reason='{decision.reason}'")

    # Try to access_credentials — should be policy-blocked by _base
    decision = policy.check("finance-1", "access_credentials", {})
    print(f"  access_credentials → allowed={decision.allowed}, reason='{decision.reason}'")

    show_memory(store, "finance-1", "Financial Agent")


# =====================================================================
# CONSOLIDATION PHASE
# =====================================================================

def run_consolidation(store: MemoryStore) -> None:
    banner("CONSOLIDATION PHASE (sleep + active forgetting)")
    consolidator = Consolidator(store)

    section("Sleep consolidation — promote reinforced tagged facts, decay the rest")
    for agent_id in ["coder-1", "priya", "finance-1"]:
        stats = consolidator.consolidate_tagged(agent_id)
        print(f"  {agent_id:12s}: promoted={stats['promoted']}, "
              f"decayed={stats['decayed']}, forgotten={stats['forgotten']}")

    section("Active forgetting — prune unused low-importance facts")
    for agent_id in ["coder-1", "priya", "finance-1"]:
        stats = consolidator.forget_unused(agent_id)
        print(f"  {agent_id:12s}: forgotten_low={stats['forgotten_low']}, "
              f"decayed_normal={stats['decayed_normal']}")


# =====================================================================
# FINAL AUDIT + COVERAGE REPORT
# =====================================================================

def show_audit_and_coverage(store: MemoryStore) -> None:
    banner("AUDIT LOG + COVERAGE MANIFEST")

    section("Recent audit entries (across all agents)")
    entries = store.audit.recent_all(limit=40)
    print(f"  Total entries shown: {len(entries)} (most recent first)")
    print(f"  {'ID':>4}  {'AGENT':<12} {'OP':<24} {'ALLOWED':<8} DETAIL")
    for e in entries[:40]:
        detail_str = str(e.detail)[:60]
        allowed_str = (
            "yes" if e.allowed else ("no" if e.allowed is False else "-")
        )
        print(f"  {e.id:>4}  {e.agent_id:<12} {e.op_type:<24} {allowed_str:<8} {detail_str}")

    section("Coverage manifest per agent")
    for agent_id in ["coder-1", "priya", "finance-1"]:
        manifest = store.audit.coverage_manifest(agent_id)
        print(f"\n  {agent_id}:")
        print(f"    total permission checks: {manifest['total_permission_checks']}")
        for action, counts in manifest["counts_by_action"].items():
            print(f"    {action:24s}  allowed={counts['allowed']:3d}  denied={counts['denied']:3d}")


# =====================================================================
# MAIN
# =====================================================================

def main() -> None:
    banner("plug-play-memory v1 — End-to-end test with 3 agents")

    db_path = tempfile.mktemp(suffix=".db")
    if os.path.exists(db_path):
        os.remove(db_path)

    store = MemoryStore(db_path)
    policy = PolicyEngine()
    judge = Judge()
    print(f"\n  Loaded policy layers: {policy.list_layers()}")

    try:
        run_coding_agent(store, policy, judge)
        run_study_agent(store, policy, judge)
        run_financial_agent(store, policy, judge)
        run_consolidation(store)
        show_audit_and_coverage(store)
    finally:
        store.close()
        if os.path.exists(db_path):
            os.remove(db_path)

    banner("TEST COMPLETE")


if __name__ == "__main__":
    main()
