#!/usr/bin/env python3
"""Game AI CLI Bridge: a local Player2-compatible API served by Claude Code / Codex / Grok Build CLI."""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import http.client
import io
import ipaddress
import json
import math
import os
import platform
import queue
import re
import signal
import shutil
import socket
import stat
import subprocess
import struct
import sys
import threading
import time
import uuid
import zlib
from contextlib import contextmanager

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

VERSION = "0.4.17"
IMAGE_MAX_REFS = 8  # reference images accepted per image edit request (all providers)
OUTPUT_READ_RETRY_SECONDS = 15.0  # a freshly written output.png can be locked briefly (antivirus scan) on Windows
CLI_EVENT_DUMP_DIR: Path | None = None  # set by --dump-cli-events; raw CLI event lines are written there


def open_cli_event_dump(label: str) -> Any:
    """Opt-in diagnostics: one file per CLI run with the raw JSON lines the CLI printed. The lines
    contain request content (prompts, agent messages, command lines), so this is off by default."""
    if CLI_EVENT_DUMP_DIR is None:
        return None
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:40] or "cli"
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{token}-{uuid.uuid4().hex[:8]}.jsonl"
    try:
        CLI_EVENT_DUMP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            CLI_EVENT_DUMP_DIR / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "a", encoding="utf-8")
    except OSError as exc:
        print(f"[{iso_now()}] cli_event_dump=failed error={type(exc).__name__}", flush=True)
        return None
    print(f"[{iso_now()}] cli_event_dump={CLI_EVENT_DUMP_DIR / name}", flush=True)
    return handle


def dump_cli_line(handle: Any, line: str) -> None:
    if handle is None:
        return
    try:
        handle.write(line + "\n")
        handle.flush()
    except (OSError, ValueError):  # ValueError: a reader thread wrote after the run closed the file
        pass


def dump_omitted_line(handle: Any, limit: int) -> None:
    dump_cli_line(handle, json.dumps({"dump_omitted": f"line longer than {limit} characters"}))


def close_cli_event_dump(handle: Any) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except (OSError, ValueError):
        pass
GROK_EDIT_MAX_REFS = 3  # Grok Imagine edit accepts up to 3 reference images
TTS_SAMPLE_RATES = {8000, 16000, 24000, 32000, 44100}  # Fish wav/pcm sample rates
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4315
PLAYER2_HOST = "127.0.0.1"
PLAYER2_PORT = 4315
DEFAULT_TIMEOUT = 300
FISH_BASE_URL = "https://api.fish.audio"
FISH_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
FISH_VOICES_CACHE_TTL = 60.0
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "bridge-config.json"
DEFAULT_SYSTEM = (
    "You are the language model behind a local Player2-compatible API used by a game or mod. "
    "The input is a role-tagged transcript. Follow the SYSTEM instructions it contains and "
    "return only the assistant reply the game expects, in the language the transcript uses. "
    "Do not mention this bridge, coding tools, or that you are an AI unless the transcript "
    "explicitly requires it."
)
GROK_DENY_TOOLS_ENV = "PLAYER2_CLI_BRIDGE_DENY_TOOLS"
GROK_CHAT_TOOL_RULE = (
    "Tools are disabled for this session: never call any tool, never read files, list directories, "
    "or run commands. Answer directly with the assistant text only."
)
GROK_IMAGE_SYSTEM_PROMPT = (
    "You are an image generation backend for a game. The user message contains an image prompt "
    "and zero or more attached reference images. On your very first turn, call the image_edit tool "
    "exactly once if reference images are attached (pass every attached image as [Image #N] references "
    "and use the user's prompt verbatim), or call image_gen exactly once if none are attached. "
    "Pass aspect_ratio {aspect_ratio} to the tool. "
    "Do not read files, list directories, run shell commands, load skills, inspect the attachments, "
    "or view the result. After the tool completes, reply with the single word DONE."
)
BRIDGE_GET_PATHS = frozenset({
    "/", "/health", "/v1/health", "/api/version", "/api/tags", "/v1/models",
})
BRIDGE_POST_PATHS = frozenset({
    "/api/show", "/v1/embeddings", "/embeddings",
    "/v1/image/generate", "/image/generate", "/v1/image/edit", "/image/edit",
    "/api/chat", "/api/generate", "/v1/chat/completions", "/chat/completions",
})
TTS_SPEAK_PATHS = frozenset({"/v1/tts/speak", "/tts/speak"})
TTS_STREAM_PATHS = frozenset({"/v1/tts/stream", "/tts/stream"})
TTS_VOICES_PATHS = frozenset({"/v1/tts/voices", "/tts/voices"})
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 500, code: str = "bridge_error", detail: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail  # fixed machine token for diagnostics, never request content


class FishFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FishNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


FISH_OPENER = build_opener(FishNoRedirect)


class GrokMediaOutput(NamedTuple):
    output_type: str
    path: Path
    filename: str
    session_folder: str


def classify_grok_media_tool_failure(raw_output: Any) -> str:
    if not isinstance(raw_output, dict) or raw_output.get("error") != "tool_execution_failed":
        return "image_tool_failure_unclassified"
    message = raw_output.get("message")
    if not isinstance(message, str) or len(message) > 4096:
        return "image_tool_failure_unclassified"
    http_match = re.match(r"^Image edit failed with HTTP ([1-5][0-9]{2})(?:\s|:)", message)
    if http_match:
        return f"image_api_http_{http_match.group(1)}"
    if message.startswith("image_edit requires at least one reference image."):
        return "image_reference_missing"
    if message.startswith("image reference "):
        if " matches no image attached to this message." in message:
            return "image_attachment_missing"
        if " does not match any attached image." in message:
            return "image_attachment_index_invalid"
    prefixes = (
        ("image reference not readable:", "image_reference_unreadable"),
        ("image reference contained no data", "image_reference_empty"),
        ("could not detect image format for reference", "image_reference_format"),
        ("image reference is too large to process ", "image_reference_too_large"),
        ("failed to decode image reference", "image_reference_decode"),
        ("could not compress image reference small enough for Imagine API:", "image_reference_compress"),
        ("Image edit API request failed:", "image_api_transport"),
        ("Failed to read image edit response body:", "image_api_response_read"),
        ("Failed to parse image edit response:", "image_api_response_parse"),
        ("Image edit returned no image data.", "image_api_no_data"),
        ("Failed to decode base64 image data:", "image_api_base64_decode"),
    )
    for prefix, code in prefixes:
        if message.startswith(prefix):
            return code
    return "image_tool_execution_failed"


GROK_IMAGE_ASPECT_RATIOS = frozenset({
    "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2",
    "19.5:9", "9:19.5", "20:9", "9:20", "auto",
})


def summarize_grok_image_edit_input(raw_input: Any, expected_tokens: tuple[str, ...]) -> str:
    if not isinstance(raw_input, dict) or set(raw_input) - {"prompt", "image", "aspect_ratio"}:
        return "invalid"
    prompt = raw_input.get("prompt")
    images = raw_input.get("image")
    aspect_ratio = raw_input.get("aspect_ratio", "auto")
    if (
        not isinstance(prompt, str) or not prompt
        or not isinstance(images, list) or tuple(images) != expected_tokens
        or not isinstance(aspect_ratio, str) or aspect_ratio not in GROK_IMAGE_ASPECT_RATIOS
    ):
        return "invalid"
    return f"valid refs={len(images)} aspect_ratio={aspect_ratio}"


def load_bridge_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("설정파일을 찾지 못했습니다", 500, "config_missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("설정파일을 읽지 못했습니다", 500, "config_invalid") from exc
    allowed_root = {"port", "player2", "claude", "codex", "grok", "image", "tts"}
    if not isinstance(raw, dict) or set(raw) - allowed_root:
        raise BridgeError("설정파일 root key가 잘못됐습니다", 500, "config_invalid")

    result: dict[str, Any] = {}

    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise BridgeError("설정파일 port는 1~65535 정수여야 합니다", 500, "config_invalid")
    result["port"] = port

    player2 = raw.get("player2", {})
    if not isinstance(player2, dict) or set(player2) - {"port", "passthrough", "allow"}:
        raise BridgeError("설정파일 player2 key가 잘못됐습니다", 500, "config_invalid")
    player2_port = player2.get("port", PLAYER2_PORT)
    if isinstance(player2_port, bool) or not isinstance(player2_port, int) or not (1 <= player2_port <= 65535):
        raise BridgeError("설정파일 player2.port는 1~65535 정수여야 합니다", 500, "config_invalid")
    passthrough = player2.get("passthrough", "all")
    if not isinstance(passthrough, str) or passthrough not in {"all", "none", "list"}:
        raise BridgeError("설정파일 player2.passthrough가 잘못됐습니다", 500, "config_invalid")
    allow = player2.get("allow", ["/v1/tts/", "/v1/stt/"])
    if not isinstance(allow, list) or any(
        not isinstance(prefix, str) or not prefix.startswith("/") for prefix in allow
    ):
        raise BridgeError("설정파일 player2.allow는 /로 시작하는 문자열 배열이어야 합니다", 500, "config_invalid")
    result["player2"] = {"port": player2_port, "passthrough": passthrough, "allow": allow}

    for provider in ("claude", "codex"):
        section = raw.get(provider, {})
        if not isinstance(section, dict) or set(section) - {"model", "effort"}:
            raise BridgeError(f"설정파일 {provider}에는 model과 effort만 사용할 수 있습니다", 500, "config_invalid")
        normalized: dict[str, str | None] = {}
        for key in ("model", "effort"):
            value = section.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise BridgeError(f"설정파일 {provider}.{key}는 문자열 또는 null이어야 합니다", 500, "config_invalid")
            normalized[key] = value.strip() if isinstance(value, str) else None
        result[provider] = normalized

    grok = raw.get("grok", {})
    if not isinstance(grok, dict) or set(grok) - {"model", "effort"}:
        raise BridgeError("설정파일 grok에는 model과 effort만 사용할 수 있습니다", 500, "config_invalid")
    grok_model = grok.get("model", "grok-4.7")
    grok_effort = grok.get("effort", "high")
    if not isinstance(grok_model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", grok_model):
        raise BridgeError("설정파일 grok.model이 잘못됐습니다", 500, "config_invalid")
    if grok_effort not in {"low", "medium", "high", "xhigh"}:
        raise BridgeError("설정파일 grok.effort가 잘못됐습니다", 500, "config_invalid")
    result["grok"] = {"model": grok_model, "effort": grok_effort}

    image = raw.get("image", {})
    allowed_image = {"provider", "fallback_provider", "width", "height"}
    if not isinstance(image, dict) or set(image) - allowed_image:
        raise BridgeError("설정파일 image key가 잘못됐습니다", 500, "config_invalid")
    image_provider = image.get("provider", "codex")
    fallback_provider = image.get("fallback_provider")
    if image_provider not in {"codex", "grok"}:
        raise BridgeError("설정파일 image.provider가 잘못됐습니다", 500, "config_invalid")
    if fallback_provider not in {None, "codex", "grok"} or fallback_provider == image_provider:
        raise BridgeError("설정파일 image.fallback_provider가 잘못됐습니다", 500, "config_invalid")
    normalized_image: dict[str, Any] = {
        "provider": image_provider,
        "fallback_provider": fallback_provider,
    }
    for key, default in (("width", 1100), ("height", 640)):
        value = image.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 128 or value > 4096:
            raise BridgeError(f"설정파일 image.{key}는 128~4096 정수여야 합니다", 500, "config_invalid")
        normalized_image[key] = value
    result["image"] = normalized_image

    tts = raw.get("tts", {})
    allowed_tts = {"provider", "model", "reference_id", "language", "fallback", "sample_rate"}
    if not isinstance(tts, dict) or set(tts) - allowed_tts:
        raise BridgeError("설정파일 tts key가 잘못됐습니다", 500, "config_invalid")
    tts_provider = tts.get("provider", "fish")
    if not isinstance(tts_provider, str) or tts_provider not in {"none", "fish"}:
        raise BridgeError("설정파일 tts.provider가 잘못됐습니다", 500, "config_invalid")
    tts_model = tts.get("model", "s2.1-pro-free")
    if not isinstance(tts_model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,31}", tts_model):
        raise BridgeError("설정파일 tts.model이 잘못됐습니다", 500, "config_invalid")
    tts_reference_id = tts.get("reference_id")
    if tts_reference_id is not None and (
        not isinstance(tts_reference_id, str) or not re.fullmatch(r"[0-9a-f]{32}", tts_reference_id)
    ):
        raise BridgeError("설정파일 tts.reference_id가 잘못됐습니다", 500, "config_invalid")
    tts_language = tts.get("language", "ko")
    if not isinstance(tts_language, str) or not re.fullmatch(
        r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", tts_language
    ):
        raise BridgeError("설정파일 tts.language가 잘못됐습니다", 500, "config_invalid")
    tts_fallback = tts.get("fallback", True)
    if not isinstance(tts_fallback, bool):
        raise BridgeError("설정파일 tts.fallback은 bool이어야 합니다", 500, "config_invalid")
    # Player2 clients treat pcm as 24 kHz mono 16-bit (the AI Influence mod encodes it that way).
    tts_sample_rate = tts.get("sample_rate", 24000)
    if isinstance(tts_sample_rate, bool) or tts_sample_rate not in TTS_SAMPLE_RATES:
        raise BridgeError(
            "설정파일 tts.sample_rate는 8000, 16000, 24000, 32000, 44100 중 하나여야 합니다", 500, "config_invalid"
        )
    result["tts"] = {
        "provider": tts_provider,
        "model": tts_model,
        "reference_id": tts_reference_id,
        "language": tts_language,
        "fallback": tts_fallback,
        "sample_rate": tts_sample_rate,
    }
    return result


class Settings:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.timeout = args.timeout
        self.max_body_bytes = args.max_body_mb * 1024 * 1024
        self.max_prompt_chars = args.max_prompt_chars
        self.config_path = Path(args.config).expanduser().resolve()
        config = load_bridge_config(self.config_path)
        self.port = args.port if args.port is not None else config["port"]
        self.player2_port = config["player2"]["port"]
        self.passthrough_mode = config["player2"]["passthrough"]
        self.passthrough_allow = config["player2"]["allow"]
        self.passthrough_active = self.passthrough_mode != "none" and self.port != self.player2_port
        self.provider = args.provider
        self.claude_model = config["claude"]["model"]
        self.claude_effort = config["claude"]["effort"]
        self.codex_model = config["codex"]["model"]
        self.codex_effort = config["codex"]["effort"]
        self.grok_model = config["grok"]["model"]
        self.grok_effort = config["grok"]["effort"]
        self.image_provider = config["image"]["provider"]
        self.image_fallback_provider = config["image"]["fallback_provider"]
        self.image_width = config["image"]["width"]
        self.image_height = config["image"]["height"]
        self.tts_provider = config["tts"]["provider"]
        self.tts_model = config["tts"]["model"]
        self.tts_reference_id = config["tts"]["reference_id"]
        self.tts_language = config["tts"]["language"]
        self.tts_fallback = config["tts"]["fallback"]
        self.tts_sample_rate = config["tts"]["sample_rate"]
        self.fish_api_key = os.environ.get("PLAYER2_FISH_AUDIO_KEY", "").strip()
        os.environ.pop("PLAYER2_FISH_AUDIO_KEY", None)
        self.fish_base_url = args.fish_base_url.rstrip("/")
        self.tts_active = self.tts_provider == "fish" and bool(self.fish_api_key)
        self.known_voice_ids = {self.tts_reference_id} if self.tts_reference_id else set()
        self.voices_cache: list[dict[str, str]] | None = None
        self.voices_cache_timestamp = 0.0
        self.voices_cache_lock = threading.Lock()

        if args.cli_model is not None:
            if self.provider == "claude":
                self.claude_model = args.cli_model
            elif self.provider == "codex":
                self.codex_model = args.cli_model
            else:
                self.grok_model = args.cli_model
        if args.cli_effort is not None:
            if self.provider == "claude":
                self.claude_effort = args.cli_effort
            elif self.provider == "codex":
                self.codex_effort = args.cli_effort
            else:
                self.grok_effort = args.cli_effort
        self.claude_model = pick_claude_model(None, self.claude_model)
        self.claude_effort = pick_claude_effort(self.claude_effort)
        self.codex_model = pick_codex_model(self.codex_model)
        self.codex_effort = pick_codex_effort(self.codex_effort)
        self.grok_model = pick_grok_model(self.grok_model)
        self.grok_effort = pick_grok_effort(self.grok_effort)
        self.claude_command = args.claude_command
        self.codex_command = args.codex_command
        self.grok_command = args.grok_command
        self.openai_api_key = os.environ.get("PLAYER2_OPENAI_EMBEDDING_KEY", "").strip()
        os.environ.pop("PLAYER2_OPENAI_EMBEDDING_KEY", None)
        self.openai_base_url = args.openai_base_url.rstrip("/")
        self.embedding_model = args.embedding_model
        self.jobs_dir = (
            Path(args.jobs_dir).expanduser().resolve()
            if args.jobs_dir
            else (Path(__file__).resolve().parent / "jobs")
        )
        self.keep_image_jobs = args.keep_image_jobs
        self.semaphore = threading.BoundedSemaphore(args.max_concurrency)

    def passthrough_allowed(self, path: str) -> bool:
        return self.passthrough_active and (
            self.passthrough_mode == "all"
            or any(path.startswith(prefix) for prefix in self.passthrough_allow)
        )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def print_console_only_prompt(label: str, prompt: str) -> None:
    """Show user-approved prompt text on a Windows console without stdout/stderr logging."""
    if platform.system() != "Windows":
        return
    try:
        with open("CONOUT$", "w", encoding="utf-8", errors="replace") as console:
            console.write(f"\n================ {label} PROMPT ================\n")
            console.write(prompt)
            if not prompt.endswith("\n"):
                console.write("\n")
            console.write(f"================ END {label} PROMPT ============\n")
            console.flush()
    except (OSError, UnicodeError, ValueError):
        # Console observability must never change request success/failure behavior.
        return


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    if value is None:
        return ""
    return str(value)


def split_messages(messages: Any) -> tuple[str, str]:
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty array", 400, "invalid_request")
    system_parts: list[str] = []
    transcript: list[str] = []
    labels = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "user")).lower()
        content = text_content(item.get("content"))
        if role in ("system", "developer"):
            if content:
                system_parts.append(content)
        else:
            transcript.append(f"[{labels.get(role, role.upper())}]\n{content}")
    system = "\n\n".join(system_parts).strip()
    conversation = "\n\n".join(transcript).strip()
    if not conversation and not system:
        raise BridgeError("messages contain no text", 400, "invalid_request")
    prompt = ""
    if system:
        prompt += f"[SYSTEM]\n{system}\n\n"
    prompt += conversation
    prompt += "\n\n[ASSISTANT]\n"
    return system, prompt


