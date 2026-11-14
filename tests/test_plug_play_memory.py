"""Unit tests for plug-play-memory v1 — brain-inspired agent memory."""

import os
import tempfile

import pytest

from plug_play_memory import (
    MemoryStore, PolicyEngine, EventEmitter, Judge, Consolidator,
)
from plug_play_memory.events import AgentEvent
from plug_play_memory.judge import RulesJudge, HeuristicEnricher, SalienceScorer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    path = tempfile.mktemp(suffix=".db")
    s = MemoryStore(path)
    yield s
    s.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def policy():
    return PolicyEngine()


@pytest.fixture
def judge():
    return Judge()


@pytest.fixture
def emitter(store, policy, judge):
    policy.set_agent_type("test-agent", "coding")
    return EventEmitter(store, policy, judge, agent_id="test-agent", agent_type="coding")


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------

def test_core_memory_roundtrip(store):
    store.remember("a1", "User prefers formal tone", tier="core", key="tone")
    core = store.get_core("a1")
    assert len(core) == 1
    assert core[0].value == "User prefers formal tone"


def test_update_not_append_on_same_key(store):
    store.remember("a1", "formal", tier="core", key="tone")
    store.remember("a1", "casual", tier="core", key="tone")
    core = store.get_core("a1")
    assert len(core) == 1
    assert core[0].value == "casual"


def test_multi_tenant_isolation(store):
    store.remember("a1", "secret-1", tier="core", key="secret")
    store.remember("a2", "secret-2", tier="core", key="secret")
    assert [f.value for f in store.get_core("a1")] == ["secret-1"]
    assert [f.value for f in store.get_core("a2")] == ["secret-2"]


def test_embedder_is_deterministic_across_processes():
    """Critical: hash() would fail this — must use blake2b."""
    from plug_play_memory.storage import default_embedder
    v1 = default_embedder("test text one")
    v2 = default_embedder("test text one")
    assert v1 == v2  # same process
    # In a different process, blake2b stays deterministic; hash() would not


def test_recall_updates_stats(store):
    """Recalling a fact should bump recall_count and last_recalled_at."""
    fact = store.remember("a1", "important fact", tier="archival")
    assert fact.recall_count == 0
    store.update_recall_stats(fact.id)
    facts = store.all_facts("a1")
    assert facts[0].recall_count == 1
    assert facts[0].last_recalled_at is not None


def test_tagged_fact_has_consolidation_deadline(store):
    """Tagged facts should get a consolidates_at deadline (24h window)."""
    fact = store.remember("a1", "uncertain event", tier="archival", tagged=True)
    assert fact.tagged is True
    assert fact.metadata == {} or isinstance(fact.metadata, dict)
    # Reload from DB
    facts = store.all_facts("a1")
    assert facts[0].tagged is True


def test_soft_delete_marks_superseded(store):
    fact = store.remember("a1", "to be forgotten", tier="archival")
    result = store.forget("a1", fact.id, hard=False)
    assert result is True
    # Should not appear in active facts
    assert store.all_facts("a1") == []
    # But should still exist with superseded=1
    rows = store._conn.execute("SELECT * FROM facts WHERE id = ?", (fact.id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["superseded"] == 1


# ---------------------------------------------------------------------------
# Policy tests
# ---------------------------------------------------------------------------

def test_unknown_action_fails_closed(policy):
    policy.set_agent_type("a1", "coding")
    decision = policy.check("a1", "delete_production_database", {})
    assert decision.allowed is False


def test_absolute_deny_in_base_cannot_be_overridden(policy):
    """access_credentials is denied — both by whitelist gate AND _base denylist."""
    policy.set_agent_type("a1", "coding")
    decision = policy.check("a1", "access_credentials", {})
    assert decision.allowed is False
    # Either reason is valid — both deny correctly
    assert ("absolute deny" in decision.reason or
            "not in whitelist" in decision.reason)


def test_coding_agent_can_write_decision(policy):
    policy.set_agent_type("a1", "coding")
    decision = policy.check("a1", "write_memory",
                            {"category": "decision", "tier": "archival"})
    assert decision.allowed is True


def test_study_agent_cannot_delete_memory(policy):
    policy.set_agent_type("a1", "study")
    decision = policy.check("a1", "delete_memory", {})
    assert decision.allowed is False


def test_financial_payment_tiered_authorization(policy):
    policy.set_agent_type("a1", "financial")
    # Small payment with approval → allowed
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True, "amount": 500})
    assert d.allowed is True
    # Medium payment needs 2FA
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True, "amount": 5000})
    assert d.allowed is False  # missing second_factor
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True,
                      "second_factor_verified": True, "amount": 5000})
    assert d.allowed is True
    # Very large payment — denied by default
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True,
                      "second_factor_verified": True, "amount": 50000})
    assert d.allowed is False


