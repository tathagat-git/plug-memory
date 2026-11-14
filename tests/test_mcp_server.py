"""
MCP server end-to-end test.

Spawns the MCP server as a subprocess, sends JSON-RPC initialize + tools/list
+ tools/call requests over stdio, and verifies the responses.

This proves the MCP server is actually usable from Claude Desktop / Cursor.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


async def main():
    # Set up a test project
    test_dir = Path(tempfile.mkdtemp(prefix="mcp_test_"))
    db_path = test_dir / "memory.db"
    config_path = test_dir / "config.yaml"
    config_path.write_text(
        f"""db_path: {db_path}
auth:
  enabled: true
  default_agent_id: coder-1
  default_agent_type: coding
  default_scopes:
    - memory:read
    - memory:write
    - memory:delete
    - permission:check
    - admin:consolidate
    - admin:audit
"""
    )

    # Spawn the MCP server
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "plug_play_memory.server",
        "--db", str(db_path),
        "--config", str(config_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "AGENTMEMORY_AGENT_ID": "coder-1",
             "AGENTMEMORY_AGENT_TYPE": "coding"},
    )

    next_id = 1

    async def send(method, params=None):
        nonlocal next_id
        msg_id = next_id
        next_id += 1
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            req["params"] = params
        data = json.dumps(req) + "\n"
        proc.stdin.write(data.encode())
        await proc.stdin.drain()
        # Read response lines until we get our id
        while True:
            line = await proc.stdout.readline()
            if not line:
                return None
            try:
                resp = json.loads(line.decode())
                if resp.get("id") == msg_id:
                    return resp
            except json.JSONDecodeError:
                continue

    print("\n=== MCP Server End-to-End Test ===\n")

    # 1. Initialize
    print("1. initialize...")
    resp = await send("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    })
    assert "result" in resp, f"initialize failed: {resp}"
    server_info = resp["result"].get("serverInfo", {})
    print(f"   Server: {server_info.get('name', '?')} v{server_info.get('version', '?')}")
    print(f"   OK")

    # Send initialized notification (no response expected)
    proc.stdin.write((json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }) + "\n").encode())
    await proc.stdin.drain()

    # 2. List tools
    print("\n2. tools/list...")
    resp = await send("tools/list", {})
    tools = resp["result"]["tools"]
    print(f"   Found {len(tools)} tools:")
    for t in tools:
        print(f"     - {t['name']}")
    expected_tools = {"remember_event", "recall", "check_permission",
                      "forget", "audit_recent", "coverage_manifest",
                      "consolidate", "list_policies"}
    found = {t["name"] for t in tools}
    assert expected_tools.issubset(found), f"missing tools: {expected_tools - found}"
    print(f"   OK — all 8 expected tools present")

    # 3. Call remember_event with a decision
    print("\n3. tools/call remember_event (decision_made)...")
    resp = await send("tools/call", {
        "name": "remember_event",
        "arguments": {
            "action": "decision_made",
            "inputs": {"topic": "auth"},
            "outputs": {"choice": "use JWT",
                        "reason": "simpler than sessions"},
            "outcome": "success",
            "raw": "Decided to use JWT for auth — simpler than sessions",
        },
    })
    result_text = resp["result"]["content"][0]["text"]
    result = json.loads(result_text)
    print(f"   saved={result.get('saved')}, tier={result.get('tier')}, "
          f"importance={result.get('importance')}, salience={result.get('salience_score')}")
    print(f"   summary: {result.get('summary')}")
    assert result["saved"] is True, f"expected saved=True, got: {result}"
    print(f"   OK")

    # 4. Call remember_event with routine action (should be discarded)
    print("\n4. tools/call remember_event (routine test_run — should discard)...")
    resp = await send("tools/call", {
        "name": "remember_event",
        "arguments": {
            "action": "test_run",
            "inputs": {"suite": "tests/", "passed": 10, "failed": 0},
            "outcome": "success",
        },
    })
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   saved={result.get('saved')}, reason={result.get('reason')}")
    assert result["saved"] is False, f"routine test should be discarded: {result}"
    print(f"   OK — routine event correctly discarded")

    # 5. Call check_permission — should be allowed for read_memory
    print("\n5. tools/call check_permission (read_memory)...")
    resp = await send("tools/call", {
        "name": "check_permission",
        "arguments": {"action": "read_memory"},
    })
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   allowed={result['allowed']}, reason={result['reason']}")
    assert result["allowed"] is True
    print(f"   OK")

    # 6. Call check_permission — should be DENIED for access_credentials
    print("\n6. tools/call check_permission (access_credentials — should DENY)...")
    resp = await send("tools/call", {
        "name": "check_permission",
        "arguments": {"action": "access_credentials"},
    })
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   allowed={result['allowed']}, reason={result['reason']}")
    assert result["allowed"] is False
    print(f"   OK — absolute deny in _base layer enforced")

    # 7. Call recall — should find the decision we just saved
    print("\n7. tools/call recall (auth)...")
    resp = await send("tools/call", {
        "name": "recall",
        "arguments": {"query": "auth JWT", "tier": "archival", "limit": 5},
    })
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   core: {len(result['core'])} facts")
    print(f"   results: {len(result['results'])} facts")
    for r in result["results"]:
        print(f"     - {r['value']} (importance={r['importance']}, salience={r['salience_score']})")
    assert len(result["results"]) >= 1, "expected at least 1 result"
    print(f"   OK")

    # 8. Call coverage_manifest
    print("\n8. tools/call coverage_manifest...")
    resp = await send("tools/call", {"name": "coverage_manifest", "arguments": {}})
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   total checks: {result['total_permission_checks']}")
    print(f"   actions checked: {result['actions_checked']}")
    for action, counts in result["counts_by_action"].items():
        print(f"     {action}: allowed={counts['allowed']}, denied={counts['denied']}")
    print(f"   OK")

    # 9. Call list_policies
    print("\n9. tools/call list_policies...")
    resp = await send("tools/call", {"name": "list_policies", "arguments": {}})
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   agent_id: {result['agent_id']}, agent_type: {result['agent_type']}")
    print(f"   layers: {result['layers']}")
    print(f"   OK")

    # 10. Call consolidate
    print("\n10. tools/call consolidate...")
    resp = await send("tools/call", {"name": "consolidate", "arguments": {}})
    result = json.loads(resp["result"]["content"][0]["text"])
    print(f"   consolidation: {result['consolidation']}")
    print(f"   forgetting: {result['forgetting']}")
    print(f"   OK")

    print("\n=== ALL MCP TESTS PASSED ===\n")

    proc.terminate()
    await proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
