"""
Event emitter — the SDK agents call to record meaningful actions.

Each emit() goes through:
  1. Judge evaluation (rules + heuristics + salience)
  2. Policy gate (does this agent have permission to write_memory from this source?)
  3. Deduplication against existing memory
  4. MemoryStore.remember() with salience + tagged metadata
  5. Audit log entry
"""

from __future__ import annotations

from typing import Any

from .events import AgentEvent
from .judge import Judge
from .policy import PolicyEngine
from .storage import MemoryStore


class EventEmitter:
    """Agents call helpers on this; the system handles judging + policy + storage."""

    def __init__(
        self,
        store: MemoryStore,
        policy: PolicyEngine,
        judge: Judge,
        agent_id: str,
        agent_type: str,
    ):
        self.store = store
        self.policy = policy
        self.judge = judge
        self.agent_id = agent_id
        self.agent_type = agent_type

    # ------------------------------------------------------------------
    # Public API — agents call these
    # ------------------------------------------------------------------

    def emit(
        self,
        action: str,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        outcome: str = "success",
        raw: str = "",
    ):
        """Generic emit. Use this for actions without a dedicated helper."""
        return self._emit(action, inputs or {}, outputs or {}, outcome, raw)

    # ---- CODING helpers ----

    def file_edited(self, path: str, diff_summary: str = "", lines_changed: int = 0, success: bool = True):
        return self._emit(
            action="file_edited",
            inputs={"path": path, "lines_changed": lines_changed},
            outputs={"diff_summary": diff_summary},
            outcome="success" if success else "failure",
            raw=diff_summary,
        )

    def git_pushed(self, branch: str, sha: str, repo: str, message: str = ""):
        return self._emit(
            action="git_pushed",
            inputs={"branch": branch, "repo": repo},
            outputs={"sha": sha, "message": message},
            outcome="success",
            raw=message,
        )

    def test_run(self, suite: str, passed: int, failed: int, failures: list[str] | None = None):
        outcome = "success" if failed == 0 else ("partial" if passed > 0 else "failure")
        return self._emit(
            action="test_run",
            inputs={"suite": suite, "passed": passed, "failed": failed},
            outputs={"failures": failures or []},
            outcome=outcome,
            raw="; ".join(failures or []),
        )

    def decision_made(self, topic: str, choice: str, reason: str = ""):
        return self._emit(
            action="decision_made",
            inputs={"topic": topic},
            outputs={"choice": choice, "reason": reason},
            outcome="success",
            raw=f"Decided {choice} for {topic}. Reason: {reason}",
        )

    def error_encountered(self, message: str, context: dict[str, Any] | None = None):
        return self._emit(
            action="error_encountered",
            inputs=context or {},
            outputs={"error": message},
            outcome="failure",
            raw=message,
        )

    def deployed(self, target: str, version: str, success: bool, notes: str = ""):
        return self._emit(
            action="deployed",
            inputs={"target": target, "version": version},
            outputs={"success": success, "notes": notes},
            outcome="success" if success else "failure",
            raw=notes,
        )

    def migration_run(self, name: str, success: bool, error: str = ""):
        return self._emit(
            action="migration_run",
            inputs={"migration": name},
            outputs={"success": success, "error": error},
            outcome="success" if success else "failure",
            raw=error or f"Migration {name} completed",
        )

    # ---- STUDY helpers ----

    def fact_observed(self, field_name: str, value: str, raw: str = ""):
        # Use field_name as the dedup key so repeated observations
        # of the same field update rather than accumulate.
        from .judge import JudgeDecision
        decision = self.judge.evaluate(AgentEvent(
            agent_id=self.agent_id, agent_type=self.agent_type,
            action="fact_observed", outcome="success",
            inputs={"field": field_name},
            outputs={"value": value},
            raw=raw or f"{field_name}: {value}",
        ))
        if decision.should_save and not decision.key:
            decision.key = field_name
        # Bypass _emit's re-judging and write directly
        if not decision.should_save:
            self.store.audit.log(
                self.agent_id, "event_discarded",
                {"action": "fact_observed", "reason": decision.discard_reason},
            )
            return None
        # Apply same policy + dedup + write pipeline as _emit
        return self._write_decision(decision, "fact_observed", {"field": field_name},
                                    {"value": value}, "success", raw or f"{field_name}: {value}")

    def question_asked(self, topic: str, question: str, difficulty: str = "normal"):
        return self._emit(
            action="question_asked",
            inputs={"topic": topic, "difficulty": difficulty},
            outputs={"question": question},
            outcome="success",
            raw=question,
        )

    def answer_attempted(
        self,
        topic: str,
        correct: bool,
        misconception: str = "",
        question_id: str = "",
        student_answer: str = "",
    ):
        outcome = "success" if correct else "failure"
        return self._emit(
            action="answer_attempted",
            inputs={"topic": topic, "question_id": question_id},
            outputs={
                "correct": correct,
                "misconception": misconception,
                "student_answer": student_answer,
            },
            outcome=outcome,
            raw=f"Student answer: {student_answer}. Misconception: {misconception}" if misconception else f"Student answer: {student_answer}",
        )

    def concept_mastered(self, topic: str, mastery_level: float, attempts: int = 1):
        return self._emit(
            action="concept_mastered",
            outcome="success",
            inputs={"topic": topic, "attempts": attempts},
            outputs={"mastery_level": mastery_level},
            raw=f"Mastered {topic} at {mastery_level:.0%} after {attempts} attempts",
        )

    def knowledge_gap_found(self, topic: str, gap: str, severity: str = "high"):
        return self._emit(
            action="knowledge_gap_found",
            outcome="failure",
            inputs={"topic": topic, "severity": severity},
            outputs={"gap": gap},
            raw=gap,
        )

    def goal_set(self, description: str, deadline: str = "", priority: str = "normal"):
        return self._emit(
            action="goal_set",
            outcome="success",
            inputs={"deadline": deadline, "priority": priority},
            outputs={"description": description},
            raw=description,
        )

    # ---- FINANCIAL helpers ----

    def make_payment(self, amount: float, recipient: str, success: bool, notes: str = ""):
        return self._emit(
            action="make_payment",
            inputs={"amount": amount, "recipient": recipient},
            outputs={"success": success, "notes": notes},
            outcome="success" if success else "failure",
            raw=notes or f"Payment of {amount} to {recipient}",
        )

    def set_budget_limit(self, category: str, limit: float, period: str = "monthly"):
        return self._emit(
            action="set_budget_limit",
            outcome="success",
            inputs={"category": category, "limit": limit, "period": period},
            outputs={},
            raw=f"Budget {category}: {limit} per {period}",
        )

    # ------------------------------------------------------------------
    # Core emit logic — judge → policy → dedup → store
    # ------------------------------------------------------------------

    def _emit(
        self,
        action: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        outcome: str,
        raw: str,
    ):
        event = AgentEvent(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            action=action,
            inputs=inputs,
            outputs=outputs,
            outcome=outcome,
            raw=raw,
        )

        # Step 1: judge
        decision = self.judge.evaluate(event)

        if not decision.should_save:
            self.store.audit.log(
                self.agent_id, "event_discarded",
                {
                    "action": action, "outcome": outcome,
                    "reason": decision.discard_reason,
                },
            )
            return None

        return self._write_decision(decision, action, inputs, outputs, outcome, raw)

    def _write_decision(
        self,
        decision,
        action: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        outcome: str,
        raw: str,
    ):
        """Shared write path: policy gate → dedup → store → audit. Used by _emit and fact_observed."""

        # Step 2: policy gate — agent must have write_memory permission for this category
        # M5 fix: pass agent_type explicitly to avoid shared-state mutation
        policy_decision = self.policy.check(
            self.agent_id, "write_memory",
            {
                "source": "event_judge",
                "action": action,
                "category": decision.category,
                "tier": decision.tier,
            },
            agent_type=self.agent_type,
        )
        # Log as permission_check so coverage_manifest picks it up
        self.store.audit.log(
            self.agent_id, "permission_check",
            {
                "action": "write_memory",
                "context": {
                    "source": "event_judge",
                    "event_action": action,
                    "category": decision.category,
                    "tier": decision.tier,
                },
                "backend": "layered_yaml",
                "reason": policy_decision.reason,
                "layers": policy_decision.layers_evaluated,
            },
            allowed=policy_decision.allowed,
        )
        if not policy_decision.allowed:
            return None

        # Step 3: dedup — skip if same key + same value already exists
        if decision.key and self._is_duplicate(decision.key, decision.summary):
            self.store.audit.log(
                self.agent_id, "event_deduplicated",
                {"action": action, "key": decision.key},
            )
            return None

        # Step 4: write with rich metadata
        fact = self.store.remember(
            agent_id=self.agent_id,
            value=decision.summary,
            tier=decision.tier,
            key=decision.key,
            importance=decision.importance,
            salience_score=decision.salience_score,
            tagged=decision.tagged,
            metadata={
                "source": "event_judge",
                "agent_type": self.agent_type,
                "action": action,
                "category": decision.category,
                "outcome": outcome,
                "inputs": inputs,
                "outputs": outputs,
            },
        )
        return fact

    def _is_duplicate(self, key: str, value: str) -> bool:
        """Check ALL tiers for duplicate (M3 fix — was core-only)."""
        for f in self.store.all_facts(self.agent_id):
            if f.key == key and f.value.lower() == value.lower():
                return True
        return False