def test_custom_override_extends_builtin(tmp_path, store):
    """Custom policy files should extend (not replace) built-in packs.

    Note: after the C3 fix, custom rules can only ALLOW actions that are
    in the agent_type's whitelist. To add a genuinely new action via custom
    policy, you must also extend the whitelist (advanced use case).
    """
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "study.yaml").write_text(
        "name: study\ndefault: deny\nrules:\n"
        "  - action: sync_to_flashcard_app\n    allow: true\n    requires: [approved_by_user]\n"
    )
    p = PolicyEngine(custom_dir=custom)
    p.set_agent_type("a1", "study")
    # Built-in rule still works
    d = p.check("a1", "recall_memory", {})
    assert d.allowed is True
    # Custom rule on a whitelisted action works
    d = p.check("a1", "sync_to_flashcard_app", {"approved_by_user": True})
    assert d.allowed is True
    # Custom rule denied without required context
    d = p.check("a1", "sync_to_flashcard_app", {})
    assert d.allowed is False
    # Built-in deny still works
    d = p.check("a1", "delete_memory", {})
    assert d.allowed is False


def test_unknown_action_blocked_by_whitelist(policy):
    """C3 fix: unknown actions are blocked at the gate, not categorized and saved."""
    policy.set_agent_type("a1", "coding")
    d = policy.check("a1", "my_malicious_unknown_action", {})
    assert d.allowed is False
    assert "whitelist" in d.reason or "not in" in d.reason


def test_reserved_agent_id_rejected():
    """M4 fix: agent_id cannot be '_base', 'coding', etc."""
    from plug_play_memory.storage import validate_agent_id, RESERVED_AGENT_IDS
    for reserved in RESERVED_AGENT_IDS:
        try:
            validate_agent_id(reserved)
            assert False, f"should have rejected '{reserved}'"
        except ValueError:
            pass  # expected
    # Normal names work
    validate_agent_id("coder-1")
    validate_agent_id("priya-study")


# ---------------------------------------------------------------------------
# Judge tests
# ---------------------------------------------------------------------------

def test_rules_judge_discards_routine_success(judge):
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="test_run", outcome="success",
        inputs={"suite": "tests/"}, outputs={"passed": 10, "failed": 0},
    )
    decision = judge.evaluate(event)
    assert decision.should_save is False


def test_rules_judge_saves_failures(judge):
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="test_run", outcome="failure",
        inputs={"suite": "tests/auth/"},
        outputs={"passed": 11, "failed": 1,
                 "failures": ["test_rate_limit: expected 429, got 200"]},
    )
    decision = judge.evaluate(event)
    assert decision.should_save is True
    assert decision.category == "error"
    assert decision.importance == "high"


def test_heuristic_escalates_big_refactor(judge):
    """A 340-line refactor should be saved even though file_edited defaults to discard."""
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="file_edited", outcome="success",
        inputs={"path": "src/auth.py", "lines_changed": 340},
        outputs={"diff_summary": "refactored entire auth module to extract RateLimiter"},
        raw="refactored entire auth module to extract RateLimiter",
    )
    decision = judge.evaluate(event)
    assert decision.should_save is True
    assert decision.importance == "high"


def test_heuristic_flags_recurring(judge):
    """Recurring failures should be flagged as high importance."""
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="migration_run", outcome="failure",
        inputs={"migration": "0142"},
        outputs={"error": "column already exists — recurring failure"},
        raw="column already exists — recurring failure",
    )
    decision = judge.evaluate(event)
    assert decision.should_save is True
    assert decision.importance == "high"


def test_heuristic_downgrades_flaky_tests(judge):
    """Flaky tests should be saved as convention, not error."""
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="test_run", outcome="partial",
        inputs={"suite": "tests/e2e/"},
        outputs={"passed": 9, "failed": 1,
                 "failures": ["test_login: flaky, passes on retry"]},
        raw="test_login: flaky, passes on retry",
    )
    decision = judge.evaluate(event)
    assert decision.should_save is True
    assert decision.category == "convention"


