"""
Basic Python usage of plug-play-memory — no MCP server required.

Useful when you want to embed agent memory directly in your Python app
instead of going through an MCP client.
"""

from plug_play_memory import (
    MemoryStore, PolicyEngine, EventEmitter, Judge, Consolidator,
)


def main():
    # 1. Initialize the system
    store = MemoryStore("my_agent_memory.db")
    policy = PolicyEngine()  # loads all built-in packs automatically
    judge = Judge()  # rules + heuristics + salience (no LLM required)
    consolidator = Consolidator(store)

    # 2. Bind an agent to a policy pack
    agent_id = "my-coder"
    policy.set_agent_type(agent_id, "coding")

    # 3. Create an emitter for this agent
    emitter = EventEmitter(
        store=store, policy=policy, judge=judge,
        agent_id=agent_id, agent_type="coding",
    )

    # 4. Emit events — the judge + policy + storage handle the rest

    # Decisions are saved (high importance, archival tier)
    emitter.decision_made(
        topic="auth",
        choice="use JWT",
        reason="simpler than sessions, no server-side state",
    )

    # Routine file edits are discarded (small, not memorable)
    emitter.file_edited(
        path="src/utils.py",
        diff_summary="fixed a typo in a comment",
        lines_changed=2,
    )

    # Big refactors are saved (heuristic escalation)
    emitter.file_edited(
        path="src/auth/middleware.py",
        diff_summary="refactored entire auth module to extract RateLimiter class",
        lines_changed=340,
    )

    # Passing tests are discarded
    emitter.test_run(suite="tests/", passed=12, failed=0)

    # Failing tests are saved as errors
    emitter.test_run(
        suite="tests/auth/",
        passed=11, failed=1,
        failures=["test_rate_limit_edge_case: expected 429, got 200"],
    )

    # Migration failures are saved (high importance)
    emitter.migration_run(
        name="0142_add_rate_limit_table",
        success=False,
        error="column already exists",
    )

    # Successful deploys are saved to recall tier
    emitter.deployed(
        target="staging", version="v1.2.3",
        success=True, notes="rate limiting live on /api/*",
    )

    # 5. Inspect the memory state
    print("\n=== CORE MEMORY (always injected) ===")
    for f in store.get_core(agent_id):
        print(f"  • [{f.importance.upper()}] {f.key}: {f.value}")

    print("\n=== RECALL TIER (recent context) ===")
    for f in store.recall_recent(agent_id, limit=10):
        print(f"  • [{f.importance.upper()}] {f.value}")

    print("\n=== ARCHIVAL TIER (long-term searchable) ===")
    archival = [f for f in store.all_facts(agent_id) if f.tier == "archival"]
    for f in archival:
        tag = " [TAGGED]" if f.tagged else ""
        print(f"  • [{f.importance.upper()}] (salience={f.salience_score:.2f}) {f.value}{tag}")

    # 6. Semantic recall — find facts about a topic
    print("\n=== RECALL: 'auth rate limiting' ===")
    results = store.recall_semantic(agent_id, "auth rate limiting", k=3)
    for f in results:
        print(f"  → {f.value}")
        # Reinforce recalled facts (spacing effect)
        consolidator.reinforce_on_recall(f.id)

    # 7. Run consolidation (the brain's "sleep" phase)
    print("\n=== CONSOLIDATION SWEEP ===")
    c_stats = consolidator.consolidate_tagged(agent_id)
    print(f"  Promoted: {c_stats['promoted']}, Decayed: {c_stats['decayed']}, "
          f"Forgotten: {c_stats['forgotten']}")

    f_stats = consolidator.forget_unused(agent_id)
    print(f"  Forgotten (low): {f_stats['forgotten_low']}, "
          f"Decayed (normal): {f_stats['decayed_normal']}")

    # 8. Audit log + coverage manifest
    print("\n=== COVERAGE MANIFEST ===")
    manifest = store.audit.coverage_manifest(agent_id)
    print(f"  Total permission checks: {manifest['total_permission_checks']}")
    for action, counts in manifest["counts_by_action"].items():
        print(f"  {action}: allowed={counts['allowed']}, denied={counts['denied']}")

    print("\nDone. Memory file saved to: my_agent_memory.db")


if __name__ == "__main__":
    main()
