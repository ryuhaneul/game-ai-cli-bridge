#!/usr/bin/env python3
# Minimal fake `grok agent stdio`: answers the bridge's ACP calls and, on session/prompt,
# emits a tool call that a PreToolUse hook denied (status=failed) followed by a text reply.
import json, os, stat, sys
from pathlib import Path

def send(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
sid = "fake-session-1"
grok_home = os.environ.get("GROK_HOME")
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    m = json.loads(line); method = m.get("method"); rid = m.get("id")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":1,"authMethods":[{"id":"cached_token"}]}})
    elif method == "authenticate":
        send({"jsonrpc":"2.0","id":rid,"result":{}})
    elif method == "session/new":
        if grok_home:
            params = m.get("params") or {}
            system_prompt = (params.get("_meta") or {}).get("systemPromptOverride", "")
            cwd = Path(params["cwd"])
            dump_path = Path(grok_home) / "fake_acp_dump.json"
            dump_path.write_text(json.dumps({
                "systemPromptOverride": system_prompt,
                "cwd": str(cwd),
                "cwd_mode": oct(stat.S_IMODE(cwd.stat().st_mode)),
                "fish_key_present": "PLAYER2_FISH_AUDIO_KEY" in os.environ,
            }), encoding="utf-8")
        send({"jsonrpc":"2.0","id":rid,"result":{"sessionId":sid}})
    elif method == "session/prompt":
        meta = {"promptId": (m.get("params",{}).get("_meta") or {}).get("promptId")}
        texts = " ".join(b.get("text", "") for b in (m.get("params", {}).get("prompt") or []) if isinstance(b, dict))
        if "FAKE_402" in texts:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":"Internal error","data":{"message":"API error (status 402 Payment Required): usage balance exhausted","http_status":402}}})
            continue
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"tool_call","toolCallId":"c1","title":"run_terminal_command","rawInput":{"command":"touch x"}},"_meta":meta}})
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"tool_call_update","toolCallId":"c1","status":"failed"},"_meta":meta}})
        reply = "Hail, traveler."
        if grok_home:
            reply_path = Path(grok_home) / "fake_acp_reply.txt"
            if reply_path.is_file() and reply_path.read_text(encoding="utf-8") == "plain":
                reply = "plain"
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":reply}},"_meta":meta}})
        send({"jsonrpc":"2.0","id":rid,"result":{"stopReason":"end_turn"}})
        # keep stdout open until the bridge terminates us (it drains late updates first)
