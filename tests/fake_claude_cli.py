#!/usr/bin/env python3
"""Minimal fake Claude CLI for structured-output acceptance tests."""

import json
import sys

prompt = sys.stdin.read()


def has_key(node, key):
    if isinstance(node, dict):
        return key in node or any(has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(has_key(value, key) for value in node)
    return False


if "--json-schema" in sys.argv:
    schema_index = sys.argv.index("--json-schema") + 1
    schema = json.loads(sys.argv[schema_index])
    if has_key(schema, "x-fake-reject"):
        # like Claude Code: "Error: --json-schema is not a valid JSON Schema: strict mode: unknown keyword"
        print('Error: --json-schema is not a valid JSON Schema: strict mode: unknown keyword: "x-fake-reject"', file=sys.stderr)
        raise SystemExit(1)
    output = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"ok":true}',
        "structured_output": {"ok": True, "schema_title": schema.get("title")},
    }
elif "Structured output required" in prompt:
    output = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps({"ok": True, "prompt_flag": True, "prompt_has_schema": "matches this JSON Schema:" in prompt}),
    }
else:
    output = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "plain result",
    }
print(json.dumps(output))
