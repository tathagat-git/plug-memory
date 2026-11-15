#!/usr/bin/env python3
"""v0.3 updater — updates v0.2 files to v0.3"""
import os, re, json

print("Updating plug-play-memory v0.2 → v0.3...")

# 1. __init__.py
with open("plug_play_memory/__init__.py", "w") as f:
    f.write('"""plug-play-memory: self-hosted, brain-inspired memory + permissions for AI agents."""\n\n__version__ = "0.3.0"\n\nfrom .storage import MemoryStore, Fact\nfrom .audit import AuditLog, AuditEntry\nfrom .policy import PolicyEngine, PolicyDecision\nfrom .events import AgentEvent\nfrom .judge import Judge, JudgeDecision, RulesJudge, HeuristicEnricher, SalienceScorer\nfrom .emitter import EventEmitter\nfrom .consolidation import Consolidator\nfrom .auth import AuthConfig, AuthContext, AuthError, ALL_SCOPES\n\n__all__ = ["MemoryStore","Fact","AuditLog","AuditEntry","PolicyEngine","PolicyDecision","AgentEvent","Judge","JudgeDecision","RulesJudge","HeuristicEnricher","SalienceScorer","EventEmitter","Consolidator","AuthConfig","AuthContext","AuthError","ALL_SCOPES"]\n')

# 2. pyproject.toml version
with open("pyproject.toml","r") as f: c=f.read()
c=c.replace('version = "0.2.0"','version = "0.3.0"')
with open("pyproject.toml","w") as f: f.write(c)

# 3. judge.py VERSION
with open("plug_play_memory/judge.py","r") as f: c=f.read()
if "VERSION" not in c[c.find("class Judge:"):c.find("class Judge:")+200]:
    c=c.replace('class Judge:\n    """The full judge pipeline','class Judge:\n    """The full judge pipeline: rules → heuristics → salience."""\n\n    VERSION = "v0.3-rules-v1"',1)
with open("plug_play_memory/judge.py","w") as f: f.write(c)

