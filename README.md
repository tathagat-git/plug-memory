# plug-play-memory

> Self-hosted, brain-inspired memory + permissions for AI agents. MCP-native. One SQLite file.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![MCP-compatible](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/tathagat-git/plug-play-memory.svg)](https://github.com/tathagat-git/plug-play-memory/stargazers)

Most agent memory systems (Mem0, Zep, Letta) try to extract facts from chat. plug-play-memory agents **emit structured events** about what they did, a **judge decides what's worth saving**, and a **layered policy engine** enforces what they're allowed to do.

Your coding agent remembers the bug it fixed last week, not the small talk around it. Your study agent remembers your misconceptions, not every question you asked. Runs on your laptop. Costs $0. No data leaves your machine.

---

## Why this exists

Agent memory today has three problems:

1. **Memory is decoupled from permissions.** Mem0/Zep/Letta store facts; Lasso/Pillar enforce security. Nobody does both. So an LLM can self-write malicious facts, or a fine-tuned model can be talked out of prompt-based "permissions."
2. **Chat-based extraction is fragile.** Regex on conversation text false-positives on code (`for x in m` looks like a fact). LLM-judge-on-every-message is expensive and noisy.
3. **Memory grows forever.** No active forgetting. No consolidation. No concept of "this fact stopped being useful."

plug-play-memory fixes all three:

| Problem | Fix |
|---|---|
| Memory without permissions | **Layered policy engine** with OPA/YAML backends, default-deny, defense-in-depth |
| Chat-based extraction | **Event-based memory** — agents emit structured events, not raw chat |
| Infinite memory | **Brain-inspired consolidation** — tag at encode, sleep-consolidate, actively forget unused |

---

## How it works

```
Agent (any kind: coding, study, financial, support)
        │
        │ emits structured events
        ▼
┌─────────────────────────────────────────┐
│  EventEmitter (SDK)                     │
│  - file_edited(path, lines_changed)     │
│  - git_pushed(branch, sha)              │
│  - decision_made(topic, choice)         │
│  - answer_attempted(topic, correct)     │
│  - make_payment(amount, recipient)      │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Judge (no LLM required)                │
│  1. Rules — deterministic per-action    │
│  2. Heuristics — regex on raw field     │
│  3. Salience — 4-factor brain score     │
└────────────────┬────────────────────────┘
                 │ should_save, salience, tagged
                 ▼
┌─────────────────────────────────────────┐
│  Policy Gate (layered)                  │
│  builtin/_base → builtin/{type}         │
│  → custom/{type} → custom/{agent_id}    │
│  Deny rules in _base are absolute       │
└────────────────┬────────────────────────┘
                 │ allowed?
                 ▼
┌─────────────────────────────────────────┐
│  MemoryStore (one SQLite file)          │
│  + Audit Log (append-only, hash-chain)  │
└─────────────────────────────────────────┘
                 │
                 │ runs nightly
                 ▼
┌─────────────────────────────────────────┐
│  Consolidator (the "sleep" phase)       │
│  - Promote tagged facts that got recalled│
│  - Decay tagged facts that didn't       │
│  - Forget low-importance facts unused 30d│
└─────────────────────────────────────────┘
```

---

## 30-second quickstart

```bash
pip install plug-play-memory

# Initialize a project
agentmemory init my-agent --type coding
cd my-agent

# Run the MCP server (for Claude Desktop / Cursor)
agentmemory serve
```

Or use it as a Python library:

```python
from plug_play_memory import (
    MemoryStore, PolicyEngine, EventEmitter, Judge, Consolidator,
)

store = MemoryStore("memory.db")
policy = PolicyEngine()
policy.set_agent_type("coder-1", "coding")
judge = Judge()
emitter = EventEmitter(store, policy, judge, agent_id="coder-1", agent_type="coding")

# Emit structured events — the judge + policy handle the rest
emitter.decision_made(
    topic="auth",
    choice="use JWT",
    reason="simpler than sessions",
)
# → saved to archival, importance=high, salience=0.30

emitter.test_run(suite="tests/", passed=10, failed=0)
# → discarded by judge (passing tests aren't memorable)

emitter.test_run(
    suite="tests/auth/", passed=11, failed=1,
    failures=["test_rate_limit: expected 429, got 200"],
)
# → saved to archival as error, importance=high

# Recall
results = store.recall_semantic("coder-1", "auth rate limiting", k=3)
for f in results:
    print(f.value)
```

---

## Use with Claude Desktop / Cursor

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "plug-play-memory": {
      "command": "agentmemory",
      "args": [
        "serve",
        "--db", "/absolute/path/to/memory.db",
        "--config", "/absolute/path/to/config.yaml"
      ]
    }
  }
}
```

Claude now has these 8 tools:

| Tool | What it does |
|---|---|
| `remember_event` | Emit a structured event; judge + policy + storage handle it |
| `recall` | Retrieve memory (core / recall / archival tiers) |
| `check_permission` | Hard enforcement gate before side-effecting actions |
| `forget` | Soft-delete a fact (preserves audit trail) |
| `audit_recent` | Recent audit log entries for this agent |
| `coverage_manifest` | What's actually been governed (not a safety claim) |
| `consolidate` | Run sleep + active forgetting sweep |
| `list_policies` | Show loaded policy layers |

---

## Brain-inspired memory design

Seven mechanisms, each mapped to a real biological principle:

| Brain mechanism | plug-play-memory equivalent |
|---|---|
| Attention filter (thalamus) | EventEmitter only emits meaningful events |
| Emotional salience (amygdala) | `outcome` field + salience scoring |
| Prediction error (dopamine) | Salience score boosts on surprising outcomes |
| Two-stage memory (hippocampus → neocortex) | `recall` tier (fast) → `archival` tier (durable) |
| Synaptic tagging (STC) | `tagged` flag — fragile until consolidated |
| Sleep consolidation | Background job promotes reinforced tagged facts |
| Active forgetting | Decay + prune unused low-importance facts |
| Spacing effect | `recall_count` strengthens frequently-recalled facts |

**Self-tuning:** memories the agent actually uses get stronger. Memories it never recalls fade. No LLM required.

---

## Layered policy system

Four built-in packs ship with the package:

```
policies/builtin/
├── _base.yaml      # deny-all baseline, defense-in-depth absolute denies
├── coding.yaml     # coding agent rules (file_edited, git_pushed, deploy, etc.)
├── study.yaml      # study agent rules (question_asked, answer_attempted, etc.)
└── financial.yaml  # financial agent rules (tiered payment authorization)
```

Custom overrides live in `policies/custom/` and **extend (not replace)** built-ins:

```yaml
# policies/custom/study.yaml
name: study
default: deny
rules:
  # Allow a new action
  - action: contact_teacher
    allow: true
    requires: [approved_by_student]

  # Tighten: students at this school can't sync to external apps
  - action: sync_to_flashcard_app
    allow: false