def pick_claude_model(requested: Any, forced: str | None) -> str:
    # Player2 may send a backend-placeholder model unrelated to Claude.
    # Provider config/CLI override owns model selection; request model is ignored.
    model = str(forced or "sonnet").lower()
    aliases = {
        "claude-haiku": "haiku",
        "claude-haiku:latest": "haiku",
        "haiku": "haiku",
        "haiku:latest": "haiku",
        "claude-sonnet": "sonnet",
        "claude-sonnet:latest": "sonnet",
        "sonnet": "sonnet",
        "sonnet:latest": "sonnet",
        "claude-opus": "opus",
        "claude-opus:latest": "opus",
        "opus": "opus",
        "opus:latest": "opus",
        "fable": "fable",
        "fable:latest": "fable",
    }
    selected = aliases.get(model)
    if selected is not None:
        return selected
    if re.fullmatch(r"claude-[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
        return model
    raise BridgeError("invalid Claude model name", 400, "invalid_model")


def pick_claude_effort(value: str | None) -> str | None:
    if value is None:
        return None
    effort = value.lower()
    if effort not in {"low", "medium", "high", "xhigh", "max", "ultracode"}:
        raise BridgeError("invalid Claude effort", 400, "invalid_effort")
    return effort


def pick_codex_model(forced: str | None) -> str | None:
    if not forced:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", forced):
        raise BridgeError("invalid Codex model name", 400, "invalid_model")
    return forced


def pick_codex_effort(value: str | None) -> str | None:
    if value is None:
        return None
    effort = value.lower()
    if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise BridgeError("invalid Codex effort", 400, "invalid_effort")
    return effort


def codex_effort_override(value: str | None) -> str | None:
    effort = pick_codex_effort(value)
    return f"model_reasoning_effort={effort}" if effort else None


def pick_grok_model(value: str | None) -> str:
    model = value or "grok-4.7"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
        raise BridgeError("invalid Grok model name", 400, "invalid_model")
    return model


def pick_grok_effort(value: str | None) -> str:
    effort = (value or "high").lower()
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise BridgeError("invalid Grok effort", 400, "invalid_effort")
    return effort


def windows_cmd_wrapper(executable: str, args: list[str]) -> list[str]:
    if platform.system() == "Windows" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline([executable, *args])]
    return [executable, *args]


def resolve_claude_launcher(configured: str) -> tuple[str, list[str]]:
    if configured != "claude":
        resolved = shutil.which(configured) or configured
        return resolved, []
    if platform.system() == "Windows":
        appdata = Path(os.environ.get("APPDATA", ""))
        candidates = [
            appdata / "npm/node_modules/@anthropic-ai/claude-code/cli.js",
            Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/cli.js",
        ]
        node = shutil.which("node")
        if node:
            for candidate in candidates:
                if candidate.is_file():
                    return node, [str(candidate)]
    resolved = shutil.which(configured)
    if not resolved:
        raise BridgeError("claude CLI를 찾지 못했습니다. Claude Code를 설치하고 로그인하세요.", 502, "cli_not_found")
    return resolved, []


def resolve_codex_launcher(configured: str) -> tuple[str, list[str]]:
    resolved = shutil.which(configured)
    if resolved:
        resolved = str(Path(resolved).resolve())
    elif Path(configured).exists():
        resolved = str(Path(configured).resolve())
    if not resolved:
        raise BridgeError("codex CLI를 찾지 못했습니다. Codex CLI를 설치하고 로그인하세요.", 502, "cli_not_found")
    return resolved, []


def grok_deny_tools_hook_path() -> Path:
    grok_home = os.environ.get("GROK_HOME", "").strip()
    base = Path(grok_home).expanduser() if grok_home else Path.home() / ".grok"
    return base / "hooks" / "player2-cli-bridge-deny-tools.json"


def ensure_grok_deny_tools_hook() -> None:
    # Grok Build's ACP stdio mode ignores --tools, --deny and --permission-mode (verified on 1.0.13),
    # so a user-global PreToolUse hook is the only hard block. The hook denies a tool call only when
    # the bridge's marker variable is present in the child environment; interactive Grok sessions
    # do not have it and are unaffected. Delete the file to uninstall.
    if platform.system() == "Windows":
        # Grok runs Windows hook commands through PowerShell (cmd only with GROK_SHELL=cmd). No "$" in
        # the command, so the runner's $VAR rewriting cannot touch it.
        command = f"if ([Environment]::GetEnvironmentVariable('{GROK_DENY_TOOLS_ENV}')) {{ exit 2 }}; exit 0"
    else:
        command = f'[ -n "${GROK_DENY_TOOLS_ENV}" ] && exit 2; exit 0'
    content = json.dumps(
        {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]}},
        indent=2,
    ) + "\n"
    path = grok_deny_tools_hook_path()
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise BridgeError("failed to install the Grok deny-tools hook", 502, "hook_install_failed") from exc
    print(f"[{iso_now()}] grok_hook=installed", flush=True)


def resolve_grok_launcher(configured: str) -> tuple[str, list[str]]:
    resolved = shutil.which(configured) or (configured if Path(configured).exists() else None)
    if not resolved and configured == "grok" and platform.system() == "Windows":
        candidate = Path.home() / ".grok/bin/grok.exe"
        if candidate.is_file():
            resolved = str(candidate)
    if not resolved:
        raise BridgeError("grok CLI를 찾지 못했습니다. Grok Build를 설치하고 로그인하세요.", 502, "cli_not_found")
    return resolved, []


SAFE_PROGRESS_EVENT_TYPES = {
    "error", "item.completed", "item.started", "response.completed",
    "response.started", "thread.started", "turn.completed", "turn.failed",
    "turn.started",
}
SAFE_DIAGNOSTIC_EVENT_VALUES = {
    "cancelled", "completed", "done", "error", "failed", "failure",
    "generation_failed", "image_generation_failed", "in_progress", "pending",
    "running", "started",
    "success", "task_failed", "tool_error", "tool_failed", "turn.failed",
}
SAFE_ACP_SESSION_UPDATES = {
    "agent_message_chunk", "agent_thought_chunk", "available_commands_update",
    "config_option_update", "current_mode_update", "plan", "session_info_update",
    "tool_call", "tool_call_update", "usage_update", "user_message_chunk",
}
SAFE_ACP_TOOL_KINDS = {"edit", "execute", "fetch", "other", "read", "search"}
SAFE_PROVIDER_ERROR_CODES = {
    "image_generation_denied", "invalid_json_schema", "request_failed", "tool_unavailable",
}
SAFE_PROGRESS_ITEM_TYPES = {
    "agent_message", "command_execution", "file_change", "mcp_tool_call",
    "message", "reasoning", "tool_call", "web_search",
}
SAFE_PROGRESS_TOOL_NAMES = {
    "generate_image", "image_edit", "image_gen", "image_generation",
    "read", "read_file", "write", "write_file",
}
MAX_CLI_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_CLI_STDERR_BYTES = 8 * 1024 * 1024
MAX_CLI_PROGRESS_LINE_CHARS = 64 * 1024
CLI_STREAM_CHUNK_CHARS = 4096


def safe_machine_token(value: Any, private_texts: tuple[str, ...] = ()) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", token):
        return None
    for private in private_texts:
        stripped = private.strip()
        if token == stripped or (len(token) >= 8 and token in private):
            return None
    return token


def safe_provider_error_code(value: Any, private_texts: tuple[str, ...] = ()) -> str | None:
    token = safe_machine_token(value, private_texts)
    return token if token in SAFE_PROVIDER_ERROR_CODES else None


def safe_error_codes(event: dict[str, Any], private_texts: tuple[str, ...] = ()) -> list[str]:
    candidates: list[str] = []
    containers = {"data", "details", "payload", "response", "result"}

    def visit(value: Any, failure_context: bool, depth: int) -> None:
        if depth > 4 or not isinstance(value, dict):
            return
        indicator_tokens = [
            safe_machine_token(value.get(key), private_texts)
            for key in ("type", "event", "kind", "status")
        ]
        local_failure = failure_context or any(
            token is not None and (
                token in SAFE_DIAGNOSTIC_EVENT_VALUES
                or "error" in token.lower()
                or "fail" in token.lower()
                or "denied" in token.lower()
                or "unavailable" in token.lower()
            )
            for token in indicator_tokens
        )
        if local_failure:
            for key in ("code", "error_code", "errorCode"):
                token = safe_provider_error_code(value.get(key), private_texts)
                if token:
                    candidates.append(token)
            message = value.get("message")
            if isinstance(message, str):
                try:
                    message_payload = json.loads(message)
                except json.JSONDecodeError:
                    message_payload = None
                if isinstance(message_payload, dict) and isinstance(message_payload.get("error"), dict):
                    token = safe_provider_error_code(message_payload["error"].get("code"), private_texts)
                    if token:
                        candidates.append(token)
        error = value.get("error")
        if isinstance(error, str):
            token = safe_provider_error_code(error, private_texts)
            if token:
                candidates.append(token)
        elif isinstance(error, dict):
            for key in ("type", "kind", "status", "error_code", "errorCode", "code"):
                token = safe_provider_error_code(error.get(key), private_texts)
                if token:
                    candidates.append(token)
            visit(error, True, depth + 1)
        for key in containers:
            visit(value.get(key), local_failure, depth + 1)

    visit(event, False, 0)
    return list(dict.fromkeys(candidates))


def emit_cli_progress(label: str, line: str, private_texts: tuple[str, ...] = ()) -> None:
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        print(f"[{iso_now()}] [{label}] unstructured_output", flush=True)
        return
    if not isinstance(event, dict):
        print(f"[{iso_now()}] [{label}] output_event", flush=True)
        return
    raw_event_type = event.get("type")
    event_type = raw_event_type if isinstance(raw_event_type, str) and raw_event_type in SAFE_PROGRESS_EVENT_TYPES else "event"
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    raw_item_type = item.get("type")
    item_type = raw_item_type if isinstance(raw_item_type, str) and raw_item_type in SAFE_PROGRESS_ITEM_TYPES else ""
    if item_type:
        raw_name = item.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name in SAFE_PROGRESS_TOOL_NAMES else ""
        detail = f"{item_type}/{name}" if name and name != item_type else item_type
        exit_code = item.get("exit_code")
        if item_type == "command_execution" and event_type == "item.completed" and isinstance(exit_code, int) and not isinstance(exit_code, bool):
            detail += f" exit={exit_code}"
        print(f"[{iso_now()}] [{label}] {event_type} {detail}", flush=True)
    elif event_type == "event":
        metadata: list[str] = []
        session_update = safe_machine_token(event.get("sessionUpdate"), private_texts)
        if session_update in SAFE_ACP_SESSION_UPDATES:
            metadata.append(f"session_update={session_update}")
        kind = safe_machine_token(event.get("kind"), private_texts)
        if kind in SAFE_ACP_TOOL_KINDS:
            metadata.append(f"kind={kind}")
        tool_name = safe_machine_token(event.get("toolName"), private_texts)
        if tool_name in SAFE_PROGRESS_TOOL_NAMES:
            metadata.append(f"tool={tool_name}")
        for key in ("event", "kind", "status"):
            if key == "kind" and kind in SAFE_ACP_TOOL_KINDS:
                continue
            token = safe_machine_token(event.get(key), private_texts)
            if token in SAFE_DIAGNOSTIC_EVENT_VALUES:
                metadata.append(f"{key}={token}")
        error_codes = safe_error_codes(event, private_texts)
        if error_codes:
            metadata.append(f"error.code={error_codes[-1]}")
        suffix = " " + " ".join(metadata) if metadata else ""
        print(f"[{iso_now()}] [{label}] event{suffix}", flush=True)
    else:
        print(f"[{iso_now()}] [{label}] {event_type}", flush=True)


MAX_OUTPUT_IMAGE_DIMENSION = 4096
MAX_DECODED_PNG_BYTES = MAX_OUTPUT_IMAGE_DIMENSION * (MAX_OUTPUT_IMAGE_DIMENSION * 4 + 1)


def validate_complete_png_data(data: bytes, max_bytes: int | None) -> bytes | None:
    if len(data) < 57 or (max_bytes is not None and len(data) > max_bytes):
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    offset = 8
    first = True
    width = height = bit_depth = color_type = 0
    row_bytes = expected_decoded = 0
    seen_palette = False
    palette_entries = 0
    seen_idat = False
    idat_ended = False
    idat_parts: list[bytes] = []
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return None
        kind = data[offset + 4:offset + 8]
        if len(kind) != 4 or any(not (65 <= value <= 90 or 97 <= value <= 122) for value in kind):
            return None
        if kind[2] & 0x20:
            return None
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:chunk_end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return None

        if first:
            if kind != b"IHDR" or length != 13:
                return None
            first = False
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", payload,
            )
            if (
                width < 1 or height < 1 or
                width > MAX_OUTPUT_IMAGE_DIMENSION or height > MAX_OUTPUT_IMAGE_DIMENSION or
                color_type not in valid_depths or bit_depth not in valid_depths[color_type] or
                compression != 0 or filter_method != 0 or interlace != 0
            ):
                return None
            row_bytes = (width * channels[color_type] * bit_depth + 7) // 8
            expected_decoded = height * (row_bytes + 1)
            if expected_decoded > MAX_DECODED_PNG_BYTES:
                return None
        elif kind == b"IHDR":
            return None
        elif kind == b"PLTE":
            if seen_palette or seen_idat or color_type in {0, 4}:
                return None
            if length < 3 or length > 768 or length % 3:
                return None
            if color_type == 3 and length // 3 > 1 << bit_depth:
                return None
            palette_entries = length // 3
            seen_palette = True
        elif kind == b"IDAT":
            if idat_ended:
                return None
            seen_idat = True
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or chunk_end != len(data) or not seen_idat:
                return None
            if color_type == 3 and not seen_palette:
                return None
            try:
                decompressor = zlib.decompressobj()
                decoded = bytearray()
                for part in idat_parts:
                    remaining = expected_decoded + 1 - len(decoded)
                    if remaining <= 0:
                        return None
                    decoded.extend(decompressor.decompress(part, remaining))
                    if len(decoded) > expected_decoded or decompressor.unconsumed_tail:
                        return None
                remaining = expected_decoded + 1 - len(decoded)
                if remaining <= 0:
                    return None
                decoded.extend(decompressor.flush(remaining))
            except zlib.error:
                return None
            if (
                len(decoded) != expected_decoded or not decompressor.eof or
                decompressor.unused_data or decompressor.unconsumed_tail
            ):
                return None
            stride = row_bytes + 1
            previous = bytearray(row_bytes)
            for row_index in range(height):
                row_offset = row_index * stride
                filter_type = decoded[row_offset]
                if filter_type > 4:
                    return None
                if color_type != 3:
                    continue

                raw_row = decoded[row_offset + 1:row_offset + stride]
                reconstructed = bytearray(row_bytes)
                for index, raw_byte in enumerate(raw_row):
                    left = reconstructed[index - 1] if index else 0
                    above = previous[index]
                    upper_left = previous[index - 1] if index else 0
                    if filter_type == 0:
                        predictor = 0
                    elif filter_type == 1:
                        predictor = left
                    elif filter_type == 2:
                        predictor = above
                    elif filter_type == 3:
                        predictor = (left + above) // 2
                    else:
                        estimate = left + above - upper_left
                        distance_left = abs(estimate - left)
                        distance_above = abs(estimate - above)
                        distance_upper_left = abs(estimate - upper_left)
                        if distance_left <= distance_above and distance_left <= distance_upper_left:
                            predictor = left
                        elif distance_above <= distance_upper_left:
                            predictor = above
                        else:
                            predictor = upper_left
                    reconstructed[index] = (raw_byte + predictor) & 0xFF

                for pixel_index in range(width):
                    bit_offset = pixel_index * bit_depth
                    byte_value = reconstructed[bit_offset // 8]
                    shift = 8 - bit_depth - bit_offset % 8
                    palette_index = (byte_value >> shift) & ((1 << bit_depth) - 1)
                    if palette_index >= palette_entries:
                        return None
                previous = reconstructed
            return data
        else:
            if seen_idat:
                idat_ended = True
            if not (kind[0] & 0x20):
                return None
        offset = chunk_end
    return None


def read_complete_png(path: Path, max_bytes: int | None, *, raise_os_error: bool = False) -> bytes | None:
    try:
        path_info = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(path_info, "st_file_attributes", 0)
        if not stat.S_ISREG(path_info.st_mode) or (reparse_flag and file_attributes & reparse_flag):
            return None
        parent = path.parent.resolve(strict=True)
        if path.resolve(strict=True).parent != parent:
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            descriptor_info = os.fstat(descriptor)
            descriptor_attributes = getattr(descriptor_info, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(descriptor_info.st_mode) or
                (reparse_flag and descriptor_attributes & reparse_flag)
            ):
                return None
            limit = max_bytes if max_bytes is not None else MAX_DECODED_PNG_BYTES
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
        finally:
            os.close(descriptor)
    except OSError:
        if raise_os_error:
            raise
        return None
    return validate_complete_png_data(data, max_bytes)


JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def validate_complete_jpeg_data(
    data: bytes, max_bytes: int | None,
) -> tuple[bytes, int, int] | None:
    if len(data) < 16 or (max_bytes is not None and len(data) > max_bytes):
        return None
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None

    offset = 2
    dimensions: tuple[int, int] | None = None
    seen_scan = False
    while offset < len(data):
        if data[offset] != 0xFF:
            return None
        marker_start = offset
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker == 0x00:
            return None
        if marker == 0xD9:
            if offset != len(data) or dimensions is None or not seen_scan:
                return None
            return data, dimensions[0], dimensions[1]
        if marker == 0xD8:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[offset:offset + 2], "big")
        if segment_length < 2:
            return None
        segment_end = offset + segment_length
        if segment_end > len(data):
            return None
        payload = data[offset + 2:segment_end]

        if marker in JPEG_SOF_MARKERS:
            if dimensions is not None or len(payload) < 6:
                return None
            height = int.from_bytes(payload[1:3], "big")
            width = int.from_bytes(payload[3:5], "big")
            components = payload[5]
            if (
                payload[0] not in {8, 12, 16}
                or width < 1 or height < 1
                or width > MAX_OUTPUT_IMAGE_DIMENSION
                or height > MAX_OUTPUT_IMAGE_DIMENSION
                or components < 1 or components > 4
                or len(payload) != 6 + 3 * components
                or width * height * 4 > MAX_DECODED_PNG_BYTES
            ):
                return None
            dimensions = (width, height)
        elif marker == 0xDA:
            if dimensions is None or len(payload) < 4:
                return None
            components = payload[0]
            if components < 1 or components > 4 or len(payload) != 4 + 2 * components:
                return None
            seen_scan = True
            offset = segment_end
            while offset < len(data):
                marker_pos = data.find(b"\xff", offset)
                if marker_pos < 0:
                    return None
                code_pos = marker_pos + 1
                while code_pos < len(data) and data[code_pos] == 0xFF:
                    code_pos += 1
                if code_pos >= len(data):
                    return None
                code = data[code_pos]
                if code == 0x00 or 0xD0 <= code <= 0xD7:
                    offset = code_pos + 1
                    continue
                offset = marker_pos
                break
            continue
        offset = segment_end
        if offset <= marker_start:
            return None
    return None