# 4. storage.py
with open("plug_play_memory/storage.py","r") as f: c=f.read()
c=c.replace("SCHEMA_VERSION = 2","SCHEMA_VERSION = 3")
c=c.replace("        if from_version < 2:\n            self._migrate_v1_to_v2(conn)","        if from_version < 2:\n            self._migrate_v1_to_v2(conn)\n        if from_version < 3:\n            self._migrate_v2_to_v3(conn)")
MIG='''    def _migrate_v2_to_v3(self, conn):
        """v2->v3: append-only event_log table"""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_log (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, agent_type TEXT NOT NULL,
                action TEXT NOT NULL, inputs TEXT DEFAULT '{}', outputs TEXT DEFAULT '{}',
                outcome TEXT DEFAULT 'success', raw TEXT DEFAULT '', timestamp REAL NOT NULL,
                judge_decision TEXT DEFAULT '{}', judge_version TEXT DEFAULT '',
                surfaced INTEGER DEFAULT 0, surfaced_at REAL, fact_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_eventlog_agent ON event_log(agent_id,timestamp);
            CREATE INDEX IF NOT EXISTS idx_eventlog_surfaced ON event_log(agent_id,surfaced);
            CREATE INDEX IF NOT EXISTS idx_eventlog_action ON event_log(agent_id,action);
        """)
'''
c=c.replace('    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:\n        """v1 → v2: no schema changes needed, but bump version to mark\n        security-hardened storage. Future v2 migrations go here."""\n        pass\n','    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:\n        """v1 -> v2: no schema changes."""\n        pass\n'+MIG)
METHODS='''
    # v0.3: Append-only event log + re-judge

    def log_event(self, agent_id, agent_type, action, inputs=None, outputs=None,
                  outcome="success", raw="", judge_decision=None, judge_version=""):
        """Append to event_log. ALWAYS succeeds."""
        validate_agent_id(agent_id)
        import uuid as _uuid
        eid = str(_uuid.uuid4())
        conn = self._conn
        conn.execute("INSERT INTO event_log (id,agent_id,agent_type,action,inputs,outputs,outcome,raw,timestamp,judge_decision,judge_version,surfaced) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (eid,agent_id,agent_type,action,json.dumps(inputs or {}),json.dumps(outputs or {}),outcome,raw,time.time(),json.dumps(judge_decision or {}),judge_version))
        conn.commit()
        return eid

    def surface_event(self, event_id, agent_id, value, tier="archival", key=None,
                      importance="normal", salience_score=0.5, tagged=False, metadata=None):
        """Promote from event_log to facts."""
        validate_agent_id(agent_id)
        conn = self._conn
        row = conn.execute("SELECT surfaced FROM event_log WHERE id=?", (event_id,)).fetchone()
        if not row or row["surfaced"]: return None
        fact = self.remember(agent_id=agent_id,value=value,tier=tier,key=key,importance=importance,salience_score=salience_score,tagged=tagged,metadata=metadata)
        conn.execute("UPDATE event_log SET surfaced=1,surfaced_at=?,fact_id=? WHERE id=?",(time.time(),fact.id,event_id))
        conn.commit()
        return fact

    def unsurface_event(self, event_id, agent_id):
        """Remove from facts, keep in event_log."""
        validate_agent_id(agent_id)
        conn = self._conn
        row = conn.execute("SELECT fact_id FROM event_log WHERE id=?", (event_id,)).fetchone()
        if not row or not row["fact_id"]: return False
        self.forget(agent_id, row["fact_id"], hard=False)
        conn.execute("UPDATE event_log SET surfaced=0,surfaced_at=NULL,fact_id=NULL WHERE id=?",(event_id,))
        conn.commit()
        return True

    def update_event_judge_decision(self, event_id, judge_decision, judge_version):
        conn = self._conn
        conn.execute("UPDATE event_log SET judge_decision=?,judge_version=? WHERE id=?",(json.dumps(judge_decision),judge_version,event_id))
        conn.commit()

    def get_all_events(self, agent_id, only_unsurfaced=False):
        validate_agent_id(agent_id)
        if only_unsurfaced:
            rows = self._conn.execute("SELECT * FROM event_log WHERE agent_id=? AND surfaced=0 ORDER BY timestamp",(agent_id,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM event_log WHERE agent_id=? ORDER BY timestamp",(agent_id,)).fetchall()
        return [{"id":r["id"],"agent_id":r["agent_id"],"agent_type":r["agent_type"],"action":r["action"],"inputs":json.loads(r["inputs"]),"outputs":json.loads(r["outputs"]),"outcome":r["outcome"],"raw":r["raw"],"timestamp":r["timestamp"],"judge_decision":json.loads(r["judge_decision"]),"judge_version":r["judge_version"],"surfaced":bool(r["surfaced"]),"surfaced_at":r["surfaced_at"],"fact_id":r["fact_id"]} for r in rows]

    def get_event_log_stats(self, agent_id):
        validate_agent_id(agent_id)
        conn = self._conn
        total = conn.execute("SELECT COUNT(*) as c FROM event_log WHERE agent_id=?",(agent_id,)).fetchone()["c"]
        surfaced = conn.execute("SELECT COUNT(*) as c FROM event_log WHERE agent_id=? AND surfaced=1",(agent_id,)).fetchone()["c"]
        return {"total_events":total,"surfaced":surfaced,"discarded":total-surfaced,"rejudgable":total-surfaced}
'''
if "def get_event_log_stats" not in c:
    c = c.rstrip() + "\n" + METHODS + "\n"
with open("plug_play_memory/storage.py","w") as f: f.write(c)

