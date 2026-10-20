#!/usr/bin/env python3
"""Minimal fake Codex CLI for structured-output acceptance tests."""

import json
import os
import stat
import sys
from pathlib import Path


valid_schema = False
schema = None
dir_mode = None
file_mode = None
if "--output-schema" in sys.argv:
    schema_index = sys.argv.index("--output-schema") + 1
    schema_path = Path(sys.argv[schema_index])
    if schema_path.is_file():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        valid_schema = True
        dir_mode = oct(stat.S_IMODE(schema_path.parent.stat().st_mode))
        file_mode = oct(stat.S_IMODE(schema_path.stat().st_mode))
prompt = sys.stdin.read()
prompt_flag = "Structured output required" in prompt


def untyped_const(node):
    """OpenAI strict mode rejects a schema node with `const` but no `type`."""
    if isinstance(node, dict):
        if "const" in node and "type" not in node:
            return True
        return any(untyped_const(value) for value in node.values())
    if isinstance(node, list):
        return any(untyped_const(value) for value in node)
    return False
if "GENERATED_PNG=" in prompt:
    # image mode: behave like the built-in image tool + the reporting line the bridge asks for
    import struct, uuid, zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\x40\x50\x60")) + chunk(b"IEND", b""))
    user_prompt = prompt.split("USER_IMAGE_PROMPT=", 1)[1].strip() if "USER_IMAGE_PROMPT=" in prompt else ""
    # like the real tool: this run's images go to generated_images/<thread id>/
    thread_id = str(uuid.uuid4())
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    run_folder = home / "generated_images" / thread_id
    if "FAKE_NOTHREAD" not in user_prompt:
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
    run_folder.mkdir(parents=True, exist_ok=True)
    if "FAKE_REPORT=" in user_prompt:
        reported = user_prompt.split("FAKE_REPORT=", 1)[1].split()[0]
    elif "FAKE_OUTSIDE" in user_prompt:
        outside = Path(os.getcwd()) / "outside.png"
        outside.write_bytes(png)
        reported = str(outside)
    elif "FAKE_BADPNG" in user_prompt:
        target = run_folder / "exec-bad.png"
        target.write_bytes(b"SECRET-TOKEN-1234 this is not an image")
        reported = str(target)
    elif "FAKE_TXT" in user_prompt:
        target = run_folder / "notes.txt"
        target.write_text("private", encoding="utf-8")
        reported = str(target)
    else:
        target = run_folder / f"exec-{uuid.uuid4().hex}.png"
        target.write_bytes(png)
        reported = str(target)
    if "FAKE_EARLY" in user_prompt:
        # the path only in an earlier message; the final message does not carry it
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": f"GENERATED_PNG={reported}"}}))
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}))
    else:
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Generating the requested image."}}))
        if "FAKE_NOPATH" not in user_prompt:
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": f"GENERATED_PNG={reported}"}}))
    print(json.dumps({"type": "turn.completed"}))
    raise SystemExit(0)
if isinstance(schema, dict) and (schema.get("x-fake-reject") is True or untyped_const(schema)):
    error_code = "invalid_json_schema" if "--ephemeral" in sys.argv else "missing_ephemeral"
    message = json.dumps({
        "error": {"code": error_code, "message": "schema rejected"},
        "ephemeral": "--ephemeral" in sys.argv,
    })
    print(json.dumps({"type": "error", "message": message}))
    print(json.dumps({"type": "turn.failed"}))
    raise SystemExit(1)
response = {
    "ok": valid_schema or prompt_flag,
    "had_schema": valid_schema,
    "prompt_flag": prompt_flag,
    "ephemeral": "--ephemeral" in sys.argv,
    "cwd": os.getcwd(),
    "prompt_has_schema": "matches this JSON Schema:" in prompt,
}
if valid_schema:
    response.update({"dir_mode": dir_mode, "file_mode": file_mode})
text = json.dumps(response, separators=(",", ":"))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed"}))