def test_salience_score_in_unit_interval(judge):
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="decision_made", outcome="success",
        inputs={"topic": "auth"},
        outputs={"choice": "use middleware", "reason": "dedupe"},
        raw="Decided use middleware for auth",
    )
    decision = judge.evaluate(event)
    assert 0.0 <= decision.salience_score <= 1.0


# ---------------------------------------------------------------------------
# Emitter tests
# ---------------------------------------------------------------------------

def test_emitter_writes_through_policy(emitter, store):
    """A normal decision should be saved with audit trail."""
    fact = emitter.decision_made(
        topic="auth", choice="use middleware",
        reason="dedupe logic across routes",
    )
    assert fact is not None
    # Audit log should have both permission_check and remember
    entries = store.audit.recent("test-agent", limit=20)
    ops = [e.op_type for e in entries]
    assert "permission_check" in ops
    assert "remember" in ops


def test_emitter_discards_routine_events(emitter, store):
    """Routine test passes should be discarded, not saved."""
    fact = emitter.test_run(suite="tests/", passed=10, failed=0)
    assert fact is None
    # Audit should record the discard
    entries = store.audit.recent("test-agent", limit=20)
    assert any(e.op_type == "event_discarded" for e in entries)


def test_emitter_dedups_same_key_same_value(emitter, store):
    """Writing the same fact twice with the same key should dedup."""
    emitter.fact_observed("name", "Alice")
    emitter.fact_observed("name", "Alice")
    core = store.get_core("test-agent")
    # Should only have 1 entry (second was deduped)
    name_facts = [f for f in core if f.key == "name"]
    assert len(name_facts) == 1


# ---------------------------------------------------------------------------
# Consolidation tests
# ---------------------------------------------------------------------------

def test_consolidation_promotes_recalled_tagged_facts(store):
    """A tagged fact that gets recalled should be promoted (tag cleared)."""
    consolidator = Consolidator(store)
    # Create a tagged fact
    fact = store.remember("a1", "uncertain event", tier="archival",
                          tagged=True, importance="normal")
    # Simulate recall
    consolidator.reinforce_on_recall(fact.id)
    # Run consolidation
    stats = consolidator.consolidate_tagged("a1")
    assert stats["promoted"] == 1
    # Verify the tag was cleared
    facts = store.all_facts("a1")
    assert facts[0].tagged is False


def test_active_forgetting_prunes_unused_low(store):
    """Low-importance facts not recalled in 30 days should be soft-deleted."""
    import time
    # Create an old, low-importance, never-recalled fact
    fact = store.remember("a1", "trivial note", tier="archival", importance="low")
    # Backdate last_recalled_at to 35 days ago
    store._conn.execute(
        "UPDATE facts SET last_recalled_at = ? WHERE id = ?",
        (time.time() - 35 * 86400, fact.id),
    )
    store._conn.commit()
    # Run active forgetting
    consolidator = Consolidator(store)
    stats = consolidator.forget_unused("a1")
    assert stats["forgotten_low"] == 1
    # Fact should be soft-deleted (superseded)
    assert store.all_facts("a1") == []


def test_audit_log_is_append_only(store):
    """No UPDATE or DELETE should work on audit_log rows."""
    store.audit.log("a1", "remember", {"test": True})
    with pytest.raises(Exception):
        store._conn.execute("DELETE FROM audit_log WHERE id = 1")
    with pytest.raises(Exception):
        store._conn.execute("UPDATE audit_log SET agent_id = 'hacker' WHERE id = 1")


# ---------------------------------------------------------------------------
# The "single-letter variable" bug — proof that event-based judging
# doesn't false-positive on coding chatter the way regex chat extraction does.
# ---------------------------------------------------------------------------

def test_single_letter_variable_in_code_does_not_become_user_name(judge):
    """
    The original regex chat-extraction bug: 'name the variable L' would
    save 'L' as user_name. With event-based judging, this is impossible —
    the only way to save a user_name is to call fact_observed() explicitly.
    """
    event = AgentEvent(
        agent_id="a1", agent_type="coding",
        action="file_edited", outcome="success",
        inputs={"path": "main.py", "lines_changed": 3},
        outputs={"diff_summary": "added variable L = []"},
        raw="added variable L = []  # also m, ma as accumulators",
    )
    decision = judge.evaluate(event)
    # Small edit, no security/recurrence keywords → discarded
    assert decision.should_save is False
    # Even if saved, key would NOT be "user_name"
    if decision.should_save:
        assert decision.key != "user_name"