def read_bounded_regular_file(path: Path, max_bytes: int) -> bytes | None:
    try:
        path_info = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(path_info, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(path_info.st_mode)
            or (reparse_flag and file_attributes & reparse_flag)
            or path_info.st_size < 1
            or path_info.st_size > max_bytes
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            before_attributes = getattr(before, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(before.st_mode)
                or (reparse_flag and before_attributes & reparse_flag)
                or before.st_size != path_info.st_size
                or before.st_size > max_bytes
            ):
                return None
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                len(data) != before.st_size
                or len(data) > max_bytes
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return None
            return data
        finally:
            os.close(descriptor)
    except OSError:
        return None


def validate_generated_image(
    path: Path, max_bytes: int,
) -> tuple[bytes, int, int, str]:
    data = read_bounded_regular_file(path, max_bytes)
    if data is None:
        raise BridgeError("generated image file is missing or unsafe", 502, "image_output_missing")
    png = validate_complete_png_data(data, max_bytes)
    if png is not None:
        width, height = struct.unpack(">II", png[16:24])
        return png, width, height, "image/png"
    jpeg = validate_complete_jpeg_data(data, max_bytes)
    if jpeg is not None:
        return jpeg[0], jpeg[1], jpeg[2], "image/jpeg"
    raise BridgeError("generated image is not a valid PNG or JPEG", 502, "image_output_invalid")


def generated_image_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    raise BridgeError("generated image MIME is invalid", 502, "image_output_invalid")


def safe_structured_cli_error(
    lines: list[str], prompt: str, *, private_texts: tuple[str, ...] = (),
) -> str | None:
    """Keep only a bounded machine error code; provider messages are private output."""
    protected = tuple(text for text in (prompt, *private_texts) if text)
    candidates: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates.extend(safe_error_codes(event, protected))
    if not candidates:
        return None
    return list(dict.fromkeys(candidates))[-1]


CHILD_SECRET_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",

    "PLAYER2_OPENAI_EMBEDDING_KEY",
    "XAI_API_KEY",
}

SAFE_CHILD_ENV_NAMES = {
    "APPDATA", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COLORTERM", "COMSPEC",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "CURL_CA_BUNDLE",
    "GROK_HOME", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL",
    "LOCALAPPDATA", "LOGNAME", "NODE_EXTRA_CA_CERTS", "NUMBER_OF_PROCESSORS",
    "OS", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PYTHONIOENCODING", "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE", "SHELL", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TERM", "TMP", "TMPDIR", "TZ",
    "USER", "USERPROFILE", "WINDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME",
}


def sanitized_child_environment(allowed_secret_names: set[str] | None = None) -> dict[str, str]:
    allowed = allowed_secret_names or set()
    if allowed - CHILD_SECRET_ENV_NAMES:
        raise ValueError("unsupported child secret allowlist entry")
    allowed_names = SAFE_CHILD_ENV_NAMES | allowed
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() in allowed_names
    }
    return environment


def termination_wait_budget(deadline: float | None) -> float:
    if deadline is None:
        return 5.0
    return max(0.0, min(5.0, deadline - time.monotonic()))


def best_effort_root_termination(
    process: subprocess.Popen[str], deadline: float | None = None,
) -> None:
    try:
        process.kill()
    except Exception:
        pass
    wait_budget = termination_wait_budget(deadline)
    if wait_budget <= 0:
        return
    try:
        process.wait(timeout=wait_budget)
    except Exception:
        pass


def _terminate_process_tree_impl(
    process: subprocess.Popen[str], deadline: float | None = None,
) -> None:
    if platform.system() == "Windows":
        taskkill_budget = termination_wait_budget(deadline)
        if taskkill_budget <= 0:
            raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed")
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=taskkill_budget, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            best_effort_root_termination(process, deadline)
            raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed") from exc
        if result.returncode != 0:
            try:
                process.wait(timeout=0)
            except Exception:
                best_effort_root_termination(process, deadline)
            raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed")
    else:
        process_group = getattr(process, "_bridge_process_group", process.pid)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            best_effort_root_termination(process, deadline)
            raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed") from exc
    wait_budget = termination_wait_budget(deadline)
    if wait_budget <= 0:
        raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed")
    try:
        process.wait(timeout=wait_budget)
    except Exception as exc:
        best_effort_root_termination(process, deadline)
        raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed") from exc


def terminate_process_tree(
    process: subprocess.Popen[str], deadline: float | None = None,
) -> None:
    try:
        _terminate_process_tree_impl(process, deadline)
    except Exception as exc:
        try:
            setattr(process, "_bridge_tree_cleanup_failed", True)
        except Exception:
            pass
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError("CLI process-tree termination failed", 500, "cli_termination_failed") from exc
    try:
        setattr(process, "_bridge_tree_cleanup_failed", False)
    except Exception:
        pass


ACTIVE_PROCESSES: set[Any] = set()
PENDING_LAUNCHES: set[Any] = set()
ACTIVE_PROCESSES_LOCK = threading.RLock()
PROCESS_SHUTTING_DOWN = False


class PendingLaunch:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.cancelled = False
        self.process: Any | None = None
        self.error: BridgeError | None = None


def reset_process_lifecycle() -> None:
    global PROCESS_SHUTTING_DOWN
    with ACTIVE_PROCESSES_LOCK:
        if ACTIVE_PROCESSES or PENDING_LAUNCHES:
            raise BridgeError("Provider process cleanup incomplete", 500, "cli_termination_failed")
        PROCESS_SHUTTING_DOWN = False


def register_active_process(process: Any) -> None:
    with ACTIVE_PROCESSES_LOCK:
        if PROCESS_SHUTTING_DOWN:
            raise BridgeError("Bridge is shutting down", 503, "bridge_shutting_down")
        ACTIVE_PROCESSES.add(process)


def unregister_active_process(process: Any) -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.discard(process)


def unregister_inactive_process(process: Any) -> None:
    try:
        unresolved_tree = bool(getattr(process, "_bridge_tree_cleanup_failed", False))
        inactive = process.poll() is not None
    except Exception:
        unresolved_tree = True
        inactive = False
    if inactive and not unresolved_tree:
        unregister_active_process(process)


def active_process_count() -> int:
    with ACTIVE_PROCESSES_LOCK:
        return len(ACTIVE_PROCESSES)


def pending_launch_count() -> int:
    with ACTIVE_PROCESSES_LOCK:
        return len(PENDING_LAUNCHES)


def terminate_active_processes() -> None:
    global PROCESS_SHUTTING_DOWN
    with ACTIVE_PROCESSES_LOCK:
        PROCESS_SHUTTING_DOWN = True
        processes = list(ACTIVE_PROCESSES)
    failures = 0
    for process in processes:
        try:
            unresolved_tree = bool(getattr(process, "_bridge_tree_cleanup_failed", False))
            if process.poll() is None or unresolved_tree:
                terminate_process_tree(process)
        except Exception:
            try:
                setattr(process, "_bridge_tree_cleanup_failed", True)
            except Exception:
                pass
            failures += 1
        else:
            unregister_inactive_process(process)
    remaining = active_process_count() + pending_launch_count()
    if failures or remaining:
        print(
            f"[{iso_now()}] shutdown_process_cleanup=failed count={failures} remaining={remaining}",
            flush=True,
        )
        raise BridgeError("Provider process cleanup incomplete", 500, "cli_termination_failed")


def shutdown_active_processes(max_attempts: int = 3, pending_wait_timeout: float = 5.0) -> bool:
    global PROCESS_SHUTTING_DOWN
    pending_deadline = time.monotonic() + pending_wait_timeout
    with ACTIVE_PROCESSES_LOCK:
        PROCESS_SHUTTING_DOWN = True
        pending = list(PENDING_LAUNCHES)
        for ticket in pending:
            ticket.cancelled = True
    for ticket in pending:
        remaining = pending_deadline - time.monotonic()
        if remaining <= 0 or not ticket.event.wait(timeout=remaining):
            return False
    for attempt in range(max_attempts):
        try:
            terminate_active_processes()
            return True
        except BridgeError:
            if attempt + 1 < max_attempts:
                time.sleep(0.05)
    return False


def launch_process_until_deadline(
    command: list[str],
    deadline: float,
    timeout: float,
    *,
    cwd: Path | None,
    allowed_secret_env: set[str] | None,
    creation_flags: int,
    windows: bool,
    cleanup_deadline: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> Any:
    ticket = PendingLaunch()
    terminate_callback = terminate_process_tree

    def terminate_launched_process(process: Any) -> None:
        if cleanup_deadline is None:
            terminate_callback(process)
        else:
            terminate_callback(process, deadline=cleanup_deadline)
    with ACTIVE_PROCESSES_LOCK:
        if PROCESS_SHUTTING_DOWN:
            raise BridgeError("Bridge is shutting down", 503, "bridge_shutting_down")
        PENDING_LAUNCHES.add(ticket)

    def launch_worker() -> None:
        process = None
        error = None
        try:
            environment = sanitized_child_environment(allowed_secret_env)
            if extra_env:
                environment.update(extra_env)
            if time.monotonic() >= deadline:
                raise BridgeError(f"CLI timeout after {timeout}s", 504, "cli_timeout")
            process = subprocess.Popen(
                command,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                shell=False,
                cwd=str(cwd) if cwd else None,
                env=environment,
                creationflags=creation_flags,
                start_new_session=not windows,
            )
        except BridgeError as exc:
            error = exc
        except Exception:
            error = BridgeError("CLI launch failed", 502, "cli_launch_failed")

        with ACTIVE_PROCESSES_LOCK:
            ticket.process = process
            ticket.error = error
            if process is not None:
                if not windows:
                    setattr(process, "_bridge_process_group", process.pid)
                ACTIVE_PROCESSES.add(process)
            cancelled = ticket.cancelled or PROCESS_SHUTTING_DOWN
            if cancelled and ticket.error is None:
                ticket.error = BridgeError("Bridge is shutting down", 503, "bridge_shutting_down")
            if not cancelled:
                PENDING_LAUNCHES.discard(ticket)
                ticket.event.set()

        if cancelled and process is not None:
            try:
                terminate_launched_process(process)
            except BridgeError:
                print(f"[{iso_now()}] delayed_launch_cleanup=failed", flush=True)
            finally:
                unregister_inactive_process(process)
        if cancelled:
            with ACTIVE_PROCESSES_LOCK:
                PENDING_LAUNCHES.discard(ticket)
                ticket.event.set()

    thread = threading.Thread(target=launch_worker, daemon=True)
    try:
        thread.start()
    except Exception as exc:
        with ACTIVE_PROCESSES_LOCK:
            PENDING_LAUNCHES.discard(ticket)
        raise BridgeError("CLI launch failed", 502, "cli_launch_failed") from exc

    remaining = deadline - time.monotonic()
    completed = remaining > 0 and ticket.event.wait(timeout=remaining)
    if not completed:
        with ACTIVE_PROCESSES_LOCK:
            ticket.cancelled = True
            process = ticket.process
            completed = ticket.event.is_set()
        if completed and process is not None:
            try:
                terminate_launched_process(process)
            finally:
                unregister_inactive_process(process)
        raise BridgeError(f"CLI timeout after {timeout}s", 504, "cli_timeout")

    if ticket.error is not None:
        raise ticket.error
    if ticket.process is None:
        raise BridgeError("CLI launch failed", 502, "cli_launch_failed")
    if time.monotonic() >= deadline:
        try:
            terminate_launched_process(ticket.process)
        finally:
            unregister_inactive_process(ticket.process)
        raise BridgeError(f"CLI timeout after {timeout}s", 504, "cli_timeout")
    return ticket.process


def run_process(
    command: list[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path | None = None,
    label: str = "CLI",
    allowed_secret_env: set[str] | None = None,
    absolute_deadline: float | None = None,
    private_texts: tuple[str, ...] = (),
) -> tuple[str, str]:
    event_dump = open_cli_event_dump(label)
    deadline = absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout
    remaining_budget = max(0.0, deadline - time.monotonic())
    cleanup_reserve = min(1.0, remaining_budget * 0.25)
    operation_deadline = deadline - cleanup_reserve
    protected_texts = tuple(text for text in (prompt, *private_texts) if text)
    windows = platform.system() == "Windows"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if windows else 0
    if windows:
        creation_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = launch_process_until_deadline(
        command,
        operation_deadline,
        timeout,
        cwd=cwd,
        allowed_secret_env=allowed_secret_env,
        creation_flags=creation_flags,
        windows=windows,
        cleanup_deadline=deadline,
    )

    stdout_buffer = bytearray()
    stdout_overflow = threading.Event()
    stderr_overflow = threading.Event()
    reader_error = threading.Event()
    turn_completed = threading.Event()

    def read_stream(
        stream: Any,
        retain_stdout: bool,
        show_progress: bool,
        transport_limit: int | None = None,
        transport_overflow: threading.Event | None = None,
    ) -> None:
        progress_buffer = ""
        dropping_progress_line = False
        transport_bytes = 0

        def process_progress_chunk(chunk: str) -> None:
            nonlocal progress_buffer, dropping_progress_line
            pending = chunk
            while pending:
                if dropping_progress_line:
                    newline = pending.find("\n")
                    if newline < 0:
                        return
                    pending = pending[newline + 1:]
                    dropping_progress_line = False
                    continue
                newline = pending.find("\n")
                if newline < 0:
                    if len(progress_buffer) + len(pending) > MAX_CLI_PROGRESS_LINE_CHARS:
                        progress_buffer = ""
                        dropping_progress_line = True
                        dump_omitted_line(event_dump, MAX_CLI_PROGRESS_LINE_CHARS)
                    else:
                        progress_buffer += pending
                    return
                line = progress_buffer + pending[:newline]
                progress_buffer = ""
                pending = pending[newline + 1:]
                if len(line) > MAX_CLI_PROGRESS_LINE_CHARS:
                    dump_omitted_line(event_dump, MAX_CLI_PROGRESS_LINE_CHARS)
                    continue
                dump_cli_line(event_dump, line)
                emit_cli_progress(label, line, protected_texts)
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    event = None
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    turn_completed.set()

        try:
            while True:
                chunk = stream.read(CLI_STREAM_CHUNK_CHARS)
                if chunk == "":
                    break
                if not isinstance(chunk, str):
                    chunk = bytes(chunk).decode("utf-8", errors="replace")
                encoded = chunk.encode("utf-8", errors="replace")
                if transport_limit is not None:
                    transport_bytes += len(encoded)
                    if transport_bytes > transport_limit:
                        if transport_overflow is not None:
                            transport_overflow.set()
                        return
                if retain_stdout:
                    remaining = MAX_CLI_OUTPUT_BYTES - len(stdout_buffer)
                    if remaining > 0:
                        stdout_buffer.extend(encoded[:remaining])
                    if len(encoded) > remaining:
                        stdout_overflow.set()
                if show_progress:
                    process_progress_chunk(chunk)
            if show_progress and progress_buffer and not dropping_progress_line:
                emit_cli_progress(label, progress_buffer, protected_texts)
        except Exception:
            reader_error.set()
        finally:
            try:
                stream.close()
            except Exception:
                reader_error.set()

    if process.stdout is None or process.stderr is None or process.stdin is None:
        try:
            terminate_process_tree(process, deadline=deadline)
        finally:
            unregister_inactive_process(process)
        raise BridgeError("CLI pipe setup failed", 502, "cli_io_failed")
    stdin_error = threading.Event()

    def write_prompt() -> None:
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except Exception:
            stdin_error.set()

    stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, True, True), daemon=True)
    stderr_thread = threading.Thread(
        target=read_stream,
        args=(process.stderr, False, False, MAX_CLI_STDERR_BYTES, stderr_overflow),
        daemon=True,
    )
    stdin_thread = threading.Thread(target=write_prompt, daemon=True)
    worker_threads = (stdout_thread, stderr_thread, stdin_thread)

    def join_workers_until_deadline() -> None:
        for thread in worker_threads:
            if not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(timeout=remaining)

    try:
        for thread in worker_threads:
            thread.start()
    except Exception as exc:
        try:
            terminate_process_tree(process, deadline=deadline)
        finally:
            unregister_inactive_process(process)
        join_workers_until_deadline()
        raise BridgeError("CLI stream setup failed", 502, "cli_io_failed") from exc

    operation_error: BridgeError | None = None
    return_code: int | None = None
    tree_terminated = False
    try:
        try:
            while True:
                if stdin_error.is_set() or reader_error.is_set():
                    raise BridgeError("CLI stream I/O failed", 502, "cli_io_failed")
                if stdout_overflow.is_set() or stderr_overflow.is_set():
                    raise BridgeError("CLI output exceeded the limit", 502, "cli_output_too_large")
                return_code = process.poll()
                if return_code is not None:
                    break
                remaining = operation_deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                turn_completed.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired as exc:
            terminate_process_tree(process, deadline=deadline)
            tree_terminated = True
            operation_error = BridgeError(f"CLI timeout after {timeout}s", 504, "cli_timeout")
            operation_error.__cause__ = exc
        except OSError as exc:
            terminate_process_tree(process, deadline=deadline)
            tree_terminated = True
            operation_error = BridgeError("CLI I/O failed", 502, "cli_io_failed")
            operation_error.__cause__ = exc
        except BridgeError as exc:
            if process.poll() is None or stdin_error.is_set() or reader_error.is_set():
                terminate_process_tree(process, deadline=deadline)
                tree_terminated = True
            operation_error = exc
        except Exception as exc:
            terminate_process_tree(process, deadline=deadline)
            tree_terminated = True
            operation_error = BridgeError("CLI execution failed", 502, "cli_execution_failed")
            operation_error.__cause__ = exc

        join_workers_until_deadline()
        workers_alive = any(thread.is_alive() for thread in worker_threads)
        if workers_alive:
            if not tree_terminated:
                terminate_process_tree(process, deadline=deadline)
                tree_terminated = True
            if operation_error is None:
                operation_error = BridgeError("CLI stream read failed", 502, "cli_io_failed")

        if operation_error is not None:
            raise operation_error
        if reader_error.is_set() or stdin_error.is_set():
            raise BridgeError("CLI stream read failed", 502, "cli_io_failed")
        if stdout_overflow.is_set() or stderr_overflow.is_set():
            raise BridgeError("CLI output exceeded the limit", 502, "cli_output_too_large")
        stdout = stdout_buffer.decode("utf-8", errors="replace")
        if return_code != 0:
            safe_detail = safe_structured_cli_error(
                stdout.splitlines(), prompt, private_texts=private_texts,
            )
            print(
                f"[{iso_now()}] [{label}] exit_code={return_code} "
                f"error_code={safe_detail or 'unknown'}",
                flush=True,
            )
            suffix = f": {safe_detail}" if safe_detail else ""
            raise BridgeError(f"CLI exited with code {return_code}{suffix}", 502, "cli_failed")
        return stdout, ""
    finally:
        close_cli_event_dump(event_dump)
        unregister_inactive_process(process)


