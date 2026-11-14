"""
CLI for plug-play-memory.

Commands:
  agentmemory init <name> [--type coding|study|financial|default]
      Scaffold a new project directory with config + policies.

  agentmemory serve [--db memory.db] [--policies policies/custom] [--config config.yaml]
      Run the MCP server (stdio transport) for Claude Desktop / Cursor.

  agentmemory policy list
      Show all loaded policy layers.

  agentmemory policy show --agent-id <id> --agent-type <type>
      Show effective policy for an agent (resolved across all layers).

  agentmemory policy customize <name>
      Scaffold an empty custom policy file in policies/custom/.

  agentmemory policy validate
      Validate all policy YAML files for syntax errors.

  agentmemory doctor
      Check that everything is set up correctly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import yaml

from .policy import PolicyEngine


TEMPLATE_CONFIG = """\
# plug-play-memory configuration
db_path: memory.db

auth:
  enabled: true
  default_agent_id: local
  default_agent_type: coding
  default_scopes:
    - memory:read
    - memory:write
    - memory:delete
    - permission:check
    - admin:consolidate
    - admin:audit
  # tokens:
  #   - id: token-coder-1
  #     token: sk-coder-1-abc123...
  #     agent_id: coder-1
  #     agent_type: coding
  #     scopes: [memory:read, memory:write, permission:check]
"""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    project_dir = Path(args.name)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "policies").mkdir(exist_ok=True)
    (project_dir / "policies" / "custom").mkdir(exist_ok=True)

    config_path = project_dir / "config.yaml"
    if not config_path.exists():
        # Customize template with chosen agent_type
        cfg = TEMPLATE_CONFIG
        if args.type and args.type != "coding":
            cfg = cfg.replace("default_agent_type: coding",
                              f"default_agent_type: {args.type}")
        if args.agent_id:
            cfg = cfg.replace("default_agent_id: local",
                              f"default_agent_id: {args.agent_id}")
        config_path.write_text(cfg)

    # .gitignore for the SQLite db
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*.db\n*.db-wal\n*.db-shm\n__pycache__/\n*.pyc\n")

    print(f"Initialized plug-play-memory project in ./{project_dir}/")
    print(f"  {project_dir}/config.yaml")
    print(f"  {project_dir}/policies/custom/   (your overrides go here)")
    print(f"  {project_dir}/.gitignore")
    print()
    print(f"Next steps:")
    print(f"  cd {project_dir}")
    print(f"  agentmemory serve")
    print()
    print(f"To use with Claude Desktop, add to claude_desktop_config.json:")
    print(f'''  {{
    "mcpServers": {{
      "plug-play-memory": {{
        "command": "agentmemory",
        "args": ["serve", "--db", "memory.db", "--config", "config.yaml"]
      }}
    }}
  }}''')


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> None:
    from .server import build_server
    if args.agent_id:
        os.environ["AGENTMEMORY_AGENT_ID"] = args.agent_id
    if args.agent_type:
        os.environ["AGENTMEMORY_AGENT_TYPE"] = args.agent_type
    server = build_server(
        db_path=args.db,
        custom_policy_dir=args.policies,
        auth_config_path=args.config,
    )
    server.run(transport="stdio")


# ---------------------------------------------------------------------------
# policy list / show / customize / validate
# ---------------------------------------------------------------------------

def cmd_policy_list(args: argparse.Namespace) -> None:
    policy = PolicyEngine(custom_dir=args.policies)
    print("Loaded policy layers:")
    for layer in policy.list_layers():
        print(f"  {layer}")


def cmd_policy_show(args: argparse.Namespace) -> None:
    policy = PolicyEngine(custom_dir=args.policies)
    policy.set_agent_type(args.agent_id, args.agent_type)
    print(f"Effective policy for agent_id={args.agent_id}, "
          f"agent_type={args.agent_type}")
    print()
    print(f"Layers evaluated (in order):")
    for src in policy.list_layers():
        print(f"  {src}")
    print()
    print("Action resolutions (sample):")
    sample_actions = [
        ("read_memory", {}),
        ("write_memory", {"category": "decision"}),  # need category for policy match
        ("write_memory", {"category": "transient"}),  # transient usually denied
        ("delete_memory", {}),
        ("recall_memory", {}),
        ("send_email", {}),
        ("make_purchase", {}),
        ("execute_code", {}),
        ("access_credentials", {}),
        ("delete_database", {}),
        ("make_payment", {"approved_by_user": True, "amount": 1000}),
    ]
    print(f"  {'ACTION':<28} {'CONTEXT':<32} {'ALLOWED':<8} REASON")
    print(f"  {'-'*28} {'-'*32} {'-'*8} {'-'*40}")
    for action, ctx in sample_actions:
        d = policy.check(args.agent_id, action, ctx)
        allowed_str = "yes" if d.allowed else "NO"
        ctx_str = str(ctx)[:30] if ctx else "-"
        print(f"  {action:<28} {ctx_str:<32} {allowed_str:<8} {d.reason[:60]}")


def cmd_policy_customize(args: argparse.Namespace) -> None:
    custom_dir = Path(args.policies) / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    # Validate name — no path separators, no reserved names
    if not args.name or not args.name.replace("_", "").replace("-", "").isalnum():
        print(f"ERROR: name must be alphanumeric (with _ or - only)")
        sys.exit(1)
    if args.name in ("_base", "coding", "study", "financial", "default"):
        print(f"ERROR: cannot override built-in pack name '{args.name}'")
        sys.exit(1)

    target = custom_dir / f"{args.name}.yaml"

    # Path-traversal safety: resolved target must be inside custom_dir
    try:
        target_resolved = target.resolve()
        custom_resolved = custom_dir.resolve()
        if not str(target_resolved).startswith(str(custom_resolved) + os.sep):
            print(f"ERROR: name resolves outside custom/ directory")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: invalid name: {e}")
        sys.exit(1)

    if target.exists():
        print(f"Already exists: {target}")
        return
    target.write_text(
        f"""# Custom override for '{args.name}'.