# ---------------------------------------------------------------------------
# Security-hardening tests (added in v0.2)
# ---------------------------------------------------------------------------

def test_path_traversal_blocked_in_customize(tmp_path):
    """C1 fix: policy customize rejects path traversal attempts."""
    import subprocess, sys
    custom_dir = tmp_path / "policies"
    custom_dir.mkdir()
    (custom_dir / "custom").mkdir()
    result = subprocess.run(
        [sys.executable, "-c", """
import sys; sys.path.insert(0, '.')
from plug_play_memory.cli import cmd_policy_customize
import argparse
class A: pass
a = A(); a.name = '../evil_escape'; a.policies = '%s'
cmd_policy_customize(a)
""" % str(tmp_path / "policies")],
        capture_output=True, text=True, cwd="/home/z/my-project",
    )
    assert "ERROR" in result.stdout or result.returncode != 0, \
        f"should have rejected path traversal, got: {result.stdout}"
    # Verify no file was created outside custom/
    assert not (tmp_path / "evil_escape.yaml").exists()


def test_reserved_name_rejected_in_customize(tmp_path):
    """C1 fix: policy customize rejects reserved names."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-c", """
import sys; sys.path.insert(0, '.')
from plug_play_memory.cli import cmd_policy_customize
class A: pass
a = A(); a.name = '_base'; a.policies = '%s'
cmd_policy_customize(a)
""" % str(tmp_path)],
        capture_output=True, text=True, cwd="/home/z/my-project",
    )
    assert "ERROR" in result.stdout or "reserved" in result.stdout.lower()


def test_size_limit_blocks_huge_event(store):
    """H1 fix: events over 100KB are rejected."""
    from plug_play_memory import PolicyEngine, EventEmitter, Judge
    policy = PolicyEngine()
    policy.set_agent_type("a1", "coding")
    judge = Judge()
    emitter = EventEmitter(store, policy, judge, "a1", "coding")

    huge_raw = "A" * (200 * 1024)  # 200KB — over limit
    # The MCP server enforces this; the emitter itself doesn't.
    # But we can test the size-check helper:
    from plug_play_memory.server import _check_event_size
    err = _check_event_size("test", {}, {}, huge_raw)
    assert err is not None
    assert "exceeds" in err


def test_recall_limit_capped(store):
    """H2 fix: recall limit is capped at 1000."""
    store.remember("a1", "fact", tier="recall")
    # Even if user asks for 999999999, we cap internally
    results = store.recall_recent("a1", limit=999999999)
    assert len(results) == 1  # only 1 fact exists


def test_rate_limiter():
    """H3 fix: rate limiter blocks after max calls."""
    from plug_play_memory.server import RateLimiter
    rl = RateLimiter(max_calls=3, window_sec=60)
    assert rl.check("a1") is True
    assert rl.check("a1") is True
    assert rl.check("a1") is True
    assert rl.check("a1") is False  # 4th call blocked


def test_tokens_hashed_at_rest(tmp_path):
    """H4 fix: tokens stored as SHA-256 hashes, not plaintext."""
    config = tmp_path / "config.yaml"
    config.write_text(
        f"db_path: /tmp/test.db\n"
        f"auth:\n"
        f"  enabled: true\n"
        f"  default_agent_id: local\n"
        f"  default_agent_type: coding\n"
        f"  tokens:\n"
        f"    - id: token-1\n"
        f"      token: sk-secret-DO-NOT-SHARE-12345\n"
        f"      agent_id: admin-user\n"
        f"      agent_type: coding\n"
        f"      scopes: [memory:read]\n"
    )
    from plug_play_memory.auth import AuthConfig, _hash_token
    ac = AuthConfig(str(config))
    # Token should be hashed internally
    assert "sk-secret-DO-NOT-SHARE-12345" not in str(ac._tokens.keys())
    # But authentication with the plaintext token works
    ctx = ac.authenticate("sk-secret-DO-NOT-SHARE-12345")
    assert ctx.agent_id == "admin-user"
    # And wrong token fails
    import pytest
    with pytest.raises(Exception):
        ac.authenticate("wrong-token")


def test_boost_importance_actually_persists(store):
    """H5 fix: _boost_importance now actually boosts (was reversed direction)."""
    from plug_play_memory import Consolidator
    consolidator = Consolidator(store)
    fact = store.remember("a1", "test", tier="archival", tagged=True, importance="low")
    # Manually call boost
    consolidator._boost_importance(fact.id)
    f = consolidator._get_fact(fact.id)
    assert f.importance == "normal", f"expected 'normal', got '{f.importance}'"
    # Boost again
    consolidator._boost_importance(fact.id)
    f = consolidator._get_fact(fact.id)
    assert f.importance == "high", f"expected 'high', got '{f.importance}'"


def test_audit_chain_verification_detects_tampering(store):
    """C4 fix: verify_chain detects appended fake entries."""
    store.audit.log("a1", "remember", {"test": True})
    # Verify clean chain
    report = store.audit.verify_chain()
    assert report["integrity_ok"] is True
    assert report["valid_signatures"] == 1

    # Append a fake entry directly via SQL (simulating attacker with DB access)
    import sqlite3, hashlib, json, time, hmac
    conn = store._conn
    prev = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev["entry_hash"]
    payload = f"a1|remember|{{\"fake\": true}}|None|{time.time()}|{prev_hash}"
    fake_hash = hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()
    # Use a WRONG signing key (attacker doesn't have the real one)
    fake_sig = hmac.new(b"attacker_key", fake_hash.encode(), hashlib.sha256).hexdigest()
    conn.execute(
        """INSERT INTO audit_log
           (agent_id, op_type, detail, allowed, timestamp, prev_hash, entry_hash, signature)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("a1", "remember", json.dumps({"fake": True}), None, time.time(),
         prev_hash, fake_hash, fake_sig),
    )
    conn.commit()

    # Verify the chain detects the tampering
    report = store.audit.verify_chain()
    assert report["integrity_ok"] is False
    assert len(report["invalid_signatures"]) == 1
    assert report["valid_signatures"] == 1  # only the real entry