SCHEMA_MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
SCHEMA_LIST_KEYWORDS = ("anyOf", "oneOf", "allOf", "prefixItems")
SCHEMA_SINGLE_KEYWORDS = (
    "items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames",
    "unevaluatedItems", "unevaluatedProperties",
)


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def schema_with_inferred_types(node: Any) -> Any:
    """Copy of a JSON Schema in which a `const`/`enum` node without `type` gets the type of its
    value(s). Meaning is unchanged; OpenAI strict mode (Codex) rejects such nodes otherwise.
    Only schema positions are visited, so a property that happens to be named "const" is safe."""
    if not isinstance(node, dict):
        return node
    out = dict(node)
    for key in SCHEMA_MAP_KEYWORDS:
        if isinstance(out.get(key), dict):
            out[key] = {name: schema_with_inferred_types(child) for name, child in out[key].items()}
    for key in SCHEMA_LIST_KEYWORDS:
        if isinstance(out.get(key), list):
            out[key] = [schema_with_inferred_types(child) for child in out[key]]
    for key in SCHEMA_SINGLE_KEYWORDS:
        if isinstance(out.get(key), dict):
            out[key] = schema_with_inferred_types(out[key])
        elif key == "items" and isinstance(out.get(key), list):
            out[key] = [schema_with_inferred_types(child) for child in out[key]]
    if "type" not in out:
        values = [out["const"]] if "const" in out else out.get("enum")
        if isinstance(values, list) and values:
            kinds = {json_type_name(value) for value in values}
            if len(kinds) == 1:
                out["type"] = kinds.pop()
    return out


def schema_prompt_instruction(schema_json: str) -> str:
    return (
        "Structured output required: respond with a single JSON value only - no prose, "
        "no code fences - that matches this JSON Schema:\n" + schema_json
    )


def run_claude(
    settings: Settings, prompt: str, requested_model: Any, deadline: float | None = None,
    *, schema_json: str | None = None, schema_in_prompt: str | None = None,
) -> tuple[str, str]:
    executable, base_args = resolve_claude_launcher(settings.claude_command)
    model = settings.claude_model
    args = [
        *base_args,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--max-turns", "1",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt", DEFAULT_SYSTEM,
    ]
    if settings.claude_effort:
        args.extend(["--effort", settings.claude_effort])
    if schema_json is not None:
        args.extend(["--json-schema", schema_json])
    if schema_in_prompt is not None:
        prompt = f"{prompt}\n\n{schema_prompt_instruction(schema_in_prompt)}"
    command = windows_cmd_wrapper(executable, args)
    stdout, _ = run_process(
        command, prompt, settings.timeout,
        allowed_secret_env={"ANTHROPIC_API_KEY"},
        absolute_deadline=deadline,
    )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError("Claude returned invalid JSON", 502, "cli_output_invalid") from exc
    structured_output = parsed.get("structured_output")
    if isinstance(structured_output, (dict, list)):
        text = json.dumps(structured_output, ensure_ascii=False)
    else:
        text = parsed.get("result")
    if not isinstance(text, str):
        content = parsed.get("message", {}).get("content") if isinstance(parsed.get("message"), dict) else None
        text = content if isinstance(content, str) else ""
    if not text.strip():
        raise BridgeError("Claude returned an empty response", 502, "cli_output_empty")
    return text.strip(), model


def extract_codex_text(stdout: str) -> str:
    candidates: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") in ("agent_message", "message") and isinstance(item.get("text"), str):
                candidates.append(item["text"])
        for key in ("output_text", "response", "text"):
            if isinstance(event.get(key), str):
                candidates.append(event[key])
    if candidates:
        return candidates[-1].strip()
    stripped = stdout.strip()
    if stripped and not stripped.startswith("{"):
        return stripped
    return ""


def run_codex(
    settings: Settings, prompt: str, deadline: float | None = None,
    *, schema_json: str | None = None, json_object: bool = False, schema_in_prompt: str | None = None,
) -> tuple[str, str]:
    executable, base_args = resolve_codex_launcher(settings.codex_command)
    model = settings.codex_model
    job_dir: Path | None = None
    operation_error: BaseException | None = None
    try:
        settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        job_dir = settings.jobs_dir / f"chat-{uuid.uuid4().hex}"
        job_dir.mkdir(mode=0o700, exist_ok=False)
        schema_path: Path | None = None
        if schema_json is not None:
            schema_path = job_dir / "schema.json"
            schema_path.write_text(schema_json, encoding="utf-8")
            schema_path.chmod(0o600)
        args = [*base_args]
        effort_override = codex_effort_override(settings.codex_effort)
        if effort_override:
            args.extend(["-c", effort_override])
        args.extend([
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--color", "never",
            "--json",
        ])
        if model:
            args.extend(["--model", model])
        if schema_path is not None:
            args.extend(["--output-schema", str(schema_path)])
        args.append("-")
        command = windows_cmd_wrapper(executable, args)
        codex_prompt = DEFAULT_SYSTEM
        if json_object:
            codex_prompt += (
                "\n\nStructured output required: respond with a single JSON object only - no prose, "
                "no code fences."
            )
        elif schema_in_prompt is not None:
            codex_prompt += "\n\n" + schema_prompt_instruction(schema_in_prompt)
        codex_prompt += f"\n\n{prompt}"
        stdout, _ = run_process(
            command, codex_prompt, settings.timeout,
            cwd=job_dir,
            allowed_secret_env={"OPENAI_API_KEY"},
            absolute_deadline=deadline,
        )
        text = extract_codex_text(stdout)
        if not text:
            raise BridgeError("Codex returned invalid JSONL", 502, "cli_output_invalid")
        return text, model or "codex-cli-default"
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        if job_dir is not None and job_dir.exists():
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                print(f"[{iso_now()}] [codex/chat] cleanup=failed", flush=True)
                if operation_error is None:
                    raise BridgeError("Codex chat job cleanup failed", 500, "job_cleanup_failed") from exc


PLAYER2_ASPECT_RATIOS = ("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "9:21")


def parse_image_size(body: dict[str, Any]) -> dict[str, Any]:
    """Size fields of a Player2 image request: aspect_ratio (takes precedence), or width and height
    (128-4096), or nothing. Returns {"aspect_ratio": str|None, "width": int|None, "height": int|None}."""
    aspect_ratio = body.get("aspect_ratio")
    if aspect_ratio is not None:
        if not isinstance(aspect_ratio, str) or aspect_ratio not in PLAYER2_ASPECT_RATIOS:
            raise BridgeError(
                "aspect_ratio must be one of " + ", ".join(PLAYER2_ASPECT_RATIOS), 400, "invalid_request",
            )
        return {"aspect_ratio": aspect_ratio, "width": None, "height": None}
    width = body.get("width")
    height = body.get("height")
    if width is None and height is None:
        return {"aspect_ratio": None, "width": None, "height": None}
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 128 or value > 4096:
            raise BridgeError(f"{name} must be an integer between 128 and 4096", 400, "invalid_request")
    return {"aspect_ratio": None, "width": width, "height": height}


def image_size_token(size: dict[str, Any]) -> str:
    if size["aspect_ratio"]:
        return f"aspect={size['aspect_ratio']}"
    if size["width"] and size["height"]:
        return f"size={size['width']}x{size['height']}"
    return "size=default"


def resolved_image_size(settings: Settings, size: dict[str, Any]) -> tuple[int, int]:
    """The size passed to the provider: the request's width/height, an aspect_ratio applied to the
    configured width, or the configured default when the request carries no size."""
    if size["width"] and size["height"]:
        return size["width"], size["height"]
    if size["aspect_ratio"]:
        w, h = size["aspect_ratio"].split(":")
        ratio = float(w) / float(h)
        width = settings.image_width
        return width, max(128, min(4096, int(round(width / ratio))))
    return settings.image_width, settings.image_height


def grok_aspect_for(width: int, height: int) -> str:
    """Nearest Grok aspect_ratio for a size; never "auto" (it produced e.g. 2208x464)."""
    ratio = width / height
    candidates = []
    for name in GROK_IMAGE_ASPECT_RATIOS:
        if name == "auto":
            continue
        w, h = name.split(":")
        candidates.append((abs(math.log(ratio) - math.log(float(w) / float(h))), name))
    return min(candidates)[1]


def decode_input_image(value: Any, max_bytes: int) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("input image must be non-empty base64", 400, "invalid_request")
    encoded = value.strip()
    if encoded.startswith("data:"):
        if "," not in encoded:
            raise BridgeError("invalid image data URI", 400, "invalid_request")
        encoded = encoded.split(",", 1)[1]
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BridgeError("invalid image base64", 400, "invalid_request") from exc
    if not data or len(data) > max_bytes:
        raise BridgeError("input image size is invalid", 413, "body_too_large")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise BridgeError("input image must be PNG, JPEG, or WebP", 400, "invalid_request")
    return data, suffix


def codex_generated_images_dir() -> Path:
    """Where Codex's built-in image tool saves its output: $CODEX_HOME/generated_images."""
    home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return (base / "generated_images").resolve()


GENERATED_PNG_LINE = re.compile(r"^\s*GENERATED_PNG\s*=\s*<?\s*(.+?)\s*>?\s*$", re.M)


CODEX_THREAD_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,63}")


def codex_run_thread_id(stdout: str) -> str | None:
    """The thread id Codex prints when the run starts; its image tool saves into
    generated_images/<thread id>/, which ties a saved image to this run."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and CODEX_THREAD_ID.fullmatch(thread_id):
                return thread_id
            return None
    return None


def extract_generated_png_path(stdout: str) -> Path | None:
    """The path Codex was asked to report (GENERATED_PNG=...), read from its final message only."""
    final_text: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") not in ("agent_message", "message"):
            continue
        if isinstance(item.get("text"), str):
            final_text = item["text"]
    if final_text is None:
        return None
    found: str | None = None
    for match in GENERATED_PNG_LINE.finditer(final_text):
        candidate = match.group(1).strip().strip("'\"`")
        if candidate.startswith("[") and "](" in candidate:  # markdown link
            candidate = candidate[candidate.index("](") + 2:].rstrip(")")
        if candidate:
            found = candidate
    return Path(found) if found else None


def reject_generated_png(reason: str, message: str) -> BridgeError:
    print(f"[{iso_now()}] generated_png=rejected reason={reason}", flush=True)
    return BridgeError(message, 502, "image_output_invalid")


def read_reported_codex_png(
    reported: Path, max_bytes: int, thread_id: str | None, deadline: float | None = None,
) -> tuple[bytes, int, int]:
    """The path comes from the model's text, which prompt injection in game text could steer, so
    only a .png directly inside this run's folder, generated_images/<thread id>/, is accepted.
    That rules out older images, images of other Codex runs and any other file."""
    if thread_id is None:
        raise reject_generated_png("no_run_id", "Codex did not report its run id")
    run_folder = codex_generated_images_dir() / thread_id
    try:
        resolved = reported.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BridgeError("reported PNG path does not exist", 502, "image_output_missing") from exc
    if resolved.parent != run_folder:
        raise reject_generated_png("not_this_run", "reported file is not in this run's image folder")
    if resolved.suffix.lower() != ".png":
        raise reject_generated_png("not_png", "reported file is not a .png file")
    if not resolved.is_file():
        raise BridgeError("reported PNG path is not a file", 502, "image_output_invalid")
    return validate_png(resolved, max_bytes, deadline)


def file_kind_label(head: bytes) -> str:
    """A coarse file-type label for diagnostics; never the file's bytes."""
    if not head:
        return "empty"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return "other"


def validate_png(path: Path, max_bytes: int, deadline: float | None = None) -> tuple[bytes, int, int]:
    if not path.is_file():
        raise BridgeError("Codex did not create output.png", 502, "image_output_missing")
    # The file exists; if it cannot be opened yet (Windows sharing violation while an antivirus
    # scans the new file), keep trying for a while instead of reporting a corrupt image.
    retry_deadline = time.monotonic() + OUTPUT_READ_RETRY_SECONDS
    if deadline is not None:
        retry_deadline = min(retry_deadline, deadline)
    last_error: OSError | None = None
    while True:
        try:
            data = read_complete_png(path, max_bytes, raise_os_error=True)
            break
        except FileNotFoundError as exc:
            last_error = exc
            data = None
            break
        except OSError as exc:
            last_error = exc
            remaining = retry_deadline - time.monotonic()
            if remaining <= 0:
                data = None
                break
            time.sleep(min(0.25, remaining))
            if time.monotonic() >= retry_deadline:  # never start a retry at or after the deadline
                data = None
                break
    if data is None:
        if last_error is not None:
            winerror = getattr(last_error, "winerror", None)
            print(
                f"[{iso_now()}] output_unreadable error={type(last_error).__name__} "
                f"errno={last_error.errno} winerror={winerror}",
                flush=True,
            )
            raise BridgeError("output.png could not be read", 502, "image_output_invalid")
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                kind = file_kind_label(handle.read(12))
        except OSError:
            size, kind = -1, "unreadable"
        print(f"[{iso_now()}] output_invalid bytes={size} kind={kind}", flush=True)
        raise BridgeError("generated file is not a valid PNG", 502, "image_output_invalid")
    width, height = struct.unpack(">II", data[16:24])
    if (
        width < 1 or height < 1 or
        width > MAX_OUTPUT_IMAGE_DIMENSION or height > MAX_OUTPUT_IMAGE_DIMENSION
    ):
        raise BridgeError("generated PNG dimensions are invalid", 502, "image_output_invalid")
    return data, width, height


def convert_jpeg_to_png(data: bytes, output_path: Path, timeout: float) -> None:
    try:
        from PIL import Image  # optional; Windows falls back to PowerShell System.Drawing below
    except ImportError:
        Image = None
    if Image is not None:
        with Image.open(io.BytesIO(data)) as image:
            image.save(output_path, format="PNG")
        return
    if platform.system() != "Windows":
        raise BridgeError("PNG conversion needs Pillow (pip install pillow)", 502, "image_output_invalid")
    source_path = output_path.with_name("grok-output.jpg")
    source_path.write_bytes(data)

    def quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$image = [System.Drawing.Image]::FromFile({quote(source_path)}); "
        f"$image.Save({quote(output_path)}, [System.Drawing.Imaging.ImageFormat]::Png); "
        "$image.Dispose()"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=sanitized_child_environment(), timeout=timeout, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("PNG conversion via PowerShell failed to run", 502, "image_output_invalid") from exc
    if completed.returncode != 0 or not output_path.is_file():
        print(f"[{iso_now()}] png_convert=failed exit_code={completed.returncode}", flush=True)
        raise BridgeError("PNG conversion via PowerShell failed", 502, "image_output_invalid")


def require_openai_key(settings: Settings, feature: str) -> str:
    if not settings.openai_api_key:
        print(f"[{iso_now()}] [SKIP] feature={feature} reason=embedding_key_absent", flush=True)
        raise BridgeError(
            f"{feature} is unavailable because the optional embedding key is not configured",
            503,
            "feature_unavailable",
        )
    return settings.openai_api_key