# Extends (does NOT replace) the built-in '{args.name}' pack.
# Rules here ADD to or OVERRIDE matching built-in rules.
# Deny rules in builtin/_base.yaml are absolute and cannot be weakened.

name: {args.name}
description: Custom override for {args.name}.
default: deny

rules:
  # Example: allow a new action
  # - action: my_custom_action
  #   allow: true

  # Example: tighten an existing rule (deny wins)
  # - action: sync_to_flashcard_app
  #   allow: false
"""
    )
    print(f"Created: {target}")
    print(f"Edit it, then restart the server.")


def cmd_policy_validate(args: argparse.Namespace) -> None:
    custom_dir = Path(args.policies) / "custom"
    builtin_dir = Path(__file__).parent / "policies" / "builtin"
    all_paths = sorted(builtin_dir.glob("*.yaml"))
    if custom_dir.exists():
        all_paths += sorted(custom_dir.glob("*.yaml"))
    errors = 0
    for path in all_paths:
        try:
            spec = yaml.safe_load(path.read_text())
            if spec is None:
                spec = {}
            if not isinstance(spec, dict):
                raise ValueError("top-level must be a mapping")
            rules = spec.get("rules", [])
            if rules is None:
                rules = []
            if not isinstance(rules, list):
                raise ValueError("'rules' must be a list")
            for r in rules:
                if "action" not in r:
                    raise ValueError(f"rule missing 'action': {r}")
            print(f"  OK   {path}")
        except Exception as e:
            print(f"  FAIL {path}: {e}")
            errors += 1
    if errors:
        print(f"\n{errors} validation error(s).")
        sys.exit(1)
    print(f"\nAll policy files valid.")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> None:
    print("plug-play-memory doctor")
    print("=" * 50)
    ok = True

    # Python version
    py_version = sys.version.split()[0]
    if sys.version_info >= (3, 10):
        print(f"  Python: {py_version:<24} OK")
    else:
        print(f"  Python: {py_version:<24} FAIL (need 3.10+)")
        ok = False

    # PyYAML
    try:
        import yaml
        print(f"  PyYAML: {yaml.__version__:<24} OK")
    except ImportError:
        print(f"  PyYAML: not installed{'':<14} FAIL")
        ok = False

    # MCP
    try:
        import mcp
        version = getattr(mcp, "__version__", "installed")
        print(f"  MCP: {version:<24} OK")
    except ImportError:
        print(f"  MCP: not installed{'':<16} FAIL (run: pip install 'mcp>=1.20.0')")
        ok = False

    # SQLite version + features (L5 fix)
    try:
        import sqlite3
        sqlite_version = sqlite3.sqlite_version
        print(f"  SQLite: {sqlite_version:<23} OK")
        # Test WAL mode support
        test_db = ":memory:"
        conn = sqlite3.connect(test_db)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            print(f"    SQLite WAL mode: not supported{'':<10} FAIL")
            ok = False
        # Test JSON1
        try:
            conn.execute("SELECT json_extract('{}', '$.x')")
        except sqlite3.OperationalError:
            print(f"    SQLite JSON1: not supported{'':<11} FAIL")
            ok = False
        # Test BLAKE2b (always available in Python's hashlib, but check)
        import hashlib
        try:
            hashlib.blake2b(b"test")
        except Exception:
            print(f"    BLAKE2b: not available{'':<16} FAIL")
            ok = False
        conn.close()
    except Exception as e:
        print(f"  SQLite: error ({e})")
        ok = False

    # Policy packs
    builtin_dir = Path(__file__).parent / "policies" / "builtin"
    expected = ["_base.yaml", "coding.yaml", "study.yaml", "financial.yaml"]
    for name in expected:
        p = builtin_dir / name
        if p.exists():
            print(f"  Policy pack {name:<20}    OK")
        else:
            print(f"  Policy pack {name:<20}    FAIL (missing)")
            ok = False

    # Custom policy dir
    custom_dir = Path(args.policies) / "custom"
    if custom_dir.exists():
        custom_files = list(custom_dir.glob("*.yaml"))
        print(f"  Custom policies: {len(custom_files)} file(s){'':<10} OK")
    else:
        print(f"  Custom policies: dir missing{'':<13} (will be created on first use)")

    print()
    if ok:
        print("  All checks passed.")
    else:
        print("  Some checks failed. Fix and re-run.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="agentmemory")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Scaffold a new project.")
    p_init.add_argument("name", help="Project directory name.")
    p_init.add_argument("--type", default="coding",
                        choices=["coding", "study", "financial", "default"],
                        help="Agent type for default policy.")
    p_init.add_argument("--agent-id", default=None,
                        help="Default agent_id to use.")
    p_init.set_defaults(func=cmd_init)

    # serve
    p_serve = sub.add_parser("serve", help="Run the MCP server (stdio).")
    p_serve.add_argument("--db", default="memory.db")
    p_serve.add_argument("--policies", default="policies/custom",
                         help="Path to custom policy directory.")
    p_serve.add_argument("--config", default="config.yaml",
                         help="Path to auth config YAML.")
    p_serve.add_argument("--agent-id", default=None)
    p_serve.add_argument("--agent-type", default=None)
    p_serve.set_defaults(func=cmd_serve)

    # policy
    p_policy = sub.add_parser("policy", help="Policy management.")
    p_sub = p_policy.add_subparsers(dest="policy_command", required=True)

    p_list = p_sub.add_parser("list", help="List loaded policy layers.")
    p_list.add_argument("--policies", default="policies/custom")
    p_list.set_defaults(func=cmd_policy_list)

    p_show = p_sub.add_parser("show", help="Show effective policy for an agent.")
    p_show.add_argument("--agent-id", required=True)
    p_show.add_argument("--agent-type", required=True,
                        choices=["coding", "study", "financial", "default"])
    p_show.add_argument("--policies", default="policies/custom")
    p_show.set_defaults(func=cmd_policy_show)

    p_cust = p_sub.add_parser("customize", help="Scaffold a custom override.")
    p_cust.add_argument("name", help="Name of the policy to override "
                                      "(e.g. 'study', 'coding').")
    p_cust.add_argument("--policies", default="policies")
    p_cust.set_defaults(func=cmd_policy_customize)

    p_val = p_sub.add_parser("validate", help="Validate policy files.")
    p_val.add_argument("--policies", default="policies")
    p_val.set_defaults(func=cmd_policy_validate)

    # doctor
    p_doc = sub.add_parser("doctor", help="Check setup is correct.")
    p_doc.add_argument("--policies", default="policies")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