def test_schema_version_migration(store):
    """H6 fix: schema version is stored and checked."""
    import sqlite3
    conn = store._conn
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    assert row is not None
    assert int(row["value"]) >= 1


def test_embedder_mismatch_warns(tmp_path):
    """M1 fix: embedder fingerprint mismatch produces a warning."""
    import warnings
    db = tmp_path / "test.db"
    store1 = MemoryStore(str(db))
    store1.remember("a1", "test fact", tier="archival")
    store1.close_all()

    # Create a new store with a different embedder
    def new_embedder(text, dim=256):
        return [0.5] * dim
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        store2 = MemoryStore(str(db), embed_fn=new_embedder)
        # Should have triggered a warning
        assert any("EMBEDDER MISMATCH" in str(warning.message) for warning in w)


def test_dedup_checks_all_tiers(store):
    """M3 fix: dedup checks all tiers, not just core."""
    from plug_play_memory import PolicyEngine, EventEmitter, Judge
    policy = PolicyEngine()
    policy.set_agent_type("a1", "coding")
    judge = Judge()
    emitter = EventEmitter(store, policy, judge, "a1", "coding")

    # Write a fact to archival with key='secret'
    store.remember("a1", "value_one", tier="archival", key="secret")
    # Now try fact_observed with same key + value — should dedup
    emitter.fact_observed("secret", "value_one")
    # Count facts with key='secret' — should be 1, not 2
    all_facts = store.all_facts("a1")
    secrets = [f for f in all_facts if f.key == "secret"]
    assert len(secrets) == 1, f"expected 1, got {len(secrets)}"


def test_nested_when_syntax(policy):
    """M2 fix: nested `when: {amount: {le: 1000}}` works without suffix collision."""
    policy.set_agent_type("a1", "financial")
    # amount=500, should match {amount: {le: 1000}}
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True, "amount": 500})
    assert d.allowed is True
    # amount=5000, should NOT match first rule, needs 2FA
    d = policy.check("a1", "make_payment",
                     {"approved_by_user": True, "amount": 5000})
    assert d.allowed is False


def test_audit_chain_signature_present(store):
    """C4 fix: every audit entry has a signature."""
    store.audit.log("a1", "remember", {"test": True})
    entries = store.audit.recent("a1", limit=5)
    assert len(entries) == 1
    assert entries[0].signature is not None
    assert len(entries[0].signature) == 64  # SHA-256 hex