# 5. emitter.py
with open("plug_play_memory/emitter.py","r") as f: c=f.read()
NEW_EMIT='''    def _emit(self, action, inputs, outputs, outcome, raw):
        event = AgentEvent(agent_id=self.agent_id, agent_type=self.agent_type, action=action, inputs=inputs, outputs=outputs, outcome=outcome, raw=raw)
        decision = self.judge.evaluate(event)
        jd = {"should_save":decision.should_save,"category":decision.category,"tier":decision.tier,"key":decision.key,"importance":decision.importance,"salience_score":decision.salience_score,"tagged":decision.tagged,"summary":decision.summary,"discard_reason":decision.discard_reason}
        event_id = self.store.log_event(agent_id=self.agent_id,agent_type=self.agent_type,action=action,inputs=inputs,outputs=outputs,outcome=outcome,raw=raw,judge_decision=jd,judge_version=self.judge.VERSION)
        if not decision.should_save:
            self.store.audit.log(self.agent_id,"event_discarded",{"action":action,"outcome":outcome,"reason":decision.discard_reason,"event_id":event_id})
            return None
        return self._write_decision(decision, action, inputs, outputs, outcome, raw, event_id)

    def _write_decision(self, decision, action, inputs, outputs, outcome, raw, event_id=""):
        """policy gate -> dedup -> surface_event"""
        policy_decision = self.policy.check(self.agent_id,"write_memory",{"source":"event_judge","action":action,"category":decision.category,"tier":decision.tier},agent_type=self.agent_type)
        self.store.audit.log(self.agent_id,"permission_check",{"action":"write_memory","context":{"source":"event_judge","event_action":action,"category":decision.category,"tier":decision.tier},"backend":"layered_yaml","reason":policy_decision.reason,"layers":policy_decision.layers_evaluated},allowed=policy_decision.allowed)
        if not policy_decision.allowed: return None
        if decision.key and self._is_duplicate(decision.key, decision.summary):
            self.store.audit.log(self.agent_id,"event_deduplicated",{"action":action,"key":decision.key})
            return None
        if event_id:
            fact = self.store.surface_event(event_id=event_id,agent_id=self.agent_id,value=decision.summary,tier=decision.tier,key=decision.key,importance=decision.importance,salience_score=decision.salience_score,tagged=decision.tagged,metadata={"source":"event_judge","agent_type":self.agent_type,"action":action,"category":decision.category,"outcome":outcome,"inputs":inputs,"outputs":outputs,"event_id":event_id})
        else:
            fact = self.store.remember(agent_id=self.agent_id,value=decision.summary,tier=decision.tier,key=decision.key,importance=decision.importance,salience_score=decision.salience_score,tagged=decision.tagged,metadata={"source":"event_judge","agent_type":self.agent_type,"action":action,"category":decision.category,"outcome":outcome,"inputs":inputs,"outputs":outputs})
        return fact
'''
old_block = re.search(r'    def _emit\(.*?(?=\n    def _is_duplicate)', c, re.DOTALL)
if old_block:
    c = c[:old_block.start()] + NEW_EMIT + c[old_block.end():]
with open("plug_play_memory/emitter.py","w") as f: f.write(c)

# 6. consolidation.py
with open("plug_play_memory/consolidation.py","r") as f: c=f.read()
REJUDGE='''
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
'''
if "def rejudge" not in c:
    c = c.rstrip() + "\n" + REJUDGE + "\n"
with open("plug_play_memory/consolidation.py","w") as f: f.write(c)

# 7. server.py
with open("plug_play_memory/server.py","r") as f: c=f.read()
TOOLS='''
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

'''
if "def rejudge_events" not in c:
    c = c.replace('    @server.tool(description=(\n        "List all policy layers', TOOLS + '    @server.tool(description=(\n        "List all policy layers')
with open("plug_play_memory/server.py","w") as f: f.write(c)

print("\nv0.3 update complete!")
print("Files updated: __init__.py, storage.py, emitter.py, consolidation.py, judge.py, server.py, pyproject.toml")
print("\nNext: git add -A && git commit -m 'v0.3.0: append-only event log + re-judge' && git push")