```

**Deny rules in `builtin/_base.yaml` are absolute** — custom policies cannot weaken them. This is the safety floor.

### Tiered authorization example

The financial pack supports amount-based rules:

```yaml
- action: make_payment
  allow: true
  requires: [approved_by_user]
  when:
    amount_le: 1000          # up to ₹1000 — just user approval

- action: make_payment
  allow: true
  requires: [approved_by_user, second_factor_verified]
  when:
    amount_le: 10000         # up to ₹10,000 — approval + 2FA

# Implicit: payments > ₹10,000 always denied by default
```

---

## Comparison vs other agent memory systems

| | Mem0 | Zep | Letta (MemGPT) | Lasso / Pillar | **plug-play-memory** |
|---|---|---|---|---|---|
| Tiered memory | Partial | Yes | Yes | — | ✅ core/recall/archival |
| Framework-agnostic | Yes | Yes | No (LangChain) | — | ✅ MCP-native |
| Hard-enforced permissions | — | — | — | Yes (separate product) | ✅ built-in |
| Audit + coverage manifest | — | — | — | Partial | ✅ append-only, HMAC-signed |
| Self-hosted, zero infra | Depends | No (Postgres) | No (Docker) | Cloud + on-prem | ✅ one SQLite file |
| Brain-inspired consolidation | — | — | — | — | ✅ tagging + sleep + forgetting |
| Event-based capture | — | — | — | — | ✅ structured events, not chat |
| Default-deny, fail-closed | — | — | — | Partial | ✅ defense-in-depth + whitelist |
| **Cost** | Free + paid cloud | Free + paid cloud | Free + paid | Free tier + paid | **Free, runs on laptop** |

The differentiation isn't "better memory" — Letta's memory tiering is more mature. It's **memory and permissions treated as one governed system, brain-inspired consolidation, and event-based capture instead of chat extraction.**

---

## CLI reference

```bash
# Initialize a project with a specific agent type
agentmemory init my-tutor --type study
agentmemory init my-coder --type coding
agentmemory init my-finance --type financial