def openai_request(
    settings: Settings,
    feature: str,
    path: str,
    body: dict[str, Any],
    *,
    expect_json: bool,
) -> Any:
    key = require_openai_key(settings, feature)
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        settings.openai_base_url + path,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json" if expect_json else "application/octet-stream",
        },
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=settings.timeout) as response:
            raw = response.read(settings.max_body_bytes + 1)
            status = response.status
    except HTTPError as exc:
        exc.read(settings.max_body_bytes)
        print(f"[{iso_now()}] feature={feature} upstream_status={exc.code}", flush=True)
        raise BridgeError(f"OpenAI {feature} request failed with HTTP {exc.code}", 502, "upstream_error") from exc
    except (URLError, TimeoutError, OSError) as exc:
        print(f"[{iso_now()}] feature={feature} upstream_connection_error", flush=True)
        raise BridgeError(f"OpenAI {feature} connection failed", 502, "upstream_error") from exc
    if status != 200 or len(raw) > settings.max_body_bytes:
        raise BridgeError(f"OpenAI {feature} returned an invalid response", 502, "upstream_error")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"[{iso_now()}] feature={feature} upstream_status=200 elapsed_ms={elapsed_ms} bytes={len(raw)}", flush=True)
    if not expect_json:
        return raw
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"OpenAI {feature} returned invalid JSON", 502, "upstream_error") from exc
    if not isinstance(parsed, dict):
        raise BridgeError(f"OpenAI {feature} returned invalid JSON", 502, "upstream_error")
    return parsed


