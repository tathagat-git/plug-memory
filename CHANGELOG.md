# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned for v0.3
- Optional Ollama integration for richer judge summaries
- Real sqlite-vec semantic search (currently keyword-based for zero-dependency)
- OPA/Rego backend for production policy enforcement
- HTTP transport for remote MCP (currently stdio-only)
- Web dashboard for audit log + coverage manifest

## [0.2.0] - 2026-10-31 — Security hardening

### 🔴 Critical fixes

- **C1: Path traversal in `agentmemory policy customize`** — name validation
  now rejects path separators and reserved builtin pack names (`_base`,
  `coding`, `study`, `financial`). Resolved target must be inside `custom/`.
- **C2: Optional encryption at rest** — `MemoryStore` accepts
  `encryption_key` parameter for SQLCipher. Recommended for production /
  regulated deployments. CLI flags: `--encryption-key` /
  `AGENTMEMORY_DB_KEY` env var.
- **C3: Policy bypass via category manipulation** — added per-`agent_type`
  action whitelist. Unknown actions are denied at the gate before the judge
  can categorize them. Agents can no longer lie about category to write
  disallowed facts.
- **C4: HMAC-signed audit entries** — every audit entry now carries an
  HMAC-SHA256 signature. New `verify_audit_integrity` MCP tool detects
  tampering (appended fake entries fail signature check). Triggers still
  block UPDATE/DELETE on rows.

### 🟠 High-severity fixes

- **H1: Event size limits** — `remember_event` rejects events over 100KB
  total or 10KB per field. Prevents memory exhaustion DoS.
- **H2: Recall/audit limit cap** — `recall(limit=N)` and `audit_recent`
  capped at 1000. Query length capped at 1000 chars.
- **H3: Rate limiting** — `remember_event` limited to 100 calls/min per
  agent_id. Configurable via `RateLimiter` class.
- **H4: Tokens hashed at rest** — config.yaml tokens are SHA-256 hashed on
  load. Plaintext never persisted in memory. Supports pre-hashed
  `token_hash` field for production deployments.
- **H5: `_boost_importance` direction reversed** — was moving importance
  toward `low` instead of `high`. Recall reinforcement now actually works.
- **H6: Real schema migrations** — schema version is now checked on init.
  Future-version DBs are rejected with a clear error. Migration runner
  framework in place for v2→v3+ upgrades.

### 🟡 Medium fixes

- **M1: Embedder fingerprint validation** — `MemoryStore` warns loudly when
  the embedder changes, since old archival embeddings become incompatible.
- **M2: Nested `when` syntax** — `when: {amount: {le: 1000}}` is now the
  preferred form. Old suffix form (`amount_le: 1000`) still works but
  emits a `DeprecationWarning`. Fixes suffix collision with legitimate
  context keys ending in `_le`, `_ge`, etc.
- **M3: Dedup checks all tiers** — `_is_duplicate` was only checking `core`
  tier; now checks all tiers. Same key + value can no longer coexist in
  different tiers.
- **M4: Reserved agent_id validation** — `agent_id` cannot be `_base`,
  `coding`, `study`, `financial`, `default`, or `all`. Prevents collision
  with policy layer names.
- **M5: `agent_type` as parameter** — `policy.check()` accepts
  `agent_type` parameter, eliminating shared-state mutation race condition
  between concurrent agents on the same PolicyEngine.
- **M6: Dockerfile non-root user** — container runs as `agentmemory` user
  (UID 1000), not root.
- **M7: Dockerfile VOLUME on directory** — was `VOLUME ["/app/memory.db"]`
  (file path, broken). Now `VOLUME ["/app/data"]` (directory, correct).

### 🟢 Low / hygiene fixes

- **L1: README accuracy** — removed "Lasso/Pillar paid only" claim (Lasso
  has a free tier). Comparison table now says "Free tier + paid".
- **L2: README scope honesty** — "memory never leaves your machine" now
  scoped to v0.1's local-only mode. Notes that v0.2 LLM enrichment (when
  it lands) will process `raw` field of explicitly-emitted events.
- **L3: `close_all()` method** — closes all thread-local connections, not
  just the calling thread's. Fixes connection leak on shutdown.
- **L4: Dead `"all"` scope check removed** — was checking for `"all"`
  string in scopes but never used. Either support it as wildcard or
  remove; chose remove.
- **L5: `agentmemory doctor` SQLite checks** — now verifies SQLite version,
  WAL mode support, JSON1 extension, and BLAKE2b availability.

### New MCP tool

- `verify_audit_integrity` — returns integrity report for the audit log
  chain. Detects tampering via invalid HMAC signatures or broken
  prev_hash linkage.

### Tests

- Added 15 new security tests covering each fix above (C1-C4, H1-H6, M1-M5)
- Total: 41 unit tests, all passing
- 3-agent end-to-end test still passes
- MCP server subprocess test still passes (10/10)

### Known limitations carried over from v0.1

- Embeddings: BLAKE2b hashing placeholder (not semantic). Swap via `embed_fn=`.
- Vector search: keyword-based (sqlite-vec integration on v0.3 roadmap).
- Judge: rules + heuristics handle ~90% of cases. 10% saved with `tagged` flag for review.
- Passive capture: not implemented (by design).
- TTL: stored per-fact but not actively swept. Active forgetting handles equivalent cleanup.

### Breaking changes

- `MemoryStore(encryption_key=...)` is a new optional parameter — existing
  code works unchanged (encryption is opt-in).
- `policy.check()` now takes optional `agent_type` parameter. Existing
  callers work unchanged (uses bound agent_type by default).
- Custom policies that relied on the suffix-form `when:` syntax
  (`amount_le: 1000`) still work but emit a deprecation warning. Migrate
  to nested form (`amount: {le: 1000}`) before v0.3.
