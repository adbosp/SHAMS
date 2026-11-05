"""Minimal MCP server over stdio for tests: tools `echo` (read-only) and `write_note`."""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "write_note", "description": "Store a note",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}}},
]
notes = []
print("fake server starting (non-JSON line must be ignored)", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, p = msg["method"], msg.get("params", {})
    if m == "initialize":
        res = {"protocolVersion": p["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        a = p.get("arguments", {})
        if p["name"] == "echo":
            res = {"content": [{"type": "text", "text": "echo: " + a.get("text", "")}]}
        elif p["name"] == "write_note":
            notes.append(a.get("note"))
            res = {"content": [{"type": "text", "text": f"saved {len(notes)}"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "no tool"}}),
                  flush=True)
            continue
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": m}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
