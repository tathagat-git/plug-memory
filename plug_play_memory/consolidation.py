"""
Consolidation — the brain's "sleep" phase for plug-play-memory.

Three background jobs, designed to be run periodically (e.g. once per day
or after every N events):

  1. consolidate_tagged()  — promote reinforced tagged facts to archival,
                             decay the rest. (Hippocampus → neocortex.)
  2. reinforce_on_recall() — boost importance + recall_count when a fact
                             is retrieved. (Spacing effect.)
  3. forget_unused()       — prune low/normal facts not recalled in N days.
                             (Active forgetting.)

Together these make the memory self-tuning: facts the agent actually uses
get stronger; facts it never recalls fade. No LLM required.
"""

from __future__ import annotations

import time
from typing import Any

from .storage import MemoryStore


class Consolidator:
    """Background jobs that turn raw memory into tuned memory."""

    def __init__(self, store: MemoryStore):
        self.store = store

    # ------------------------------------------------------------------
    # 1. Sleep consolidation — review tagged facts
    # ------------------------------------------------------------------

    def consolidate_tagged(self, agent_id: str) -> dict[str, int]:
        """
        For each tagged fact:
          - If recall_count > 0 → promote to archival, clear tag, boost importance
          - If similar events happened recently → promote (pattern reinforcement)
          - Else → decay importance; if it hits low and is old, forget it
        """
        tagged = self.store.get_tagged(agent_id, older_than_hours=0)
        stats = {"promoted": 0, "decayed": 0, "forgotten": 0, "skipped": 0}

        for fact in tagged:
            # Was it recalled?
            if fact.recall_count > 0:
                self.store.promote(fact.id, tier="archival")
                self._boost_importance(fact.id)
                stats["promoted"] += 1
                self.store.audit.log(agent_id, "consolidation_promote", {
                    "fact_id": fact.id, "reason": "recalled",
                    "recall_count": fact.recall_count,
                })
                continue

            # Similar event in last 24h? (poor man's pattern detection)
            if self._has_similar_recent(agent_id, fact, hours=24):
                self.store.promote(fact.id, tier="archival")
                stats["promoted"] += 1
                self.store.audit.log(agent_id, "consolidation_promote", {
                    "fact_id": fact.id, "reason": "similar_recent_event",
                })
                continue

            # Otherwise decay
            self.store.decay_importance(fact.id, levels=1)
            refreshed = self._get_fact(fact.id)
            if refreshed and refreshed.importance == "low":
                age_hours = (time.time() - refreshed.created_at) / 3600
                if age_hours > 48:
                    self.store.forget(agent_id, fact.id, hard=False)
                    stats["forgotten"] += 1
                    self.store.audit.log(agent_id, "consolidation_forget", {
                        "fact_id": fact.id, "reason": "low_salience_unused",
                    })
                else:
                    stats["decayed"] += 1
            else:
                stats["decayed"] += 1

        return stats

    # ------------------------------------------------------------------
    # 2. Recall reinforcement — spacing effect
    # ------------------------------------------------------------------

    def reinforce_on_recall(self, fact_id: str) -> None:
        """Call this every time a fact is recalled."""
        self.store.update_recall_stats(fact_id)
        # Boost importance if recalled multiple times
        fact = self._get_fact(fact_id)
        if fact and fact.recall_count >= 3 and fact.importance == "normal":
            self._boost_importance(fact_id)
        elif fact and fact.recall_count >= 5 and fact.importance == "low":
            self._boost_importance(fact_id)

    # ------------------------------------------------------------------
    # 3. Active forgetting — prune unused
    # ------------------------------------------------------------------

    def forget_unused(self, agent_id: str) -> dict[str, int]:
        """
        Active forgetting sweep. Brains don't keep unused memories forever.
          - low importance, not recalled in 30 days → forget
          - normal importance, not recalled in 90 days → decay to low
          - high importance → never auto-forget (like childhood memories)
        """
        stats = {"forgotten_low": 0, "decayed_normal": 0}

        # Forget low-importance facts not recalled in 30 days
        for fact in self.store.get_stale(agent_id, importance="low", days_since_recall=30):
            self.store.forget(agent_id, fact.id, hard=False)
            stats["forgotten_low"] += 1
            self.store.audit.log(agent_id, "active_forgetting", {
                "fact_id": fact.id, "reason": "low_importance_unused_30d",
            })

        # Decay normal-importance facts not recalled in 90 days
        for fact in self.store.get_stale(agent_id, importance="normal", days_since_recall=90):
            self.store.decay_importance(fact.id, levels=1)
            stats["decayed_normal"] += 1
            self.store.audit.log(agent_id, "active_decay", {
                "fact_id": fact.id, "reason": "normal_importance_unused_90d",
            })

        return stats

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_fact(self, fact_id: str):
        rows = self.store._conn.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchall()
        if not rows:
            return None
        return self.store._row_to_fact(rows[0])

    def _has_similar_recent(self, agent_id: str, fact, hours: int) -> bool:
        """Has an event with the same action happened in the last N hours?"""
        cutoff = time.time() - (hours * 3600)
        action = fact.metadata.get("action", "")
        if not action:
            return False
        rows = self.store._conn.execute(
            """SELECT COUNT(*) as c FROM facts
               WHERE agent_id = ? AND created_at >= ?
                 AND json_extract(metadata, '$.action') = ?
                 AND id != ?""",
            (agent_id, cutoff, action, fact.id),
        ).fetchone()
        return rows["c"] > 0

    def _boost_importance(self, fact_id: str) -> None:
        """Boost one level: low → normal → high (H5 fix — direction was reversed)."""
        # Order is low → normal → high. Boost moves TOWARD high (higher index).
        order = ["low", "normal", "high"]
        fact = self._get_fact(fact_id)
        if not fact:
            return
        idx = order.index(fact.importance) if fact.importance in order else 1
        new_idx = min(len(order) - 1, idx + 1)  # +1 to move toward high (was -1, wrong direction)
        if new_idx != idx:
            conn = self.store._conn
            conn.execute(
                "UPDATE facts SET importance = ?, updated_at = ? WHERE id = ?",
                (order[new_idx], time.time(), fact_id),
            )
            conn.commit()

    # v0.3: Re-judge

    def rejudge(self, agent_id, judge):
        """Re-evaluate ALL events with current judge rules."""
        from .events import AgentEvent
        all_events = self.store.get_all_events(agent_id, only_unsurfaced=False)
        stats = {"rejudged":0,"newly_surfaced":0,"unsurfaced":0,"unchanged":0}
        for ed in all_events:
            event = AgentEvent(agent_id=ed["agent_id"],agent_type=ed["agent_type"],action=ed["action"],inputs=ed["inputs"],outputs=ed["outputs"],outcome=ed["outcome"],raw=ed["raw"])
            nd = judge.evaluate(event)
            jd = {"should_save":nd.should_save,"category":nd.category,"tier":nd.tier,"key":nd.key,"importance":nd.importance,"salience_score":nd.salience_score,"tagged":nd.tagged,"summary":nd.summary,"discard_reason":nd.discard_reason}
            self.store.update_event_judge_decision(ed["id"],jd,judge.VERSION)
            stats["rejudged"] += 1
            ws = ed["surfaced"]; sbs = nd.should_save
            if sbs and not ws:
                self.store.surface_event(event_id=ed["id"],agent_id=agent_id,value=nd.summary,tier=nd.tier,key=nd.key,importance=nd.importance,salience_score=nd.salience_score,tagged=nd.tagged,metadata={"source":"rejudge","agent_type":ed["agent_type"],"action":ed["action"],"category":nd.category,"outcome":ed["outcome"],"inputs":ed["inputs"],"outputs":ed["outputs"],"event_id":ed["id"],"rejudged_with":judge.VERSION})
                stats["newly_surfaced"] += 1
            elif not sbs and ws:
                self.store.unsurface_event(ed["id"],agent_id)
                stats["unsurfaced"] += 1
            else:
                stats["unchanged"] += 1
        self.store.audit.log(agent_id,"rejudge",{"judge_version":judge.VERSION,"stats":stats})
        return stats