- Custom policies that allowed actions NOT in the agent_type whitelist
  will now be denied. To extend the whitelist, edit `ACTION_WHITELIST` in
  `policy.py` (or open an issue requesting the action be added).

## [0.1.0] - 2025-10-30

### Added — initial release

**Core memory system**
- Three-tier memory: `core` (always injected), `recall` (recent context), `archival` (long-term searchable)
- Single portable SQLite file — zero external database dependencies
- Update-not-append via `superseded` flag (no contradictory facts over time)
- Multi-tenant isolation via `agent_id` scoping on every operation
- Thread-local connections for safe concurrent MCP worker access
- BLAKE2b-based deterministic embedder (survives server restarts — Python's `hash()` does not)
- Schema versioning for future migrations

**Brain-inspired memory mechanisms**
- Salience scoring (4-factor: outcome severity + prediction error + magnitude + schema fit)
- Synaptic tagging (`tagged` flag) — fragile until consolidated
- Sleep consolidation (background job: promote reinforced tagged facts, decay the rest)
- Active forgetting (decay + prune unused low-importance facts)
- Recall reinforcement (spacing effect — `recall_count` strengthens frequently-recalled facts)
- Consolidation deadlines per tagged fact (24h window by default)

**Layered policy engine**
- Four built-in packs: `_base` (deny-all baseline), `coding`, `study`, `financial`
- Layered resolution: `builtin:_base` → `builtin:{type}` → `custom:{type}` → `custom:{agent_id}`
- Custom policies extend (do not replace) built-ins — empty custom layers are skipped
- Defense-in-depth: deny rules in `_base` are absolute, cannot be weakened by custom policies
- `when` conditions support equality, list-membership, and numeric operators (`_le`, `_ge`, `_lt`, `_gt`)
- Default-deny, fail-closed semantics throughout
- Tiered authorization example: financial pack supports amount-based payment rules

**Event-based memory capture**
- `AgentEvent` dataclass — structured unit of memory input
- `EventEmitter` SDK with helpers for coding, study, and financial agents
- `emit()` generic method for custom actions
- Every event flows through: judge → policy gate → dedup → store → audit

**Judge (no LLM required)**
- Layer 1: `RulesJudge` — deterministic per-action rules (handles ~80% of cases)
- Layer 2: `HeuristicEnricher` — regex patterns on raw field (catches recurring, refactor, security, flaky, surprise cases)
- Layer 3: `SalienceScorer` — 4-factor brain-inspired score → tagged vs consolidated
- Three layers compose in `Judge.evaluate()` — no LLM, no API costs, no network calls
- Optional `history_lookup` callable for prediction-error scoring (pass in agent's past success count)

**Audit log (compliance-grade)**
- Append-only enforced by SQLite triggers (UPDATE and DELETE both fail)
- Hash-chained entries (each entry references previous entry's hash) — tamper-evident
- Every operation logged: `remember`, `recall`, `forget`, `permission_check`, `consolidation`, `active_forgetting`
- `coverage_manifest()` reports what was actually checked, NOT a safety claim
- Soft-delete by default (`forget()` marks superseded, preserves audit trail)

**MCP server**
- 8 tools exposed: `remember_event`, `recall`, `check_permission`, `forget`, `audit_recent`, `coverage_manifest`, `consolidate`, `list_policies`
- stdio transport (compatible with Claude Desktop, Cursor, any MCP client)
- `agent_id` NEVER a tool parameter — bound at connection time via `AuthContext`
- Scope-based authorization: `memory:read`, `memory:write`, `memory:delete`, `permission:check`, `admin:consolidate`, `admin:audit`

**Auth layer**
- Token → agent_id + scopes mapping (config-file-based for v1)
- Default context for local stdio (via `AGENTMEMORY_AGENT_ID` and `AGENTMEMORY_AGENT_TYPE` env vars)
- Multi-tenant ready (token-based authentication for future HTTP transport)

**CLI**
- `agentmemory init <name> --type {coding,study,financial,default}` — scaffold a project
- `agentmemory serve` — run the MCP server
- `agentmemory policy list` — show loaded policy layers
- `agentmemory policy show --agent-id X --agent-type Y` — show effective policy with sample action resolutions
- `agentmemory policy customize <name>` — scaffold a custom override
- `agentmemory policy validate` — validate all policy YAML files
- `agentmemory doctor` — health check (Python version, dependencies, policy packs)

**Documentation & infrastructure**
- README with architecture diagram, comparison table, 30-sec quickstart
- Dockerfile for one-command self-host
- Examples folder with Claude Desktop config, Cursor config, basic Python script
- 26 unit tests covering storage, policy, judge, emitter, consolidation, audit, and the original "single-letter variable" bug
- End-to-end 3-agent test (coding agent, JEE study agent, financial agent with tiered payment authorization)
- MCP server end-to-end test (spawns real subprocess, sends JSON-RPC, verifies 8 tools work)

### Security
- `agent_id` not exposed as tool parameter — prevents prompt-injection impersonation
- Absolute deny rules in `_base` cannot be overridden by custom policies
- Audit log tamper-evident via hash chain + append-only triggers
- Default-deny semantics: unrecognized actions fail closed

### Known limitations (documented honestly)
- Embeddings: BLAKE2b hashing placeholder (not semantic). Swap via `embed_fn=`.
- Vector search: keyword-based (sqlite-vec integration on v0.2 roadmap).
- Judge: rules + heuristics handle ~90% of cases. 10% saved with `tagged` flag for review.
- Passive capture: not implemented (by design — automatic capture has prompt-injection surface).
- TTL: stored per-fact but not actively swept. Active forgetting handles equivalent cleanup.