# Run the MCP server (stdio transport)
agentmemory serve --db memory.db --config config.yaml

# Policy management
agentmemory policy list                          # show loaded layers
agentmemory policy show --agent-id priya --agent-type study
agentmemory policy customize study               # scaffold custom override
agentmemory policy validate                      # validate YAML syntax

# Health check
agentmemory doctor
```

---

## MCP server tools (full reference)

### `remember_event(action, inputs, outputs, outcome, raw)`

Emit a structured event. The judge decides whether to save it.

```python
# Examples
remember_event(
    action="decision_made",
    inputs={"topic": "auth"},
    outputs={"choice": "JWT", "reason": "simpler"},
    outcome="success",
)
```

### `recall(query, tier, limit)`

Retrieve memory. Returns core memory plus tier-specific results.

```python
recall(query="auth rate limiting", tier="archival", limit=5)
recall(tier="core")  # just always-injected facts
recall(tier="recall", limit=20)  # recent context
```

### `check_permission(action, context)`

Hard enforcement gate. **Always call this before any side-effecting action.**

```python
check_permission(action="make_payment",
                 context={"approved_by_user": True, "amount": 1000})
# → {allowed: True, reason: "matched rule in builtin:financial", ...}

check_permission(action="access_credentials")
# → {allowed: False, reason: "absolute deny in builtin:_base", ...}
```

### `forget(fact_id, hard=False)`

Soft-delete by default (preserves audit trail). Hard delete requires admin scope.

### `audit_recent(limit)` / `coverage_manifest()`

Compliance and debugging tools.

### `consolidate()`

Run the brain's "sleep" phase: promote reinforced tagged facts, decay the rest, prune unused.

### `list_policies()`

Show which policy layers are loaded for the current agent.

---

## Security design

**`agent_id` is NEVER a tool parameter.** It comes from `AuthContext` at connection time, established from:
- Environment variables (`AGENTMEMORY_AGENT_ID`, `AGENTMEMORY_AGENT_TYPE`) for local stdio
- Auth config file with token → agent_id mapping for multi-tenant
- (Future) Bearer token from HTTP headers for remote MCP

A prompt-injected LLM **cannot** call `remember_event(agent_id="admin", ...)` because there is no `agent_id` parameter. The agent identity is established once at connection time and used for every subsequent call.

### Audit log integrity

- **Append-only** enforced by SQLite triggers (`UPDATE` and `DELETE` both fail)
- **Hash-chained** — each entry references the previous entry's hash (tamper-evident)
- Every operation logged: `remember`, `recall`, `forget`, `permission_check`, `consolidation`, `active_forgetting`

### Coverage manifest (not a safety claim)

`coverage_manifest()` reports **what was actually checked**, not a safety score:

```json
{
  "agent_id": "coder-1",
  "actions_checked": ["write_memory", "read_memory", "access_credentials"],
  "counts_by_action": {
    "write_memory": {"allowed": 7, "denied": 0},
    "access_credentials": {"allowed": 0, "denied": 1}
  },
  "total_permission_checks": 8,
  "note": "Reflects only actions actually attempted — not a safety claim."
}
```

---

## Self-hosted by design

Your agent's memory stays on your machine in v0.1's default configuration.

- One portable SQLite file. Back up with `cp memory.db backup.db`.
- No external database server required.
- No cloud dependency for the core system.
- **For production / regulated industries:** enable encryption at rest with `--encryption-key` (SQLCipher) and audit log signing with `--audit-key` (HMAC). See [Security](#security) below.
- Optional: cloud LLM for richer summaries (you provide your own API key, we never see it). When this lands in v0.2, only the `raw` field of events you explicitly send will be processed — core storage never leaves your machine.

**Honest scope of v0.1:** the default config is "trust the local user" — fine for OSS dev. For enterprise/regulated deployment, you must enable encryption + audit signing + use the multi-tenant token config. See `SECURITY_AUDIT_v0.1.md` for what was fixed and what's still on the roadmap.

---

## Roadmap

### v0.1 (current)
- ✅ Three-tier memory (core/recall/archival)
- ✅ Layered policy engine with 4 built-in packs
- ✅ Rules + heuristics + salience judge (no LLM required)
- ✅ Brain-inspired consolidation (tagging, sleep, active forgetting)
- ✅ Append-only hash-chained audit log
- ✅ MCP server with 8 tools
- ✅ CLI with init/serve/policy/doctor commands

### v0.2 (next)
- 🔲 Optional Ollama integration for richer summaries
- 🔲 Real sqlite-vec semantic search (currently keyword-based for zero-dependency)
- 🔲 OPA/Rego backend (currently YAML-only)
- 🔲 HTTP transport for remote MCP (currently stdio-only)
- 🔲 Web dashboard for audit log + coverage manifest

### v0.3
- 🔲 LangGraph integration (auto-emit on state transitions)
- 🔲 CrewAI integration
- 🔲 Aider-style coding agent example
- 🔲 Benchmark suite vs Mem0/Zep/Letta

### v1.0
- 🔲 Enterprise tier: signed policy packs, SIEM integration, SSO
- 🔲 Multi-region sync for self-hosted enterprise
- 🔲 Policy marketplace (curated, audited policy bundles)

---

## Limitations (honest)

- **Embeddings:** v1 uses BLAKE2b hashing as a dependency-free placeholder embedder. Swap in a real embedder via `MemoryStore(embed_fn=...)` for genuine semantic recall.
- **Vector search:** v1 uses keyword matching for archival recall. sqlite-vec integration is on the v0.2 roadmap.
- **Judge:** Rules + heuristics handle ~90% of cases correctly. The 10% it misses are saved with a `tagged` flag for review during consolidation. Optional LLM enrichment coming in v0.2.
- **Passive capture:** v1 requires explicit event emission (no automatic capture from chat). This is by design — automatic capture introduces cost and prompt-injection surface.
- **TTL:** `ttl_seconds` is stored per-fact but not yet actively swept. Active forgetting handles the equivalent cleanup.

---

## Contributing

PRs welcome. Especially valuable:

- More built-in policy packs (legal, healthcare, devops, customer-support)
- Integration examples (LangGraph, CrewAI, AutoGen, Aider)
- Benchmarks against Mem0/Zep/Letta
- Bug reports + reproducible test cases

See `tests/` for the existing test suite (26 unit tests + 3-agent end-to-end demo + MCP server test).

---

## License

Apache-2.0. Use it commercially, fork it, sell it. Just don't blame us if your agent remembers things you didn't want it to.

---

## Author

**Tathagat** — AI/ML engineer building systems end-to-end.

- GitHub: [@tathagat-git](https://github.com/tathagat-git)
- Twitter: [@tathagat_](https://twitter.com/tathagat_)
- LinkedIn: [tathagat-tathagat3460](https://linkedin.com/in/tathagat-tathagat3460)
- Portfolio: [tathagat-git.github.io](https://tathagat-git.github.io/)

Built while studying how the brain decides what to remember. The neuroscience parallels are real; the implementation is pragmatic Python.

---

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=tathagat-git/plug-play-memory&type=Date)](https://star-history.com/#tathagat-git/plug-play-memory&Date)

---

**If this project saves you time, [star the repo](https://github.com/tathagat-git/plug-play-memory).** It's the only metric that tells me to keep building.