def create_embeddings(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    value = body.get("input")
    inputs = value if isinstance(value, list) else [value]
    if not inputs or len(inputs) > 100 or any(not isinstance(item, str) or not item for item in inputs):
        raise BridgeError("input must be a non-empty string or array of up to 100 strings", 400, "invalid_request")
    request_body: dict[str, Any] = {
        "model": settings.embedding_model,
        "input": value,
        "encoding_format": "float",
    }
    dimensions = body.get("dimensions")
    if dimensions is not None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
            raise BridgeError("dimensions must be a positive integer", 400, "invalid_request")
        request_body["dimensions"] = dimensions
    response = openai_request(settings, "embeddings", "/embeddings", request_body, expect_json=True)
    data = response.get("data")
    if not isinstance(data, list) or len(data) != len(inputs):
        raise BridgeError("OpenAI embeddings response count mismatch", 502, "upstream_error")
    for index, item in enumerate(data):
        if not isinstance(item, dict) or item.get("index") != index or not isinstance(item.get("embedding"), list):
            raise BridgeError("OpenAI embeddings response is invalid", 502, "upstream_error")
    return response


ACP_HTTP_STATUS_HINTS = {401: "login_required", 402: "usage_or_credits_exhausted", 403: "forbidden", 429: "rate_limited"}


def log_acp_error_status(label: str, method: str, error: Any) -> None:
    """Log the upstream HTTP status of a Grok ACP error (a number only, never provider text)."""
    data = error.get("data") if isinstance(error, dict) else None
    status = data.get("http_status") if isinstance(data, dict) else None
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return
    safe_method = method if method in {"initialize", "authenticate", "session/new", "session/prompt"} else "other"
    hint = ACP_HTTP_STATUS_HINTS.get(status)
    suffix = f" hint={hint}" if hint else ""
    print(f"[{iso_now()}] [{label}] acp_error method={safe_method} upstream_http_status={status}{suffix}", flush=True)



def run_grok_acp(*args: Any, **kwargs: Any) -> Any:
    event_dump = open_cli_event_dump(kwargs.get("diagnostic_label", "grok/chat"))
    try:
        return _run_grok_acp(event_dump, *args, **kwargs)
    finally:
        close_cli_event_dump(event_dump)

def _run_grok_acp(
    event_dump: Any,
    command: list[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path,
    system_prompt: str = "",
    absolute_deadline: float | None = None,
    prompt_blocks: list[dict[str, Any]] | None = None,
    expected_media_type: str | None = None,
    diagnostic_label: str = "grok/chat",
    private_texts: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> str | GrokMediaOutput:
    source_prompt = (
        prompt_blocks
        if prompt_blocks is not None
        else [{"type": "text", "text": prompt}]
    )
    request_prompt_json = json.dumps(
        source_prompt, ensure_ascii=False, separators=(",", ":"),
    )
    outbound_prompt_bytes = len(request_prompt_json.encode("utf-8"))
    request_prompt = json.loads(request_prompt_json)
    expected_reference_tokens = tuple(
        f"[Image #{index}]"
        for index in range(
            1,
            1 + sum(
                1 for item in request_prompt
                if isinstance(item, dict) and item.get("type") == "image"
            ),
        )
    )
    acp_stdout_limit = MAX_CLI_OUTPUT_BYTES + outbound_prompt_bytes
    deadline = absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout
    remaining_budget = max(0.0, deadline - time.monotonic())
    cleanup_reserve = min(1.0, remaining_budget * 0.25)
    operation_deadline = deadline - cleanup_reserve
    windows = platform.system() == "Windows"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if windows else 0
    if windows:
        creation_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = launch_process_until_deadline(
        command,
        operation_deadline,
        timeout,
        cwd=cwd,
        allowed_secret_env={"XAI_API_KEY"},
        creation_flags=creation_flags,
        windows=windows,
        cleanup_deadline=deadline,
        extra_env=extra_env,
    )

    if process.stdin is None or process.stdout is None or process.stderr is None:
        try:
            terminate_process_tree(process, deadline=deadline)
        finally:
            unregister_inactive_process(process)
        raise BridgeError("Grok ACP pipe setup failed", 502, "cli_io_failed")

    messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
    reader_error = threading.Event()
    invalid_output = threading.Event()
    output_overflow = threading.Event()
    overflow_source_lock = threading.Lock()
    overflow_source: str | None = None
    queue_deadline = threading.Event()
    consumer_done = threading.Event()
    stdout_done = threading.Event()

    writer_threads: list[threading.Thread] = []
    assistant = bytearray()
    tool_names: dict[str, str] = {}
    tool_call_ids: set[str] = set()
    media_tool_call_ids: set[str] = set()
    media_tool_inputs_logged: set[str] = set()
    media_outputs: dict[str, GrokMediaOutput] = {}
    session_id: str | None = None
    current_prompt_id = uuid.uuid4().hex if expected_media_type is not None else None
    prompt_updates_active = False
    next_request_id = 1

    def timeout_error() -> BridgeError:
        return BridgeError(f"CLI timeout after {timeout}s", 504, "cli_timeout")

    def mark_output_overflow(source: str) -> None:
        nonlocal overflow_source
        with overflow_source_lock:
            if overflow_source is None:
                overflow_source = source
        output_overflow.set()

    def queue_message(line: str) -> bool:
        if not line.strip():
            return True
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            invalid_output.set()
            return False
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            invalid_output.set()
            return False
        while True:
            if consumer_done.is_set():
                return False
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                queue_deadline.set()
                return False
            try:
                messages.put(message, timeout=min(0.05, remaining))
                return True
            except queue.Full:
                continue

    def read_stdout() -> None:
        pending = ""
        total_bytes = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            while True:
                raw_stream = getattr(process.stdout, "buffer", None)
                if raw_stream is not None and hasattr(raw_stream, "read1"):
                    raw = raw_stream.read1(CLI_STREAM_CHUNK_CHARS)
                    if raw == b"":
                        break
                    total_bytes += len(raw)
                    chunk = decoder.decode(raw)
                else:
                    chunk = process.stdout.read(1)
                    if chunk == "":
                        break
                    if not isinstance(chunk, str):
                        chunk = bytes(chunk).decode("utf-8", errors="strict")
                    total_bytes += len(chunk.encode("utf-8", errors="strict"))
                if total_bytes > acp_stdout_limit:
                    mark_output_overflow("stdout_bytes")
                    return
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line) <= acp_stdout_limit:
                        dump_cli_line(event_dump, line)
                    if len(line) > acp_stdout_limit or not queue_message(line):
                        if len(line) > acp_stdout_limit:
                            mark_output_overflow("frame_chars")
                        return
                if len(pending) > acp_stdout_limit:
                    mark_output_overflow("frame_chars")
                    return
            pending += decoder.decode(b"", final=True)
            if pending:
                dump_cli_line(event_dump, pending)
            if pending and not queue_message(pending):
                return
        except UnicodeDecodeError:
            invalid_output.set()
        except Exception:
            reader_error.set()
        finally:
            try:
                process.stdout.close()
            except Exception:
                reader_error.set()
            stdout_done.set()

    def drain_stderr() -> None:
        total_bytes = 0
        try:
            raw_stream = getattr(process.stderr, "buffer", None)
            while True:
                chunk = (
                    raw_stream.read1(CLI_STREAM_CHUNK_CHARS)
                    if raw_stream is not None and hasattr(raw_stream, "read1")
                    else process.stderr.read(1)
                )
                if chunk in ("", b""):
                    break
                total_bytes += len(chunk) if isinstance(chunk, bytes) else len(chunk.encode("utf-8", errors="replace"))
                if total_bytes > MAX_CLI_STDERR_BYTES:
                    mark_output_overflow("stderr_bytes")
                    return
        except Exception:
            reader_error.set()
        finally:
            try:
                process.stderr.close()
            except Exception:
                reader_error.set()


    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)

    def check_stream_state() -> None:
        if queue_deadline.is_set():
            raise timeout_error()
        if output_overflow.is_set():
            print(
                f"[{iso_now()}] [{diagnostic_label}] overflow_source={overflow_source or 'unknown'}",
                flush=True,
            )
            raise BridgeError("Grok ACP output exceeded the limit", 502, "cli_output_too_large")
        if invalid_output.is_set():
            raise BridgeError("Grok ACP returned invalid JSON-RPC", 502, "cli_output_invalid")
        if reader_error.is_set():
            raise BridgeError("Grok ACP stream I/O failed", 502, "cli_io_failed")

    def write_message(message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error()
        completed = threading.Event()
        failed = threading.Event()

        def writer() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except Exception:
                failed.set()
            finally:
                completed.set()

        thread = threading.Thread(target=writer, daemon=True)
        writer_threads.append(thread)
        try:
            thread.start()
        except Exception as exc:
            raise BridgeError("Grok ACP stream setup failed", 502, "cli_io_failed") from exc
        if not completed.wait(timeout=remaining):
            raise timeout_error()
        if failed.is_set():
            raise BridgeError("Grok ACP stdin write failed", 502, "cli_io_failed")

    def handle_session_update(message: dict[str, Any]) -> None:
        nonlocal session_id
        if message.get("method") != "session/update":
            return
        params = message.get("params")
        if (
            not prompt_updates_active
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(params, dict)
            or params.get("sessionId") != session_id
        ):
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        update_type = update.get("sessionUpdate")
        if expected_media_type is not None:
            update_meta = params.get("_meta")
            if (
                not isinstance(update_meta, dict)
                or not isinstance(current_prompt_id, str)
                or update_meta.get("promptId") != current_prompt_id
            ):
                return
            diagnostic_update = update
            raw_call_id = safe_machine_token(update.get("toolCallId"), private_texts)
            raw_tool_name = safe_machine_token(update.get("toolName"), private_texts)
            if raw_call_id and raw_tool_name in SAFE_PROGRESS_TOOL_NAMES:
                if len(tool_names) < 64 or raw_call_id in tool_names:
                    tool_names[raw_call_id] = raw_tool_name
            elif raw_call_id and raw_call_id in tool_names:
                diagnostic_update = dict(update)
                diagnostic_update["toolName"] = tool_names[raw_call_id]
            if update_type not in {"agent_thought_chunk", "agent_message_chunk"}:
                # ACP streams thoughts/answers token by token; those chunks carry no diagnostic value.
                emit_cli_progress(
                    diagnostic_label,
                    json.dumps(diagnostic_update, ensure_ascii=False, separators=(",", ":")),
                    private_texts,
                )
            if update_type in {"tool_call", "tool_call_update"}:
                call_id = update.get("toolCallId")
                if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
                    raise BridgeError("Grok ACP returned an invalid tool call", 502, "cli_output_invalid")
                if update_type == "tool_call":
                    tool_call_ids.add(call_id)
                    if len(tool_call_ids) > 64:
                        raise BridgeError("Grok ACP returned too many tool calls", 502, "cli_output_invalid")
                elif call_id not in tool_call_ids:
                    raise BridgeError("Grok ACP tool update has no matching call", 502, "cli_output_invalid")

                raw_input = update.get("rawInput")
                image_edit_input = (
                    tool_names.get(call_id) == "image_edit"
                    or (isinstance(raw_input, dict) and "image" in raw_input)
                )
                if (
                    expected_media_type == "ImageEdit"
                    and image_edit_input
                    and call_id not in media_tool_inputs_logged
                    and "rawInput" in update
                ):
                    tool_input_summary = summarize_grok_image_edit_input(
                        raw_input, expected_reference_tokens,
                    )
                    print(
                        f"[{iso_now()}] [{diagnostic_label}] tool_input={tool_input_summary}",
                        flush=True,
                    )
                    media_tool_inputs_logged.add(call_id)

                if update_type == "tool_call_update" and update.get("status") == "failed":
                    failure_code = classify_grok_media_tool_failure(update.get("rawOutput"))
                    if (
                        expected_media_type == "ImageEdit"
                        and image_edit_input
                        and call_id not in media_tool_inputs_logged
                    ):
                        print(
                            f"[{iso_now()}] [{diagnostic_label}] tool_input=unavailable",
                            flush=True,
                        )
                        media_tool_inputs_logged.add(call_id)
                    if image_edit_input or failure_code != "image_tool_failure_unclassified":
                        print(
                            f"[{iso_now()}] [{diagnostic_label}] failure_code={failure_code}",
                            flush=True,
                        )

                if update_type == "tool_call_update" and update.get("status") == "completed":
                    raw_output = update.get("rawOutput")
                    if not isinstance(raw_output, dict):
                        return
                    output_type = raw_output.get("type")
                    if output_type not in {"ImageEdit", "ImageGen"}:
                        return
                    if set(raw_output) != {
                        "type", "path", "filename", "session_folder",
                    }:
                        raise BridgeError("Grok ACP image result is invalid", 502, "cli_output_invalid")
                    raw_path = raw_output.get("path")
                    filename = raw_output.get("filename")
                    session_folder = raw_output.get("session_folder")
                    if (
                        output_type != expected_media_type
                        or not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path
                        or not isinstance(filename, str) or not filename or len(filename) > 255
                        or Path(filename).name != filename or filename in {".", ".."}
                        or session_folder != "images"
                    ):
                        raise BridgeError("Grok ACP image result fields are invalid", 502, "cli_output_invalid")
                    output_path = Path(raw_path)
                    if (
                        not output_path.is_absolute()
                        or output_path.name != filename
                        or output_path.parent.name != session_folder
                        or call_id in media_outputs
                        or media_tool_call_ids
                    ):
                        raise BridgeError("Grok ACP image result path is invalid", 502, "cli_output_invalid")
                    media_tool_call_ids.add(call_id)
                    media_outputs[call_id] = GrokMediaOutput(
                        output_type=output_type,
                        path=output_path,
                        filename=filename,
                        session_folder=session_folder,
                    )
        elif update_type in {"tool_call", "tool_call_update"}:
            # Chat sessions must not run tools; a hook denial shows up here as status=failed.
            emit_cli_progress(
                diagnostic_label,
                json.dumps(update, ensure_ascii=False, separators=(",", ":")),
                private_texts,
            )
        if update_type != "agent_message_chunk":
            return
        content = update.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            return
        encoded = content["text"].encode("utf-8", errors="replace")
        if len(assistant) + len(encoded) > MAX_CLI_OUTPUT_BYTES:
            mark_output_overflow("assistant_bytes")
            raise BridgeError("Grok ACP output exceeded the limit", 502, "cli_output_too_large")
        assistant.extend(encoded)

    def next_message(wait_limit: float | None = None) -> dict[str, Any] | None:
        while True:
            check_stream_state()
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            wait = min(0.05, remaining)
            if wait_limit is not None:
                if wait_limit <= 0:
                    return None
                wait = min(wait, wait_limit)
            started = time.monotonic()
            try:
                return messages.get(timeout=wait)
            except queue.Empty:
                if wait_limit is not None:
                    wait_limit -= time.monotonic() - started
                    if wait_limit <= 0:
                        return None
                if stdout_done.is_set() and messages.empty():
                    check_stream_state()
                    raise BridgeError("Grok ACP exited before completion", 502, "cli_failed")
                if process.poll() is not None and stdout_done.is_set():
                    raise BridgeError("Grok ACP exited before completion", 502, "cli_failed")

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal next_request_id
        request_id = next_request_id
        next_request_id += 1
        write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = next_message()
            assert message is not None
            handle_session_update(message)
            if message.get("method") is not None and message.get("id") is not None:
                write_message({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Unsupported ACP client method"},
                })
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                log_acp_error_status(diagnostic_label, method, message.get("error"))
                raise BridgeError(f"Grok ACP {method} failed", 502, "cli_failed")
            result = message.get("result")
            if not isinstance(result, dict):
                raise BridgeError("Grok ACP returned an invalid response", 502, "cli_output_invalid")
            return result

    try:
        try:
            stdout_thread.start()
            stderr_thread.start()
            initialized = request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            })
            protocol_version = initialized.get("protocolVersion")
            if type(protocol_version) is not int or protocol_version != 1:
                raise BridgeError("Grok ACP protocol negotiation failed", 502, "cli_output_invalid")
            raw_methods = initialized.get("authMethods")
            auth_methods = (
                {
                    item.get("id") for item in raw_methods
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                if isinstance(raw_methods, list) else set()
            )
            has_api_key = bool(os.environ.get("XAI_API_KEY", "").strip())
            if has_api_key and "xai.api_key" in auth_methods:
                auth_method = "xai.api_key"
            elif "cached_token" in auth_methods:
                auth_method = "cached_token"
            else:
                raise BridgeError("Grok CLI is not authenticated", 502, "cli_failed")
            request("authenticate", {"methodId": auth_method, "_meta": {"headless": True}})
            session_params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": []}
            if system_prompt:
                session_params["_meta"] = {"systemPromptOverride": system_prompt}
            new_session = request("session/new", session_params)
            raw_session_id = new_session.get("sessionId")
            if not isinstance(raw_session_id, str) or not raw_session_id:
                raise BridgeError("Grok ACP returned an invalid session", 502, "cli_output_invalid")
            session_id = raw_session_id
            prompt_params: dict[str, Any] = {
                "sessionId": session_id,
                "prompt": request_prompt,
            }
            if current_prompt_id is not None:
                prompt_params["_meta"] = {"promptId": current_prompt_id}
            prompt_updates_active = True
            try:
                prompt_result = request("session/prompt", prompt_params)
                if prompt_result.get("stopReason") not in {
                    "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
                }:
                    raise BridgeError("Grok ACP returned an invalid stop reason", 502, "cli_output_invalid")

                stable_checks = 0
                last_length = len(assistant)
                while stable_checks < 2:
                    message = next_message(wait_limit=min(0.15, max(0.0, operation_deadline - time.monotonic())))
                    if message is None:
                        stable_checks += 1
                        continue
                    handle_session_update(message)
                    if len(assistant) == last_length:
                        continue
                    last_length = len(assistant)
                    stable_checks = 0
            finally:
                prompt_updates_active = False

            text = assistant.decode("utf-8", errors="replace").strip()
            if expected_media_type is not None:
                if len(media_tool_call_ids) != 1 or len(media_outputs) != 1:
                    raise BridgeError("Grok image output is missing", 502, "image_output_missing")
                return next(iter(media_outputs.values()))
            if not text:
                raise BridgeError("Grok returned an empty response", 502, "cli_output_empty")
            return text
        except BridgeError as exc:
            raise
        except Exception as exc:
            error = BridgeError("Grok ACP execution failed", 502, "cli_execution_failed")
            error.__cause__ = exc
            raise error
    finally:
        consumer_done.set()
        cleanup_error: BridgeError | None = None
        post_cleanup_stream_error: BridgeError | None = None
        try:
            terminate_process_tree(process, deadline=deadline)
        except BridgeError as exc:
            cleanup_error = exc
        finally:
            unregister_inactive_process(process)
            stdin_close_failed = threading.Event()
            stdin_stream = process.stdin

            def close_stdin() -> None:
                try:
                    stdin_stream.close()
                except Exception:
                    stdin_close_failed.set()

            stdin_close_thread: threading.Thread | None = threading.Thread(
                target=close_stdin, daemon=True,
            )
            try:
                stdin_close_thread.start()
            except Exception:
                stdin_close_thread = None
                stdin_close_failed.set()
            cleanup_threads = [stdout_thread, stderr_thread, *writer_threads]
            if stdin_close_thread is not None:
                cleanup_threads.append(stdin_close_thread)
            for thread in cleanup_threads:
                if thread.is_alive():
                    remaining = max(0.0, deadline - time.monotonic())
                    thread.join(timeout=remaining)
            if (
                stdin_close_failed.is_set()
                or stdin_close_thread is None
                or stdin_close_thread.is_alive()
                or any(thread.is_alive() for thread in [stdout_thread, stderr_thread, *writer_threads])
            ) and cleanup_error is None:
                cleanup_error = BridgeError(
                    "Grok ACP stream cleanup failed", 502, "cli_io_failed",
                )
            try:
                check_stream_state()
            except BridgeError as exc:
                post_cleanup_stream_error = exc
        if cleanup_error is not None:
            raise cleanup_error
        if post_cleanup_stream_error is not None:
            raise post_cleanup_stream_error


def grok_image_acp_args(settings: Settings) -> list[str]:
    return [
        "--no-auto-update",
        "--model", settings.grok_model,
        "--effort", settings.grok_effort,
        "--sandbox", "workspace",
        "--max-turns", "4",
        "--no-plan",
        "--no-subagents",
        "--no-memory",
        "--disable-web-search",
        "--always-approve",
        "agent", "stdio",
    ]


def run_grok(
    settings: Settings,
    prompt: str,
    deadline: float | None = None,
    *,
    system_prompt: str = "",
    schema_json: str | None = None,
) -> tuple[str, str]:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    job_dir = settings.jobs_dir / f"chat-{uuid.uuid4().hex}"
    job_dir.mkdir(mode=0o700, exist_ok=False)
    operation_error: BaseException | None = None
    try:
        ensure_grok_deny_tools_hook()
        executable, base_args = resolve_grok_launcher(settings.grok_command)
        args = [
            *base_args,
            "--no-auto-update",
            "--model", settings.grok_model,
            "--effort", settings.grok_effort,
            "--sandbox", "read-only",
            "--max-turns", "1",
            "--no-plan",
            "--no-subagents",
            "--no-memory",
            "--disable-web-search",
            "agent", "stdio",
        ]
        command = windows_cmd_wrapper(executable, args)
        chat_system_prompt = f"{DEFAULT_SYSTEM}\n\n{GROK_CHAT_TOOL_RULE}" + (f"\n\n{system_prompt}" if system_prompt else "")
        if schema_json is not None:
            chat_system_prompt += "\n\n" + schema_prompt_instruction(schema_json)
        text = run_grok_acp(
            command, prompt, settings.timeout, cwd=job_dir,
            system_prompt=chat_system_prompt, absolute_deadline=deadline,
            extra_env={GROK_DENY_TOOLS_ENV: "1"},
        )
        return text, settings.grok_model
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        if job_dir.exists():
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                print(f"[{iso_now()}] [grok/chat] cleanup=failed", flush=True)
                if operation_error is None:
                    raise BridgeError("Grok chat job cleanup failed", 500, "job_cleanup_failed") from exc


def feature_not_implemented(feature: str) -> BridgeError:
    print(f"[{iso_now()}] [UNAVAILABLE] feature={feature} status=501 extension_point=reserved", flush=True)
    return BridgeError(
        f"{feature} is not implemented in this bridge build",
        501,
        "feature_not_implemented",
    )


def fish_failure_code(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        if error.code == 401:
            return "unauthorized"
        if error.code == 402:
            return "payment_required"
        if error.code == 429:
            return "rate_limited"
        if 400 <= error.code < 500:
            return "http_4xx"
        return "http_5xx"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, URLError):
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            return "timeout"
        return "network"
    return "network"


def fish_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FishFailure("timeout")
    return min(30.0, remaining)


def fish_open(
    settings: Settings, method: str, path: str, deadline: float, body: dict[str, Any] | None = None,
):
    data = None
    headers = {
        "Authorization": f"Bearer {settings.fish_api_key}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    if method == "POST":
        headers["model"] = settings.tts_model
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(settings.fish_base_url + path, data=data, headers=headers, method=method)
    try:
        return FISH_OPENER.open(request, timeout=fish_timeout(deadline))
    except HTTPError as exc:
        code = fish_failure_code(exc)
        exc.close()
        raise FishFailure(code) from exc
    except (URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        raise FishFailure(fish_failure_code(exc)) from exc


def fish_request(
    settings: Settings, method: str, path: str, deadline: float, body: dict[str, Any] | None = None,
) -> bytes:
    response = fish_open(settings, method, path, deadline, body)
    try:
        try:
            data = response.read(FISH_MAX_RESPONSE_BYTES + 1)
        except (URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            raise FishFailure(fish_failure_code(exc)) from exc
        if len(data) > FISH_MAX_RESPONSE_BYTES:
            raise FishFailure("output_too_large")
        return data
    finally:
        response.close()


def parse_tts_body(settings: Settings, raw_body: bytes) -> tuple[dict[str, Any], str, str | None]:
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("invalid JSON body", 400, "invalid_json") from exc
    if not isinstance(body, dict):
        raise BridgeError("JSON body must be an object", 400, "invalid_request")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BridgeError("text must be a non-empty string", 400, "invalid_request", detail="text")
    if len(text) > settings.max_prompt_chars:
        raise BridgeError("text is too large", 413, "prompt_too_large", detail="text")
    # Optional fields sent as JSON null are treated as absent.
    voice_ids = body.get("voice_ids")
    if voice_ids is None:
        voice_ids = []
    if not isinstance(voice_ids, list) or any(not isinstance(item, str) for item in voice_ids):
        raise BridgeError("voice_ids must be a string array", 400, "invalid_request", detail="voice_ids")
    audio_format = body.get("audio_format")
    if audio_format is None:
        audio_format = "mp3"
    if not isinstance(audio_format, str):
        raise BridgeError("audio_format must be a string", 400, "invalid_request", detail="audio_format")
    audio_format = audio_format.strip().lower()
    if audio_format not in {"mp3", "wav", "pcm"}:
        # Player2 clients send formats outside the documented enum; serve mp3 (the Player2 default)
        # and report it in the response. Only a short safe token is logged, never the text.
        token = audio_format if re.fullmatch(r"[a-z0-9_.-]{1,16}", audio_format) else "invalid"
        print(f"[{iso_now()}] tts_format={token or 'empty'} served=mp3", flush=True)
        audio_format = "mp3"
    speed = body.get("speed")
    if speed is None:
        speed = 1.0
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not (0.5 <= speed <= 2.0):
        raise BridgeError("speed must be between 0.5 and 2.0", 400, "invalid_request", detail="speed")
    fish_format = audio_format if audio_format in {"wav", "pcm"} else "mp3"
    fish_body: dict[str, Any] = {
        "text": text,
        "format": fish_format,
        "prosody": {"speed": speed},
    }
    if fish_format == "mp3":
        fish_body["mp3_bitrate"] = 128
    else:
        # wav/pcm are 16-bit mono at the configured rate (pcm is raw samples without a header).
        fish_body["sample_rate"] = settings.tts_sample_rate
    return fish_body, fish_format, voice_ids[0] if voice_ids else None


TTS_MIMETYPES = {"mp3": "audio/mpeg", "wav": "audio/wav", "pcm": "audio/pcm"}


def tts_duration(settings: Settings, audio_format: str, audio: bytes) -> float:
    if audio_format == "wav":
        return round(wav_duration(audio), 3)
    if audio_format == "pcm":
        return round(len(audio) / (settings.tts_sample_rate * 2), 3)
    return round(len(audio) * 8 / 128000, 3)


def wav_duration(data: bytes) -> float:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0.0
    offset = 12
    byte_rate = 0
    data_size = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = int.from_bytes(data[offset + 4:offset + 8], "little")
        chunk_start = offset + 8
        if chunk_id == b"data":
            # Streamed WAV (Fish Audio) carries a placeholder data size such as 0xFFFFFF00;
            # the audio actually received is what follows the chunk header.
            data_size = min(chunk_size, len(data) - chunk_start)
            break
        if chunk_start + chunk_size > len(data):
            return 0.0
        if chunk_id == b"fmt " and chunk_size >= 12:
            byte_rate = int.from_bytes(data[chunk_start + 8:chunk_start + 12], "little")
        offset = chunk_start + chunk_size + (chunk_size & 1)
    return data_size / byte_rate if byte_rate and data_size else 0.0


def load_fish_voices(settings: Settings, deadline: float) -> tuple[list[dict[str, str]], str]:
    with settings.voices_cache_lock:
        now = time.monotonic()
        if settings.voices_cache is not None and now - settings.voices_cache_timestamp < FISH_VOICES_CACHE_TTL:
            return settings.voices_cache, "hit"
        path = "/model?" + urlencode({
            "language": settings.tts_language,
            "sort_by": "score",
            "page_size": 50,
        })
        raw = fish_request(settings, "GET", path, deadline)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FishFailure("http_5xx") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise FishFailure("http_5xx")
        voices: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            voice_id = item.get("_id")
            title = item.get("title")
            languages = item.get("languages")
            if not isinstance(voice_id, str) or not isinstance(title, str):
                continue
            language = (
                languages[0]
                if isinstance(languages, list) and languages and isinstance(languages[0], str)
                else settings.tts_language
            )
            voices.append({"id": voice_id, "name": title, "language": language})
            settings.known_voice_ids.add(voice_id)
        if settings.tts_reference_id and all(
            voice["id"] != settings.tts_reference_id for voice in voices
        ):
            voices.insert(0, {
                "id": settings.tts_reference_id,
                "name": settings.tts_reference_id,
                "language": settings.tts_language,
            })
        settings.voices_cache = voices
        settings.voices_cache_timestamp = time.monotonic()
        return voices, "miss"


def remaining_image_timeout(settings: Settings, deadline: float | None) -> float:
    if deadline is None:
        return float(settings.timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BridgeError("image request deadline exceeded", 504, "image_deadline_exceeded")
    return min(float(settings.timeout), remaining)


@contextmanager
def image_semaphore_slot(settings: Settings, deadline: float | None):
    wait_timeout = remaining_image_timeout(settings, deadline)
    if not settings.semaphore.acquire(timeout=wait_timeout):
        raise BridgeError("image request deadline exceeded", 504, "image_deadline_exceeded")
    try:
        yield
    finally:
        settings.semaphore.release()


@contextmanager
def chat_semaphore_slot(settings: Settings, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not settings.semaphore.acquire(timeout=remaining):
        raise BridgeError("chat request deadline exceeded", 504, "cli_timeout")
    try:
        yield
    finally:
        settings.semaphore.release()


def cleanup_image_job(
    settings: Settings, job_dir: Path, operation: str, active_error: BaseException | None,
) -> None:
    if not settings.keep_image_jobs:
        try:
            shutil.rmtree(job_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[{iso_now()}] request={operation} cleanup=failed", flush=True)
            if active_error is not None:
                return
            raise BridgeError("image job cleanup failed", 500, "image_cleanup_failed") from exc
    if settings.keep_image_jobs:
        print(f"[{iso_now()}] request={operation} cleanup=kept path={job_dir}", flush=True)
        return
    print(f"[{iso_now()}] request={operation} cleanup=done", flush=True)


def run_grok_image(
    settings: Settings, body: dict[str, Any], edit: bool, deadline: float | None = None,
) -> tuple[bytes, int, int, Path]:
    prompt_value = body.get("prompt")
    if not isinstance(prompt_value, str) or not prompt_value.strip():
        raise BridgeError("prompt must be a non-empty string", 400, "invalid_request")
    if len(prompt_value) > settings.max_prompt_chars:
        raise BridgeError("prompt too large", 413, "prompt_too_large")
    size = parse_image_size(body)
    width, height = resolved_image_size(settings, size)
    aspect_ratio = grok_aspect_for(width, height)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    job_dir = settings.jobs_dir / f"image-{uuid.uuid4().hex}"
    job_dir.mkdir(mode=0o700, exist_ok=False)
    operation = "image_edit" if edit else "image_generate"
    print(f"[{iso_now()}] [grok/{operation}] {image_size_token(size)} using={width}x{height} aspect_ratio={aspect_ratio}", flush=True)
    try:
        prompt_blocks: list[dict[str, Any]] = []
        raw_inputs: list[Any] = []
        if edit:
            if body.get("image") is not None:
                raw_inputs.append(body.get("image"))
            if isinstance(body.get("images"), list):
                raw_inputs.extend(body["images"])
            if not raw_inputs:
                raise BridgeError("image or images is required for image edit", 400, "invalid_request")
            if len(raw_inputs) > IMAGE_MAX_REFS:
                raise BridgeError(f"at most {IMAGE_MAX_REFS} input images are supported", 400, "invalid_request")
            if len(raw_inputs) > GROK_EDIT_MAX_REFS:
                print(
                    f"[{iso_now()}] [grok/image_edit] refs={len(raw_inputs)} sent={GROK_EDIT_MAX_REFS}",
                    flush=True,
                )
                raw_inputs = raw_inputs[:GROK_EDIT_MAX_REFS]

        request_text = prompt_value.strip()
        prompt_blocks.append({"type": "text", "text": request_text})
        mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        for value in raw_inputs:
            data, suffix = decode_input_image(value, settings.max_body_bytes)
            mime_type = mime_types.get(suffix.lower())
            if mime_type is None:
                raise BridgeError("unsupported input image type", 400, "invalid_request")
            prompt_blocks.append({
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime_type,
            })

        executable, base_args = resolve_grok_launcher(settings.grok_command)
        command = windows_cmd_wrapper(
            executable, [*base_args, *grok_image_acp_args(settings)],
        )
        started = time.monotonic()
        with image_semaphore_slot(settings, deadline):
            result = run_grok_acp(
                command,
                request_text,
                remaining_image_timeout(settings, deadline),
                cwd=job_dir,
                system_prompt=GROK_IMAGE_SYSTEM_PROMPT.format(aspect_ratio=aspect_ratio),
                absolute_deadline=deadline,
                prompt_blocks=prompt_blocks,
                expected_media_type="ImageEdit" if edit else "ImageGen",
                diagnostic_label=f"grok/{operation}",
                private_texts=(prompt_value,),
            )
        if not isinstance(result, GrokMediaOutput):
            raise BridgeError("Grok ACP image result is missing", 502, "image_output_missing")
        data, actual_width, actual_height, mime_type = validate_generated_image(
            result.path, settings.max_body_bytes,
        )
        output_path = result.path
        if mime_type == "image/jpeg":
            # Grok Imagine returns JPEG; the Player2 image contract is base64 PNG.
            output_path = job_dir / "output.png"
            convert_jpeg_to_png(data, output_path, remaining_image_timeout(settings, deadline))
            data, actual_width, actual_height = validate_png(output_path, settings.max_body_bytes, deadline)
            mime_type = "image/png"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"[{iso_now()}] request={operation} done elapsed_ms={elapsed_ms} "
            f"image={actual_width}x{actual_height} bytes={len(data)} mime={mime_type}",
            flush=True,
        )
        return data, actual_width, actual_height, output_path
    finally:
        cleanup_image_job(settings, job_dir, operation, sys.exc_info()[1])


def run_codex_image(
    settings: Settings, body: dict[str, Any], edit: bool, deadline: float | None = None,
) -> tuple[bytes, int, int, Path]:
    prompt_value = body.get("prompt")
    if not isinstance(prompt_value, str) or not prompt_value.strip():
        raise BridgeError("prompt must be a non-empty string", 400, "invalid_request")
    if len(prompt_value) > settings.max_prompt_chars:
        raise BridgeError("prompt too large", 413, "prompt_too_large")
    size = parse_image_size(body)
    width, height = resolved_image_size(settings, size)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    job_dir = settings.jobs_dir / f"image-{uuid.uuid4().hex}"
    job_dir.mkdir(mode=0o700, exist_ok=False)
    output_path = job_dir / "output.png"
    operation = "image_edit" if edit else "image_generate"
    try:
        input_paths: list[Path] = []
        if edit:
            raw_inputs: list[Any] = []
            if body.get("image") is not None:
                raw_inputs.append(body.get("image"))
            if isinstance(body.get("images"), list):
                raw_inputs.extend(body["images"])
            if not raw_inputs:
                raise BridgeError("image or images is required for image edit", 400, "invalid_request")
            if len(raw_inputs) > IMAGE_MAX_REFS:
                raise BridgeError(f"at most {IMAGE_MAX_REFS} input images are supported", 400, "invalid_request")
            for index, value in enumerate(raw_inputs, start=1):
                data, suffix = decode_input_image(value, settings.max_body_bytes)
                input_path = job_dir / f"input-{index:02d}{suffix}"
                input_path.write_bytes(data)
                input_path.chmod(0o600)
                input_paths.append(input_path)

        print(f"[{iso_now()}] [codex/{operation}] {image_size_token(size)} using={width}x{height}", flush=True)
        task = "Edit the supplied reference images" if edit else "Generate a new image"
        references = "\n".join(f"REFERENCE_IMAGE={path}" for path in input_paths)
        codex_prompt = (
            "Use the Codex image generation tool for this request.\n"
            f"{task} according to USER_IMAGE_PROMPT.\n"
            "Call the image generation tool exactly once, asking for WIDTHxHEIGHT; if the tool cannot "
            "produce that exact size, use the closest size it supports.\n"
            "Do not run any shell commands and do not copy, move, resize, crop, convert or otherwise "
            "post-process the image. Do not return base64 in chat.\n"
            "After the tool finishes, reply with exactly one line and nothing else:\n"
            "GENERATED_PNG=<absolute path of the PNG file the tool saved>\n"
            f"WIDTH={width}\nHEIGHT={height}\n"
            f"{references}\n"
            f"USER_IMAGE_PROMPT={prompt_value.strip()}\n"
        )
        executable, base_args = resolve_codex_launcher(settings.codex_command)
        model = settings.codex_model
        args = [*base_args]
        effort_override = codex_effort_override(settings.codex_effort)
        if effort_override:
            args.extend(["-c", effort_override])
        # read-only: Codex only calls its image tool (which saves under CODEX_HOME) and reports
        # the path; the bridge reads that file itself. A sandboxed copy into the job directory
        # produced a file the bridge could not read on Windows (PermissionError, winerror 5).
        args.extend([
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--color", "never",
            "--json",
        ])
        if model:
            args.extend(["--model", model])
        args.append("-")
        command = windows_cmd_wrapper(executable, args)
        started = time.monotonic()
        with image_semaphore_slot(settings, deadline):
            stdout, _ = run_process(
                command, codex_prompt, remaining_image_timeout(settings, deadline), cwd=job_dir,
                label=f"codex/{operation}",
                allowed_secret_env={"OPENAI_API_KEY"},
                absolute_deadline=deadline,
            )
        reported = extract_generated_png_path(stdout)
        if reported is not None:
            data, actual_width, actual_height = read_reported_codex_png(
                reported, settings.max_body_bytes, codex_run_thread_id(stdout), deadline,
            )
            print(f"[{iso_now()}] [codex/{operation}] generated_png=reported", flush=True)
            if settings.keep_image_jobs:
                output_path.write_bytes(data)
        elif output_path.exists():
            # The model copied the file itself despite the instruction; accept it if readable.
            data, actual_width, actual_height = validate_png(output_path, settings.max_body_bytes, deadline)
            print(f"[{iso_now()}] [codex/{operation}] generated_png=output_png", flush=True)
        else:
            raise BridgeError("Codex did not report the generated PNG", 502, "image_output_missing")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"[{iso_now()}] request={operation} done elapsed_ms={elapsed_ms} "
            f"image={actual_width}x{actual_height} bytes={len(data)}",
            flush=True,
        )
        return data, actual_width, actual_height, output_path
    finally:
        cleanup_image_job(settings, job_dir, operation, sys.exc_info()[1])


RETRYABLE_IMAGE_ERROR_CODES = {
    "cli_execution_failed", "cli_failed", "cli_io_failed", "cli_launch_failed",
    "cli_not_found", "cli_output_empty", "cli_output_invalid", "cli_output_too_large",
    "cli_timeout", "image_output_invalid", "image_output_missing", "upstream_error",
}


def run_image_route(settings: Settings, body: dict[str, Any], edit: bool) -> tuple[bytes, int, int, Path]:
    prompt_value = body.get("prompt")
    if isinstance(prompt_value, str) and prompt_value.strip():
        print_console_only_prompt("IMAGE EDIT" if edit else "IMAGE GENERATE", prompt_value.strip())
    runners = {"codex": run_codex_image, "grok": run_grok_image}
    primary = settings.image_provider
    fallback = settings.image_fallback_provider
    deadline = time.monotonic() + settings.timeout
    try:
        return runners[primary](settings, body, edit, deadline)
    except BridgeError as primary_error:
        if primary_error.code not in RETRYABLE_IMAGE_ERROR_CODES or fallback is None:
            raise
        remaining_image_timeout(settings, deadline)
        print(
            f"[{iso_now()}] image_provider={primary} status=failed "
            f"code={primary_error.code} reason={json.dumps(str(primary_error))} fallback={fallback}",
            flush=True,
        )
        try:
            return runners[fallback](settings, body, edit, deadline)
        except BridgeError as fallback_error:
            if fallback_error.code == "cli_termination_failed":
                raise
            raise BridgeError(
                "primary and fallback image providers failed",
                fallback_error.status,
                "image_fallback_failed",
            ) from fallback_error


def run_completion(
    settings: Settings, body: dict[str, Any], mode: str, *, supports_response_format: bool = False,
) -> tuple[str, str, int]:
    schema_json: str | None = None
    json_object = False
    if supports_response_format:
        response_format = body.get("response_format")
        if response_format is not None:
            if not isinstance(response_format, dict) or not isinstance(response_format.get("type"), str):
                raise BridgeError(
                    "response_format must be an object with a type", 400, "invalid_request",
                )
            response_format_type = response_format["type"]
            if response_format_type == "json_schema":
                json_schema = response_format.get("json_schema")
                if not isinstance(json_schema, dict) or not isinstance(json_schema.get("schema"), dict):
                    raise BridgeError(
                        "response_format json_schema must contain a schema object",
                        400,
                        "invalid_request",
                    )
                schema = json_schema["schema"]
            elif response_format_type == "json_object":
                schema = {"type": "object"}
                json_object = True
            elif response_format_type == "text":
                schema = None
            else:
                raise BridgeError("unsupported response_format type", 400, "invalid_request")
            if schema is not None:
                schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                if len(schema_json) > 30000:
                    raise BridgeError("response_format schema is too large", 413, "schema_too_large")
    if mode == "generate":
        system = text_content(body.get("system"))
        user_prompt = text_content(body.get("prompt"))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        system, prompt = split_messages(messages)
    else:
        system, prompt = split_messages(body.get("messages"))
    if len(prompt) > settings.max_prompt_chars:
        raise BridgeError(
            f"prompt too large: {len(prompt)} chars (limit {settings.max_prompt_chars})",
            413,
            "prompt_too_large",
        )
    print_console_only_prompt("CHAT", prompt)
    started = time.monotonic()
    deadline = started + settings.timeout
    native_schema_json: str | None = None
    if schema_json is not None and not json_object:
        native_schema_json = json.dumps(
            schema_with_inferred_types(json.loads(schema_json)), ensure_ascii=False, separators=(",", ":"),
        )

    def run_native_then_prompt(runner: Callable[..., tuple[str, str]], **native_kwargs: Any) -> tuple[str, str]:
        """Claude/Codex enforce a schema natively but reject schemas their strict validators do
        not accept (unknown keywords, non-strict objects). Then retry once with the schema in
        the prompt, as for Grok; the client validates the JSON either way."""
        try:
            return runner(**native_kwargs)
        except BridgeError as error:
            if native_schema_json is None or error.code != "cli_failed" or time.monotonic() >= deadline:
                raise
            print(
                f"[{iso_now()}] structured_output=native_rejected provider={settings.provider} fallback=prompt",
                flush=True,
            )
            retry_kwargs = dict(native_kwargs, schema_json=None, schema_in_prompt=schema_json)
            return runner(**retry_kwargs)

    with chat_semaphore_slot(settings, deadline):
        if settings.provider == "claude":
            text, model = run_native_then_prompt(
                lambda **kw: run_claude(settings, prompt, body.get("model"), deadline, **kw),
                schema_json=native_schema_json,
            )
        elif settings.provider == "codex":
            if json_object:
                text, model = run_codex(settings, prompt, deadline, json_object=True)
            else:
                text, model = run_native_then_prompt(
                    lambda **kw: run_codex(settings, prompt, deadline, **kw),
                    schema_json=native_schema_json,
                )
        elif settings.provider == "grok":
            grok_prompt = prompt
            if system:
                system_prefix = f"[SYSTEM]\n{system}\n\n"
                if not grok_prompt.startswith(system_prefix):
                    raise BridgeError("failed to separate Grok system prompt", 500, "internal_error")
                grok_prompt = grok_prompt[len(system_prefix):]
            text, model = run_grok(
                settings, grok_prompt, deadline, system_prompt=system, schema_json=schema_json,
            )
        else:
            raise BridgeError("unsupported chat provider", 500, "config_invalid")
    if schema_json is not None:
        try:
            json.loads(text)
        except json.JSONDecodeError:
            print(f"[{iso_now()}] structured_output=invalid", flush=True)
    elapsed_ns = int((time.monotonic() - started) * 1_000_000_000)
    return text, model, elapsed_ns


class Handler(BaseHTTPRequestHandler):
    server_version = f"Player2CLIBridge/{VERSION}"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        status = args[1] if len(args) > 1 else "unknown"
        print(f"[{iso_now()}] client={self.address_string()} http_status={status}", flush=True)

    def request_path(self) -> str:
        return urlsplit(self.path).path

    def send_json(self, status: int, body: Any, content_type: str = "application/json; charset=utf-8") -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def send_error_json(self, error: BridgeError) -> None:
        self.send_json(error.status, {"error": {"message": str(error), "type": error.code, "code": error.code}})

    def read_json(self) -> dict[str, Any]:
        data = self.read_raw_body()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid JSON body", 400, "invalid_json") from exc
        if not isinstance(parsed, dict):
            raise BridgeError("JSON body must be an object", 400, "invalid_request")
        return parsed

    def read_raw_body(self) -> bytes:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise BridgeError("Content-Type must be application/json", 415, "unsupported_media_type")
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            raise BridgeError("Content-Length is required", 411, "length_required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BridgeError("invalid Content-Length", 400, "invalid_request") from exc
        if length < 0 or length > self.settings.max_body_bytes:
            raise BridgeError("request body too large", 413, "body_too_large")
        return self.rfile.read(length)

    def proxy_to_player2(self, raw_body: bytes | None = None) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        path = self.request_path()
        started = time.monotonic()
        headers_sent = False
        upstream: http.client.HTTPConnection | None = None
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                self.send_error_json(BridgeError("Content-Length is required", 411, "length_required"))
                print(
                    f"[{iso_now()}] {request_id} passthrough method={self.command} "
                    f"path={path} status=411 elapsed_ms={int((time.monotonic() - started) * 1000)}",
                    flush=True,
                )
                return
            body = raw_body
            if raw_body is None:
                raw_length = self.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        length = int(raw_length)
                    except ValueError as exc:
                        raise BridgeError("invalid Content-Length", 400, "invalid_request") from exc
                    if length < 0:
                        raise BridgeError("invalid Content-Length", 400, "invalid_request")
                    body = self.rfile.read(length)
            request_headers = {
                name: value for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "host"
            }
            request_headers["Host"] = f"{PLAYER2_HOST}:{self.settings.player2_port}"
            upstream = http.client.HTTPConnection(
                PLAYER2_HOST, self.settings.player2_port, timeout=self.settings.timeout,
            )
            upstream.request(self.command, self.path, body=body, headers=request_headers)
            response = upstream.getresponse()
            response_headers = [
                (name, value) for name, value in response.getheaders()
                if name.lower() not in HOP_BY_HOP_HEADERS
            ]
            has_content_length = any(name.lower() == "content-length" for name, _ in response_headers)
            self.send_response(response.status, response.reason)
            for name, value in response_headers:
                self.send_header(name, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            if self.command != "HEAD":
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            if not has_content_length:
                self.close_connection = True
            print(
                f"[{iso_now()}] {request_id} passthrough method={self.command} path={path} "
                f"status={response.status} elapsed_ms={int((time.monotonic() - started) * 1000)}",
                flush=True,
            )
        except BridgeError as exc:
            print(
                f"[{iso_now()}] {request_id} passthrough method={self.command} path={path} "
                f"status={exc.status} elapsed_ms={int((time.monotonic() - started) * 1000)}",
                flush=True,
            )
            self.send_error_json(exc)
        except OSError:
            if headers_sent:
                self.close_connection = True
            else:
                self.send_error_json(BridgeError(
                    f"Player2 app is not reachable at {PLAYER2_HOST}:{self.settings.player2_port}",
                    502,
                    "upstream_unavailable",
                ))
            print(
                f"[{iso_now()}] {request_id} passthrough method={self.command} "
                f"path={path} status=upstream_unavailable",
                flush=True,
            )
        finally:
            if upstream is not None:
                upstream.close()

    def tunnel_to_player2(self) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        path = self.request_path()
        started = time.monotonic()
        try:
            upstream = socket.create_connection(
                (PLAYER2_HOST, self.settings.player2_port), timeout=self.settings.timeout,
            )
        except OSError:
            self.send_error_json(BridgeError(
                f"Player2 app is not reachable at {PLAYER2_HOST}:{self.settings.player2_port}",
                502,
                "upstream_unavailable",
            ))
            print(
                f"[{iso_now()}] {request_id} passthrough method={self.command} "
                f"path={path} status=upstream_unavailable",
                flush=True,
            )
            return

        print(
            f"[{iso_now()}] {request_id} passthrough method={self.command} path={path} status=tunnel",
            flush=True,
        )
        relay_threads: list[threading.Thread] = []
        try:
            request_head = f"{self.command} {self.path} {self.request_version}\r\n".encode("iso-8859-1")
            for name, value in self.headers.items():
                if name.lower() == "host":
                    value = f"{PLAYER2_HOST}:{self.settings.player2_port}"
                request_head += f"{name}: {value}\r\n".encode("iso-8859-1")
            upstream.sendall(request_head + b"\r\n")
            previous_timeout = self.connection.gettimeout()
            self.connection.setblocking(False)
            try:
                buffered = self.rfile.peek(0)
            except BlockingIOError:
                buffered = b""
            finally:
                self.connection.settimeout(previous_timeout)
            if buffered:
                upstream.sendall(self.rfile.read(len(buffered)))

            self.connection.settimeout(None)
            upstream.settimeout(None)

            def relay(source: socket.socket, destination: socket.socket) -> None:
                try:
                    while True:
                        data = source.recv(65536)
                        if not data:
                            break
                        destination.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            relay_threads = [
                threading.Thread(target=relay, args=(self.connection, upstream), daemon=True),
                threading.Thread(target=relay, args=(upstream, self.connection), daemon=True),
            ]
            for thread in relay_threads:
                thread.start()
            for thread in relay_threads:
                thread.join()
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            finally:
                self.connection.close()
                self.close_connection = True
            for thread in relay_threads:
                if thread.is_alive():
                    thread.join()
            print(
                f"[{iso_now()}] {request_id} passthrough method={self.command} path={path} "
                f"status=tunnel_closed elapsed_ms={int((time.monotonic() - started) * 1000)}",
                flush=True,
            )

    def dispatch_passthrough(self) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.tunnel_to_player2()
        else:
            self.proxy_to_player2()

    def tts_failure(
        self, request_id: str, failure: FishFailure, raw_body: bytes | None = None,
    ) -> None:
        if self.settings.tts_fallback:
            fallback = "passthrough" if self.settings.passthrough_allowed(self.request_path()) else "stub"
        else:
            fallback = "error"
        print(
            f"[{iso_now()}] {request_id} status=fish_failed code={failure.code} fallback={fallback}",
            flush=True,
        )
        if fallback == "passthrough":
            self.proxy_to_player2(raw_body=raw_body)
        elif fallback == "stub":
            if self.request_path() in TTS_VOICES_PATHS:
                self.send_json(200, {"voices": []})
            else:
                self.send_error_json(feature_not_implemented("tts"))
        else:
            self.send_error_json(BridgeError(
                f"Fish Audio TTS failed: {failure.code}", 502, "tts_failed",
            ))

    def handle_tts_voices(self, request_id: str) -> None:
        started = time.monotonic()
        print(f"[{iso_now()}] {request_id} request=tts_voices provider=fish status=started", flush=True)
        try:
            voices, cache = load_fish_voices(self.settings, started + self.settings.timeout)
        except FishFailure as failure:
            self.tts_failure(request_id, failure)
            return
        print(
            f"[{iso_now()}] {request_id} request=tts_voices provider=fish status=200 "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)} count={len(voices)} cache={cache}",
            flush=True,
        )
        self.send_json(200, {"voices": voices})

    def prepare_tts_request(self, raw_body: bytes, deadline: float) -> tuple[dict[str, Any], str]:
        fish_body, audio_format, requested_voice_id = parse_tts_body(self.settings, raw_body)
        if self.settings.voices_cache is None:
            try:
                load_fish_voices(self.settings, deadline)
            except FishFailure:
                print(f"[{iso_now()}] voices_cache=unavailable", flush=True)
        if requested_voice_id in self.settings.known_voice_ids:
            fish_body["reference_id"] = requested_voice_id
        elif self.settings.tts_reference_id:
            fish_body["reference_id"] = self.settings.tts_reference_id
        return fish_body, audio_format

    def handle_tts_speak(self, request_id: str, raw_body: bytes) -> None:
        started = time.monotonic()
        deadline = started + self.settings.timeout
        print(f"[{iso_now()}] {request_id} request=tts_speak provider=fish status=started", flush=True)
        fish_body, audio_format = self.prepare_tts_request(raw_body, deadline)
        try:
            audio = fish_request(self.settings, "POST", "/v1/tts", deadline, fish_body)
        except FishFailure as failure:
            self.tts_failure(request_id, failure, raw_body)
            return
        duration = tts_duration(self.settings, audio_format, audio)
        print(
            f"[{iso_now()}] {request_id} request=tts_speak provider=fish status=200 "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)} bytes={len(audio)}",
            flush=True,
        )
        encoded = base64.b64encode(audio).decode("ascii")
        # The current Player2 spec names the base64 audio "data"; older clients read "audio".
        self.send_json(200, {
            "data": encoded,
            "audio": encoded,
            "format": audio_format,
            "mimetype": TTS_MIMETYPES[audio_format],
            "duration": duration,
        })

    def handle_tts_stream(self, request_id: str, raw_body: bytes) -> None:
        started = time.monotonic()
        deadline = started + self.settings.timeout
        print(f"[{iso_now()}] {request_id} request=tts_stream provider=fish status=started", flush=True)
        fish_body, audio_format = self.prepare_tts_request(raw_body, deadline)
        response = None
        headers_sent = False
        byte_count = 0
        try:
            response = fish_open(self.settings, "POST", "/v1/tts", deadline, fish_body)
            raw_content_length = response.headers.get("Content-Length")
            expected_length = int(raw_content_length) if raw_content_length and raw_content_length.isdigit() else None
            if expected_length is not None and expected_length > FISH_MAX_RESPONSE_BYTES:
                raise FishFailure("output_too_large")
            try:
                first = response.read(65536)
            except (URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
                raise FishFailure(fish_failure_code(exc)) from exc
            if not first:
                raise FishFailure("network")
            self.send_response(200)
            self.send_header("Content-Type", TTS_MIMETYPES[audio_format])
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self.wfile.write(first)
            self.wfile.flush()
            byte_count = len(first)
            while True:
                try:
                    chunk = response.read(65536)
                except (URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
                    raise FishFailure(fish_failure_code(exc)) from exc
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > FISH_MAX_RESPONSE_BYTES:
                    raise FishFailure("output_too_large")
                self.wfile.write(chunk)
                self.wfile.flush()
            if expected_length is not None and byte_count != expected_length:
                raise FishFailure("network")
        except FishFailure as failure:
            if headers_sent:
                self.close_connection = True
                print(
                    f"[{iso_now()}] {request_id} status=fish_failed code={failure.code} "
                    "fallback=none stage=streaming",
                    flush=True,
                )
            else:
                self.tts_failure(request_id, failure, raw_body)
            return
        finally:
            if response is not None:
                response.close()
        self.close_connection = True
        print(
            f"[{iso_now()}] {request_id} request=tts_stream provider=fish status=200 "
            f"elapsed_ms={int((time.monotonic() - started) * 1000)} bytes={byte_count}",
            flush=True,
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        path = self.request_path()
        if self.settings.tts_active and path in TTS_VOICES_PATHS:
            self.handle_tts_voices(f"req-{uuid.uuid4().hex[:12]}")
            return
        if path not in BRIDGE_GET_PATHS and self.settings.passthrough_allowed(path):
            self.dispatch_passthrough()
            return
        if path == "/":
            data = f"Game AI CLI Bridge {VERSION} ({self.settings.provider}) is running\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True
            return
        if path in ("/health", "/v1/health"):
            self.send_json(200, {
                "status": "ok", "healthy": True, "ready": True,
                "client_version": VERSION,
                "version": VERSION, "provider": self.settings.provider,
                "capabilities": {
                    "chat": True,
                    "image_generate": True,
                    "image_edit": True,
                    "embeddings": bool(self.settings.openai_api_key),
                    "tts": self.settings.tts_active or self.settings.passthrough_allowed("/v1/tts/"),
                    "stt": self.settings.passthrough_allowed("/v1/stt/"),
                },
            })
            return
        if path == "/api/version":
            self.send_json(200, {"version": f"0.1.50-cli-bridge-{VERSION}"})
            return
        if path == "/api/tags":
            prefix = self.settings.provider
            aliases = ["sonnet", "haiku", "opus", "fable"] if prefix == "claude" else ["default"]
            self.send_json(200, {"models": [{
                "name": f"{prefix}-{alias}:latest", "model": f"{prefix}-{alias}:latest",
                "modified_at": iso_now(), "size": 0, "digest": f"sha256:{prefix}-cli",
                "details": {"format": "cli", "family": prefix, "families": [prefix]},
            } for alias in aliases]})
            return
        if path == "/v1/models":
            name = f"{self.settings.provider}-cli"
            self.send_json(200, {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local"}]})
            return
        if path in TTS_VOICES_PATHS:
            print(f"[{iso_now()}] [UNAVAILABLE] feature=tts_voices status=200 empty_list=true", flush=True)
            self.send_json(200, {"voices": []})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        try:
            path = self.request_path()
            if self.settings.tts_active and path in TTS_SPEAK_PATHS | TTS_STREAM_PATHS:
                origin = self.headers.get("Origin")
                if origin:
                    parsed_origin = urlsplit(origin)
                    if parsed_origin.scheme not in ("http", "https") or parsed_origin.hostname not in (
                        "127.0.0.1", "localhost", "::1"
                    ):
                        raise BridgeError("cross-origin requests are not allowed", 403, "origin_forbidden")
                raw_body = self.read_raw_body()
                if path in TTS_SPEAK_PATHS:
                    self.handle_tts_speak(request_id, raw_body)
                else:
                    self.handle_tts_stream(request_id, raw_body)
                return
            if path not in BRIDGE_POST_PATHS and self.settings.passthrough_allowed(path):
                self.dispatch_passthrough()
                return
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlsplit(origin)
                if parsed_origin.scheme not in ("http", "https") or parsed_origin.hostname not in (
                    "127.0.0.1", "localhost", "::1"
                ):
                    raise BridgeError("cross-origin requests are not allowed", 403, "origin_forbidden")
            if path in TTS_SPEAK_PATHS | TTS_STREAM_PATHS:
                raise feature_not_implemented("tts")
            if path in ("/v1/stt/start", "/stt/start", "/v1/stt/stop", "/stt/stop"):
                raise feature_not_implemented("stt")
            body = self.read_json()
            if path == "/api/show":
                name = str(body.get("name") or f"{self.settings.provider}-cli:latest")
                self.send_json(200, {
                    "modelfile": f"# Game AI CLI Bridge: {name}",
                    "parameters": "", "template": "{{ .Prompt }}",
                    "details": {"format": "cli", "family": self.settings.provider},
                })
                return
            if path in ("/v1/embeddings", "/embeddings"):
                print(f"[{iso_now()}] {request_id} request=embeddings status=started", flush=True)
                self.send_json(200, create_embeddings(self.settings, body))
                return
            if path in ("/v1/image/generate", "/image/generate", "/v1/image/edit", "/image/edit"):
                edit = path.endswith("/edit")
                operation = "image_edit" if edit else "image_generate"
                print(f"[{iso_now()}] {request_id} request={operation} status=started", flush=True)
                image_data, _, _, _ = run_image_route(self.settings, body, edit)
                response: dict[str, Any] = {"image": base64.b64encode(image_data).decode("ascii")}
                if edit:
                    response["mimetype"] = generated_image_mime_type(image_data)
                self.send_json(200, response)
                return
            if path not in ("/api/chat", "/api/generate", "/v1/chat/completions", "/chat/completions"):
                raise BridgeError("unsupported endpoint", 404, "unsupported_endpoint")
            mode = "generate" if path == "/api/generate" else "chat"
            response_format_type = None
            if path in ("/v1/chat/completions", "/chat/completions"):
                response_format = body.get("response_format")
                if response_format is not None:
                    raw_response_format_type = (
                        response_format.get("type") if isinstance(response_format, dict) else None
                    )
                    response_format_type = (
                        raw_response_format_type
                        if isinstance(raw_response_format_type, str)
                        and raw_response_format_type in {"json_schema", "json_object", "text"}
                        else "invalid"
                    )
            structured_log = (
                f" structured_output={response_format_type}" if response_format_type is not None else ""
            )
            print(
                f"[{iso_now()}] {request_id} request={mode} provider={self.settings.provider} "
                f"status=started{structured_log}",
                flush=True,
            )
            text, effective_model, elapsed_ns = run_completion(
                self.settings,
                body,
                mode,
                supports_response_format=path in ("/v1/chat/completions", "/chat/completions"),
            )
            print(
                f"[{iso_now()}] {request_id} provider={self.settings.provider} "
                f"model={effective_model} chars={len(text)} elapsed_ms={elapsed_ns // 1_000_000}",
                flush=True,
            )
            requested_model = str(body.get("model") or f"{self.settings.provider}-cli")
            if path.startswith("/api/"):
                now = iso_now()
                if path == "/api/chat":
                    first = {
                        "model": requested_model, "created_at": now,
                        "message": {"role": "assistant", "content": text}, "done": not bool(body.get("stream")),
                    }
                    final = {
                        "model": requested_model, "created_at": now,
                        "message": {"role": "assistant", "content": ""}, "done": True,
                        "done_reason": "stop", "total_duration": elapsed_ns,
                        "load_duration": 0, "prompt_eval_count": 0, "eval_count": 0,
                    }
                else:
                    first = {
                        "model": requested_model, "created_at": now,
                        "response": text, "done": not bool(body.get("stream")),
                    }
                    final = {
                        "model": requested_model, "created_at": now,
                        "response": "", "done": True, "done_reason": "stop",
                        "total_duration": elapsed_ns, "load_duration": 0,
                    }
                if body.get("stream"):
                    payload = (
                        json.dumps(first, ensure_ascii=False, separators=(",", ":")) + "\n" +
                        json.dumps(final, ensure_ascii=False, separators=(",", ":")) + "\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                else:
                    first.update(final)
                    if path == "/api/chat":
                        first["message"] = {"role": "assistant", "content": text}
                    else:
                        first["response"] = text
                    self.send_json(200, first)
                return
            created = int(time.time())
            completion = {
                "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion",
                "created": created, "model": effective_model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            if body.get("stream"):
                chunk = {
                    "id": completion["id"], "object": "chat.completion.chunk", "created": created,
                    "model": effective_model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
                }
                done = {
                    "id": completion["id"], "object": "chat.completion.chunk", "created": created,
                    "model": effective_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                payload = (
                    f"data: {json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    f"data: {json.dumps(done, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True
            else:
                self.send_json(200, completion)
        except BridgeError as exc:
            detail_token = f" detail={exc.detail}" if exc.detail else ""
            print(f"[{iso_now()}] {request_id} error={exc.code} status={exc.status}{detail_token}", flush=True)
            self.send_error_json(exc)
        except Exception:
            print(f"[{iso_now()}] {request_id} error=internal status=500", flush=True)
            self.send_error_json(BridgeError("internal bridge error", 500, "internal_error"))

    def do_PUT(self) -> None:
        if self.settings.passthrough_allowed(self.request_path()):
            self.dispatch_passthrough()
            return
        self.send_json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        if self.settings.passthrough_allowed(self.request_path()):
            self.dispatch_passthrough()
            return
        self.send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if self.settings.passthrough_allowed(self.request_path()):
            self.dispatch_passthrough()
            return
        self.send_json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        if self.settings.passthrough_allowed(self.request_path()):
            self.dispatch_passthrough()
            return
        self.send_json(404, {"error": "not found"})


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], settings: Settings) -> None:
        self.settings = settings
        self.request_slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        del request, client_address
        print(f"[{iso_now()}] server_error=request_failed", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Game AI CLI Bridge: Player2-compatible local API served by Claude/Codex/Grok CLI"
    )
    parser.add_argument("--provider", choices=("claude", "codex", "grok"), required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--max-body-mb", type=int, default=20)
    parser.add_argument("--max-prompt-chars", type=int, default=1_000_000)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--cli-model", default=None, help="Override current provider model")
    parser.add_argument("--cli-effort", default=None, help="Override current provider reasoning effort")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--grok-command", default="grok")

    parser.add_argument(
        "--jobs-dir",
        default=None,
        help="Bridge-owned directory for per-request job files (image jobs and Codex schema files)",
    )
    parser.add_argument("--keep-image-jobs", action="store_true", help="Keep generated image job directories for debugging")
    parser.add_argument(
        "--dump-cli-events", default=None, metavar="DIR",
        help="Write the raw JSON event lines of every CLI run to DIR (contains request content; diagnostics only)",
    )
    parser.add_argument("--openai-base-url", default="https://api.openai.com/v1")
    parser.add_argument("--fish-base-url", default=FISH_BASE_URL)
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    return parser.parse_args()


def provider_model_effort(settings: Settings, provider: str) -> tuple[str, str | None]:
    if provider == "claude":
        return settings.claude_model, settings.claude_effort
    if provider == "codex":
        return settings.codex_model or "CLI default", settings.codex_effort
    if provider == "grok":
        return settings.grok_model, settings.grok_effort
    raise BridgeError("unsupported provider", 500, "config_invalid")


def main() -> int:
    reset_process_lifecycle()
    args = parse_args()
    if args.dump_cli_events:
        globals()["CLI_EVENT_DUMP_DIR"] = Path(args.dump_cli_events).expanduser().resolve()
    if args.host != "127.0.0.1":
        print("보안을 위해 127.0.0.1 bind만 허용합니다.", file=sys.stderr)
        return 2
    try:
        openai_base_url = urlsplit(args.openai_base_url)
        openai_host = openai_base_url.hostname or ""
        if openai_base_url.scheme == "https":
            openai_base_url_allowed = bool(openai_host)
        elif openai_base_url.scheme == "http" and openai_host:
            openai_base_url_allowed = (
                openai_host == "localhost" or ipaddress.ip_address(openai_host).is_loopback
            )
        else:
            openai_base_url_allowed = False
    except ValueError:
        openai_base_url_allowed = False
    if not openai_base_url_allowed:
        print("--openai-base-url 은 https 또는 loopback http 만 허용합니다.", file=sys.stderr)
        return 2
    try:
        fish_base_url = urlsplit(args.fish_base_url)
        fish_host = fish_base_url.hostname or ""
        if fish_base_url.scheme == "https":
            fish_base_url_allowed = bool(fish_host)
        elif fish_base_url.scheme == "http" and fish_host:
            fish_base_url_allowed = fish_host == "localhost" or ipaddress.ip_address(fish_host).is_loopback
        else:
            fish_base_url_allowed = False
    except ValueError:
        fish_base_url_allowed = False
    if not fish_base_url_allowed:
        print("--fish-base-url 은 https 또는 loopback http 만 허용합니다.", file=sys.stderr)
        return 2
    if args.timeout < 1 or args.max_concurrency < 1:
        print("timeout/max-concurrency 값이 잘못됐습니다.", file=sys.stderr)
        return 2
    try:
        settings = Settings(args)
    except BridgeError as exc:
        print(f"Bridge setup failed: {exc.code}", file=sys.stderr)
        return 2
    if not (1 <= settings.port <= 65535):
        print("port 값이 잘못됐습니다.", file=sys.stderr)
        return 2
    try:
        server = BridgeServer((settings.host, settings.port), settings)
    except OSError:
        print("Bridge bind failed", file=sys.stderr)
        return 2
    print("=" * 64)
    print(f" Game AI CLI Bridge {VERSION}")
    print("=" * 64)
    print(f" Provider : {settings.provider}")
    active_model, active_effort = provider_model_effort(settings, settings.provider)
    image_cli_model, image_cli_effort = provider_model_effort(settings, settings.image_provider)
    print(f" Model    : {active_model}")
    print(f" Effort   : {active_effort or 'CLI default'}")
    print(f" Image CLI: {settings.image_provider}")
    print(f" Img Model: {image_cli_model}")
    print(f" Img Effort: {image_cli_effort or 'CLI default'}")
    print(f" Image    : {settings.image_width}x{settings.image_height} (used when a request has no size)")
    print(" Config   : loaded")
    print(f" URL      : http://{settings.host}:{settings.port}")
    print(" Backend  : Player2")
    if settings.port == settings.player2_port:
        print(
            " Player2  : passthrough disabled "
            f"(bridge port equals the Player2 port {settings.player2_port})"
        )
    elif settings.passthrough_mode == "list":
        print(
            f" Player2  : passthrough=list ({len(settings.passthrough_allow)} prefixes) "
            f"-> {PLAYER2_HOST}:{settings.player2_port}"
        )
    elif settings.passthrough_mode == "all":
        print(f" Player2  : passthrough=all -> {PLAYER2_HOST}:{settings.player2_port}")
    else:
        print(" Player2  : passthrough=none")
    print(f" Embedding: {'configured' if settings.openai_api_key else 'disabled'}")
    if settings.tts_provider == "none":
        print(" TTS      : none")
    elif not settings.fish_api_key:
        print(" TTS      : fish (key not configured, falling back)")
    else:
        print(
            f" TTS      : fish model={settings.tts_model} "
            f"voice={settings.tts_reference_id or 'default'} lang={settings.tts_language} "
            f"fallback={'on' if settings.tts_fallback else 'off'}"
        )
    print(" Stop     : Ctrl+C")
    print("=" * 64, flush=True)
    shutdown_exit_code = 0
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nBridge stopped.")
    finally:
        if not shutdown_active_processes():
            print("Bridge shutdown cleanup failed", file=sys.stderr)
            shutdown_exit_code = 3
        try:
            server.server_close()
        except OSError:
            print("Bridge server close failed", file=sys.stderr)
            shutdown_exit_code = 3
    return shutdown_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
