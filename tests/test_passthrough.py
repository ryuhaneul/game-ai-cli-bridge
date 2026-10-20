#!/usr/bin/env python3
"""Acceptance tests for configurable port and Player2 passthrough."""

from __future__ import annotations

import base64
import stat
import struct
import http.client
import importlib.util
import inspect
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "player2_cli_bridge.py"
FAKE_GROK = ROOT / "tests" / "fake_grok_acp.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude_cli.py"
FAKE_CODEX = ROOT / "tests" / "fake_codex_cli.py"
FAKE_FISH = ROOT / "tests" / "fake_fish.py"
PLAYER2_PORT = 4315
BRIDGE_PORT = 4399
OVERRIDE_PORT = 4400


class FakePlayer2Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args

    def _handle(self) -> None:
        self.server.request_count += 1  # type: ignore[attr-defined]
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            )
            if self.path == "/close-first":
                self.server.closed_first.set()  # type: ignore[attr-defined]
                self.close_connection = True
                return
            while True:
                data = self.connection.recv(65536)
                if not data:
                    self.server.client_eof.set()  # type: ignore[attr-defined]
                    break
                self.connection.sendall(data)
            self.close_connection = True
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.last_body = body  # type: ignore[attr-defined]
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/destination")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in (b"one", b"two", b"three"):
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.2)
            self.close_connection = True
            return
        path, _, query = self.path.partition("?")
        payload = json.dumps({
            "method": self.command,
            "path": path,
            "query": query,
            "body_len": len(body),
            "content_type": self.headers.get("Content-Type", ""),
        }, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle


class FakePlayer2Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int = PLAYER2_PORT) -> None:
        self.client_eof = threading.Event()
        self.closed_first = threading.Event()
        self.request_count = 0
        self.last_body = b""
        super().__init__(("127.0.0.1", port), FakePlayer2Handler)


@contextmanager
def fake_player2(port: int = PLAYER2_PORT) -> Iterator[FakePlayer2Server]:
    server = FakePlayer2Server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def config_file(
    folder: Path, port: int, mode: str, allow: list[str] | None = None,
    player2_port: int | None = None, tts: dict[str, Any] | None = None,
) -> Path:
    path = folder / f"config-{port}-{mode}.json"
    player2 = {
        "passthrough": mode,
        "allow": allow if allow is not None else ["/v1/tts/", "/v1/stt/"],
    }
    if player2_port is not None:
        player2["port"] = player2_port
    config: dict[str, Any] = {
        "port": port,
        "player2": player2,
    }
    if tts is not None:
        config["tts"] = tts
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@contextmanager
def bridge_process(
    folder: Path,
    config: Path,
    *,
    override_port: int | None = None,
    provider: str = "grok",
    jobs_dir: Path | None = None,
    openai_base_url: str | None = None,
    fish_base_url: str | None = None,
    fish_key: bool = False,
    timeout: int = 10,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[subprocess.Popen[str], int]]:
    command = [
        sys.executable, str(BRIDGE), "--provider", provider, "--config", str(config),
        "--timeout", str(timeout),
    ]
    if extra_args:
        command.extend(extra_args)
    fake_commands = {
        "grok": ("--grok-command", str(FAKE_GROK)),
        "claude": ("--claude-command", str(FAKE_CLAUDE.relative_to(ROOT))),
        "codex": ("--codex-command", str(FAKE_CODEX.relative_to(ROOT))),
    }
    command_flag, fake_command = fake_commands[provider]
    command.extend([command_flag, fake_command])
    port = json.loads(config.read_text(encoding="utf-8"))["port"]
    if override_port is not None:
        command.extend(["--port", str(override_port)])
        port = override_port
    if jobs_dir is not None:
        command.extend(["--jobs-dir", str(jobs_dir)])
    if openai_base_url is not None:
        command.extend(["--openai-base-url", openai_base_url])
    if fish_base_url is not None:
        command.extend(["--fish-base-url", fish_base_url])
    environment = os.environ.copy()
    environment.pop("PLAYER2_FISH_AUDIO_KEY", None)
    if fish_key:
        environment["PLAYER2_FISH_AUDIO_KEY"] = "test-key"
    environment["GROK_HOME"] = str(folder / "grok-home")
    if extra_env:
        environment.update(extra_env)
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"bridge exited early ({process.returncode}): {output}")
        try:
            status, _, _ = request(port, "GET", "/v1/health")
            if status == 200:
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise AssertionError(f"bridge did not listen on {port}")
    try:
        yield process, port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def fake_fish(folder: Path) -> Iterator[tuple[str, Path, Path]]:
    dump_path = folder / "fake-fish-dump.json"
    count_path = folder / "fake-fish-count.txt"
    dump_path.unlink(missing_ok=True)
    count_path.unlink(missing_ok=True)
    count_path.with_suffix(".bad").unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["FAKE_FISH_DUMP"] = str(dump_path)
    environment["FAKE_FISH_COUNT"] = str(count_path)
    process = subprocess.Popen(
        [sys.executable, str(FAKE_FISH)], cwd=ROOT, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    port_line = process.stdout.readline().strip()
    if not port_line:
        error = process.stderr.read() if process.stderr else ""
        raise AssertionError(f"fake Fish failed to start: {error}")
    try:
        yield f"http://127.0.0.1:{int(port_line)}", dump_path, count_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def request(
    port: int, method: str, path: str, body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, data
    finally:
        connection.close()


def json_request(
    port: int, method: str, path: str, body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    status, response_headers, data = request(port, method, path, body, headers)
    return status, response_headers, json.loads(data)


def assert_refused(port: int) -> None:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        assert sock.connect_ex(("127.0.0.1", port)) != 0
    finally:
        sock.close()


def recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(65536)
        assert chunk
        data += chunk
    return data


def recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(min(65536, length - len(data)))
        assert chunk
        data.extend(chunk)
    return bytes(data)


def raw_response(request_bytes: bytes) -> tuple[bytes, bytes]:
    sock = socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=5)
    try:
        sock.sendall(request_bytes)
        response = recv_until(sock, b"\r\n\r\n")
        head, body = response.split(b"\r\n\r\n", 1)
        time.sleep(0.5)
        sock.settimeout(0.5)
        try:
            body += sock.recv(65536)
        except socket.timeout:
            pass
        return head, body
    finally:
        sock.close()


def minimal_png_bytes() -> bytes:
    import struct, zlib
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def criterion_1(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg):
        assert request(BRIDGE_PORT, "GET", "/v1/health")[0] == 200
    with bridge_process(folder, cfg, override_port=OVERRIDE_PORT):
        assert request(OVERRIDE_PORT, "GET", "/v1/health")[0] == 200
        assert_refused(BRIDGE_PORT)


def criterion_2(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2(), bridge_process(folder, cfg):
        calls = [
            ("GET", "/v1/tts/voices", None, {}),
            ("POST", "/v1/tts/speak", b'{"text":"hello"}', {"Content-Type": "application/json"}),
            ("POST", "/v1/stt/audio", b"x" * (1024 * 1024), {"Content-Type": "application/octet-stream"}),
            ("GET", "/anything?x=1&y=2", None, {}),
            ("PUT", "/x", b"put", {}),
            ("DELETE", "/x", None, {}),
        ]
        results = []
        for method, path, body, headers in calls:
            status, _, payload = json_request(BRIDGE_PORT, method, path, body, headers)
            assert status == 200
            results.append(payload)
        assert results[3]["query"] == "x=1&y=2"
        assert results[2]["body_len"] == 1048576
        assert results[2]["content_type"] == "application/octet-stream"
        assert [item["method"] for item in results] == ["GET", "POST", "POST", "GET", "PUT", "DELETE"]


def criterion_3(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2(), bridge_process(folder, cfg):
        status, headers, body = request(BRIDGE_PORT, "GET", "/redirect")
        assert status == 302 and headers["location"] == "/destination" and body == b""


def criterion_4(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2(), bridge_process(folder, cfg):
        status, headers, body = request(BRIDGE_PORT, "GET", "/stream")
        assert status == 200 and "content-length" not in headers
        assert body == b"onetwothree"


def criterion_5(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg):
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/tts/speak", b"{}", {"Content-Type": "application/json"},
        )
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
        status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
        assert status == 200 and body == {"voices": []}
        assert request(BRIDGE_PORT, "GET", "/anything")[0] == 404


def criterion_6(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "list", ["/v1/tts/"])
    with fake_player2(), bridge_process(folder, cfg):
        status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
        assert status == 200 and body["path"] == "/v1/tts/voices"
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/stt/start", b"{}", {"Content-Type": "application/json"},
        )
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
        assert request(BRIDGE_PORT, "GET", "/anything")[0] == 404


def criterion_7(folder: Path) -> None:
    cfg = config_file(folder, PLAYER2_PORT, "all")
    with bridge_process(folder, cfg) as (process, _):
        status, _, body = json_request(
            PLAYER2_PORT, "POST", "/v1/tts/speak", b"{}", {"Content-Type": "application/json"},
        )
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
        process.terminate()
        process.wait(timeout=5)
        output = process.stdout.read() if process.stdout else ""
        assert "passthrough disabled" in output


def criterion_8(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with bridge_process(folder, cfg):
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/tts/speak", b"{}", {"Content-Type": "application/json"},
        )
        assert status == 502 and body["error"]["code"] == "upstream_unavailable"


def criterion_9(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2() as upstream, bridge_process(folder, cfg):
        sock = socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=5)
        sock.sendall(
            b"GET /v1/stt/stream HTTP/1.1\r\nHost: 127.0.0.1:4399\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        response = recv_until(sock, b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 101")
        sock.sendall(b"hello")
        assert recv_exact(sock, 5) == b"hello"
        payload = os.urandom(1024 * 1024)
        sock.sendall(payload)
        assert recv_exact(sock, len(payload)) == payload
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(65536):
            pass
        sock.close()
        assert upstream.client_eof.wait(5)
        assert request(BRIDGE_PORT, "GET", "/v1/health")[0] == 200

        sock = socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=5)
        sock.sendall(
            b"GET /close-first HTTP/1.1\r\nHost: 127.0.0.1:4399\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        response = recv_until(sock, b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 101")
        assert upstream.closed_first.wait(5)
        assert sock.recv(1) == b""
        sock.close()


def criterion_10(folder: Path) -> None:
    cases = [
        ("all", None, True, True),
        ("none", None, False, False),
        ("list", ["/v1/tts/"], True, False),
    ]
    for mode, allow, tts, stt in cases:
        cfg = config_file(folder, BRIDGE_PORT, mode, allow)
        with bridge_process(folder, cfg):
            status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/health")
            assert status == 200
            assert body["capabilities"]["tts"] is tts
            assert body["capabilities"]["stt"] is stt


def criterion_11(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2(), bridge_process(folder, cfg):
        chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"},
        )
        assert status == 200 and body["choices"][0]["message"]["content"] == "Hail, traveler."
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions", chat,
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        assert status == 403 and body["error"]["code"] == "origin_forbidden"
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/tts/speak", b"{}",
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        assert status == 200 and body["path"] == "/v1/tts/speak"


def criterion_12(folder: Path) -> None:
    del folder
    subprocess.run([sys.executable, "-m", "py_compile", str(BRIDGE)], check=True)


def criterion_13(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all")
    with fake_player2() as upstream, bridge_process(folder, cfg):
        for transfer_encoding in (b"gzip, chunked", b"chunked"):
            head, _ = raw_response(
                b"POST /v1/tts/speak HTTP/1.1\r\nHost: 127.0.0.1:4399\r\n"
                b"Transfer-Encoding: " + transfer_encoding + b"\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n"
            )
            assert head.startswith(b"HTTP/1.1 411")
        assert upstream.request_count == 0


def criterion_14(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg):
        head, body = raw_response(
            b"HEAD /anything HTTP/1.1\r\nHost: 127.0.0.1:4399\r\nConnection: close\r\n\r\n"
        )
        assert head.startswith(b"HTTP/1.1 404") and body == b""

    cfg = config_file(folder, BRIDGE_PORT, "all")
    with bridge_process(folder, cfg):
        head, body = raw_response(
            b"HEAD /v1/tts/speak HTTP/1.1\r\nHost: 127.0.0.1:4399\r\nConnection: close\r\n\r\n"
        )
        assert head.startswith(b"HTTP/1.1 502") and body == b""

    with fake_player2(), bridge_process(folder, cfg):
        head, body = raw_response(
            b"HEAD /v1/tts/speak HTTP/1.1\r\nHost: 127.0.0.1:4399\r\nConnection: close\r\n\r\n"
        )
        assert head.startswith(b"HTTP/1.1 200") and body == b""


def criterion_15(folder: Path) -> None:
    grok_hooks = folder / "grok-home" / "hooks"
    grok_hooks.mkdir(parents=True, exist_ok=True)
    new_hook = grok_hooks / "player2-cli-bridge-deny-tools.json"
    if new_hook.exists():
        new_hook.unlink()

    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg) as (process, _):
        chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions", chat,
            {"Content-Type": "application/json"},
        )
        assert status == 200 and body["choices"][0]["message"]["content"] == "Hail, traveler."

    output = process.stdout.read() if process.stdout else ""
    assert new_hook.is_file()
    assert "PLAYER2_CLI_BRIDGE_DENY_TOOLS" in new_hook.read_text(encoding="utf-8")
    assert "grok_hook=installed" in output


def criterion_16(folder: Path) -> None:
    player2_port = 4316
    cfg = config_file(folder, BRIDGE_PORT, "all", player2_port=player2_port)
    with fake_player2(player2_port), bridge_process(folder, cfg):
        status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
        assert status == 200 and body["path"] == "/v1/tts/voices"


def criterion_17(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "all", player2_port=BRIDGE_PORT)
    with bridge_process(folder, cfg) as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/tts/speak", b"{}", {"Content-Type": "application/json"},
        )
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
        process.terminate()
        process.wait(timeout=5)
        output = process.stdout.read() if process.stdout else ""
        assert "passthrough disabled" in output


def structured_chat(response_format: Any, *, stream: bool = False) -> bytes:
    return json.dumps({
        "messages": [{"role": "user", "content": "Return structured output"}],
        "response_format": response_format,
        "stream": stream,
    }).encode()


def criterion_18(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    dump_path = folder / "grok-home" / "fake_acp_dump.json"
    schema = {"title": "votc_actions", "type": "object"}
    with bridge_process(folder, cfg) as (process, _):
        status, _, _ = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"name": "votc_actions", "schema": schema}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
    output = process.stdout.read() if process.stdout else ""
    dumped_prompt = json.loads(dump_path.read_text(encoding="utf-8"))["systemPromptOverride"]
    assert "Structured output required" in dumped_prompt
    assert '"title":"votc_actions"' in dumped_prompt
    assert "request=chat provider=grok status=started structured_output=json_schema" in output
    assert "votc_actions" not in output


def criterion_19(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    schema = {"title": "votc_actions", "type": "object"}
    with bridge_process(folder, cfg, provider="claude"):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"schema": schema}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert json.loads(body["choices"][0]["message"]["content"]) == {
            "ok": True,
            "schema_title": "votc_actions",
        }
        status, headers, stream_body = request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat(
                {"type": "json_schema", "json_schema": {"schema": schema}},
                stream=True,
            ),
            {"Content-Type": "application/json"},
        )
        assert status == 200 and headers["content-type"].startswith("text/event-stream")
        first_event = json.loads(stream_body.splitlines()[0].removeprefix(b"data: "))
        assert json.loads(first_event["choices"][0]["delta"]["content"])["ok"] is True
        plain_chat = json.dumps({
            "messages": [{"role": "user", "content": "Return text"}],
        }).encode()
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            plain_chat,
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "plain result"


def criterion_20(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    jobs_dir = folder / "codex-jobs"
    schema = {"title": "votc_actions", "type": "object"}
    with bridge_process(folder, cfg, provider="codex", jobs_dir=jobs_dir):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"schema": schema}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        content = json.loads(body["choices"][0]["message"]["content"])
        assert content["ok"] is True and content["had_schema"] is True
        assert content["ephemeral"] is True
        assert not list(jobs_dir.glob("chat-*"))


def criterion_21(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    dump_path = folder / "grok-home" / "fake_acp_dump.json"
    with bridge_process(folder, cfg):
        status, _, _ = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_object"}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        dumped_prompt = json.loads(dump_path.read_text(encoding="utf-8"))["systemPromptOverride"]
        assert '{"type":"object"}' in dumped_prompt

        invalid_formats = ["json", {"type": "xml"}]
        for response_format in invalid_formats:
            status, _, body = json_request(
                BRIDGE_PORT,
                "POST",
                "/v1/chat/completions",
                structured_chat(response_format),
                {"Content-Type": "application/json"},
            )
            assert status == 400 and body["error"]["code"] == "invalid_request"

        oversized_schema = {"value": "x" * 29989}
        assert len(json.dumps(oversized_schema, separators=(",", ":"))) == 30001
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"schema": oversized_schema}}),
            {"Content-Type": "application/json"},
        )
        assert status == 413 and body["error"]["code"] == "schema_too_large"

        ollama_chat = json.dumps({
            "messages": [{"role": "user", "content": "Ignore response format"}],
            "response_format": "json",
        }).encode()
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/api/chat",
            ollama_chat,
            {"Content-Type": "application/json"},
        )
        assert status == 200 and body["message"]["content"] == "Hail, traveler."


def criterion_22(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    grok_home = folder / "grok-home"
    grok_home.mkdir(parents=True, exist_ok=True)
    (grok_home / "fake_acp_reply.txt").write_text("plain", encoding="utf-8")
    with bridge_process(folder, cfg) as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_object"}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "plain"
    output = process.stdout.read() if process.stdout else ""
    assert "structured_output=invalid" in output


def criterion_23(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    jobs_dir = folder / "codex-rejected-jobs"
    schema = {"type": "object", "x-fake-reject": True}
    with bridge_process(folder, cfg, provider="codex", jobs_dir=jobs_dir) as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"schema": schema}}),
            {"Content-Type": "application/json"},
        )
        # Codex rejects the schema (strict mode); the bridge retries once with the schema in the prompt
        assert status == 200, body
        content = json.loads(body["choices"][0]["message"]["content"])
        assert content["had_schema"] is False and content["prompt_flag"] is True and content["prompt_has_schema"] is True
    output = process.stdout.read() if process.stdout else ""
    assert "error_code=invalid_json_schema" in output
    assert "structured_output=native_rejected provider=codex fallback=prompt" in output
    assert "missing_ephemeral" not in output


def criterion_24(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    jobs_dir = folder / "codex-json-object-jobs"
    with bridge_process(folder, cfg, provider="codex", jobs_dir=jobs_dir):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_object"}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        content = json.loads(body["choices"][0]["message"]["content"])
        assert content["had_schema"] is False and content["prompt_flag"] is True
        assert content["ephemeral"] is True
        assert not list(jobs_dir.glob("chat-*"))


def criterion_25(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    forged_type = "xml\nFORGED_LOG=SCHEMA_SECRET"
    with bridge_process(folder, cfg) as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": forged_type}),
            {"Content-Type": "application/json"},
        )
        assert status == 400 and body["error"]["code"] == "invalid_request"
    output = process.stdout.read() if process.stdout else ""
    assert "FORGED_LOG" not in output and "SCHEMA_SECRET" not in output
    assert output.count("structured_output=invalid") == 1


def criterion_26(folder: Path) -> None:
    if os.name == "nt":
        print("   SKIP criterion_26 POSIX mode assertions", flush=True)
        return
    cfg = config_file(folder, BRIDGE_PORT, "none")
    jobs_dir = folder / "codex-mode-jobs"
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    with bridge_process(folder, cfg, provider="codex", jobs_dir=jobs_dir):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"schema": schema}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200
        content = json.loads(body["choices"][0]["message"]["content"])
        assert content["dir_mode"] == "0o700" and content["file_mode"] == "0o600"
        assert not list(jobs_dir.glob("chat-*"))


def criterion_27(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    jobs_dir = folder / "codex-private-jobs"
    chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
    with bridge_process(folder, cfg, provider="codex", jobs_dir=jobs_dir):
        status, _, body = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            chat,
            {"Content-Type": "application/json"},
        )
        assert status == 200
        content = json.loads(body["choices"][0]["message"]["content"])
        cwd = Path(content["cwd"])
        assert content["ephemeral"] is True
        assert cwd.parent == jobs_dir and cwd.name.startswith("chat-")
        assert not cwd.exists()


def criterion_28(folder: Path) -> None:
    if os.name == "nt":
        print("   SKIP criterion_28 POSIX mode assertions", flush=True)
        return
    cfg = config_file(folder, BRIDGE_PORT, "none")
    dump_path = folder / "grok-home" / "fake_acp_dump.json"
    with bridge_process(folder, cfg):
        chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        status, _, _ = json_request(
            BRIDGE_PORT,
            "POST",
            "/v1/chat/completions",
            chat,
            {"Content-Type": "application/json"},
        )
        assert status == 200
        assert json.loads(dump_path.read_text(encoding="utf-8"))["cwd_mode"] == "0o700"

    spec = importlib.util.spec_from_file_location("player2_cli_bridge_under_test", BRIDGE)
    assert spec is not None and spec.loader is not None
    bridge_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_module)
    module_source = inspect.getsource(bridge_module)
    assert module_source.count("mkdir(mode=0o700, exist_ok=False)") == 4
    codex_image_source = inspect.getsource(bridge_module.run_codex_image)
    assert '"exec",\n            "--ephemeral",\n            "--skip-git-repo-check",\n            "--sandbox", "read-only"' in codex_image_source
    assert "input_path.chmod(0o600)" in codex_image_source


def criterion_29(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none")
    command = [
        sys.executable, str(BRIDGE), "--provider", "grok", "--config", str(cfg),
        "--grok-command", str(FAKE_GROK),
        "--openai-base-url", "http://evil.example/v1",
    ]
    process = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=5, check=False,
    )
    assert process.returncode == 2
    assert "--openai-base-url 은 https 또는 loopback http 만 허용합니다." in process.stderr
    assert_refused(BRIDGE_PORT)

    # https without a hostname must be rejected too
    process = subprocess.run(
        command[:-1] + ["https://"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=False,
    )
    assert process.returncode == 2
    assert_refused(BRIDGE_PORT)

    for allowed_url in ("http://127.0.0.1:9/v1", "http://127.0.0.2:9/v1", "http://localhost:9/v1"):
        with bridge_process(folder, cfg, openai_base_url=allowed_url):
            status, _, _ = request(BRIDGE_PORT, "GET", "/v1/health")
            assert status == 200


def fish_tts_config(
    *, reference_id: str | None = "d" * 32, fallback: bool = True, sample_rate: int | None = None
) -> dict[str, Any]:
    config = {
        "provider": "fish",
        "model": "s2.1-pro-free",
        "reference_id": reference_id,
        "language": "ko",
        "fallback": fallback,
    }
    if sample_rate is not None:
        config["sample_rate"] = sample_rate
    return config


def tts_request(port: int, path: str, body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
    return json_request(
        port, "POST", path, json.dumps(body, separators=(",", ":")).encode(),
        {"Content-Type": "application/json"},
    )


def criterion_30(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with bridge_process(folder, cfg) as (process, _):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hello"})
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
    output = process.stdout.read() if process.stdout else ""
    assert "TTS      : fish (key not configured, falling back)" in output


def criterion_31(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, dump_path, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hello"})
        assert status == 200 and body["format"] == "mp3" and body["mimetype"] == "audio/mpeg"
        assert body["duration"] > 0 and len(base64.b64decode(body["audio"])) == 16000
        dumped = json.loads(dump_path.read_text(encoding="utf-8"))
        assert dumped["model"] == "s2.1-pro-free"
        assert dumped["authorization"] == "Bearer test-key"
        assert dumped["body"]["reference_id"] == "d" * 32


def criterion_32(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, dump_path, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        status, _, _ = tts_request(
            BRIDGE_PORT, "/v1/tts/speak", {"text": "known", "voice_ids": ["b" * 32]},
        )
        assert status == 200
        assert json.loads(dump_path.read_text(encoding="utf-8"))["body"]["reference_id"] == "b" * 32
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {
            "text": "unknown", "voice_ids": ["zzz-unknown"], "audio_format": "ogg", "speed": 1.5,
        })
        assert status == 200 and body["format"] == "mp3"
        dumped = json.loads(dump_path.read_text(encoding="utf-8"))["body"]
        assert dumped["reference_id"] == "d" * 32
        assert dumped["format"] == "mp3" and dumped["prosody"]["speed"] == 1.5


def criterion_33(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        status, _, body = tts_request(
            BRIDGE_PORT, "/v1/tts/speak", {"text": "wav", "audio_format": "wav"},
        )
        assert status == 200 and body["format"] == "wav" and body["mimetype"] == "audio/wav"
        assert abs(body["duration"] - 1.0) <= 0.01
        assert body["duration"] == round(body["duration"], 3)
        # streamed WAV with placeholder RIFF/data sizes (as Fish Audio sends): duration from received bytes
        status, _, body = tts_request(
            BRIDGE_PORT, "/v1/tts/speak", {"text": "wavstream", "audio_format": "wav"},
        )
        assert status == 200 and abs(body["duration"] - 1.0) <= 0.01


def criterion_34(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, dump_path, count_path), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        for _ in range(2):
            status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
            assert status == 200
            assert [voice["name"] for voice in body["voices"]] == ["d" * 32, "Voice A", "Voice B"]
        assert count_path.read_text(encoding="ascii") == "1"
        status, _, _ = tts_request(
            BRIDGE_PORT, "/v1/tts/speak", {"text": "known-set", "voice_ids": ["b" * 32]},
        )
        assert status == 200
        assert json.loads(dump_path.read_text(encoding="utf-8"))["body"]["reference_id"] == "b" * 32


def criterion_35(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        raw = json.dumps({"text": "stream"}).encode()
        status, headers, body = request(
            BRIDGE_PORT, "POST", "/v1/tts/stream", raw, {"Content-Type": "application/json"},
        )
        assert status == 200 and headers["content-type"].startswith("audio/")
        assert body == b"ID3" + b"\0" * 15997
        connection = http.client.HTTPConnection("127.0.0.1", BRIDGE_PORT, timeout=10)
        connection.request(
            "POST", "/v1/tts/stream", json.dumps({"text": "CUTMID"}).encode(),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        try:
            cut_body = response.read()
        except http.client.IncompleteRead as exc:
            cut_body = exc.partial
        connection.close()
        assert len(cut_body) == 65536
        assert request(BRIDGE_PORT, "GET", "/v1/health")[0] == 200
    output = process.stdout.read() if process.stdout else ""
    assert "fallback=none stage=streaming" in output


def criterion_36(folder: Path) -> None:
    raw = json.dumps({"text": "FAIL402"}, separators=(",", ":")).encode()
    cfg = config_file(folder, BRIDGE_PORT, "all", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), fake_player2() as upstream, bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            status, _, body = json_request(
                BRIDGE_PORT, "POST", path, raw, {"Content-Type": "application/json"},
            )
            assert status == 200 and body["path"] == path and upstream.last_body == raw
    output = process.stdout.read() if process.stdout else ""
    assert output.count("code=payment_required fallback=passthrough") == 2

    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            status, _, body = json_request(
                BRIDGE_PORT, "POST", path, raw, {"Content-Type": "application/json"},
            )
            assert status == 501 and body["error"]["code"] == "feature_not_implemented"

    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config(fallback=False))
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True, timeout=3,
    ) as (process, _):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            status, _, body = tts_request(BRIDGE_PORT, path, {"text": "FAIL402"})
            assert status == 502 and body["error"]["code"] == "tts_failed"
        status, _, _ = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "FAIL401"})
        assert status == 502
        status, _, _ = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "HANG"})
        assert status == 502
    output = process.stdout.read() if process.stdout else ""
    assert "code=unauthorized fallback=error" in output
    assert "code=timeout fallback=error" in output


def criterion_37(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        status, _, health = json_request(BRIDGE_PORT, "GET", "/v1/health")
        assert status == 200 and health["capabilities"]["tts"] is True
        assert tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "SECRETTEXT"})[0] == 200
        chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        assert json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"},
        )[0] == 200
        dump_path = folder / "grok-home" / "fake_acp_dump.json"
        assert json.loads(dump_path.read_text(encoding="utf-8"))["fish_key_present"] is False
    output = process.stdout.read() if process.stdout else ""
    assert "SECRETTEXT" not in output and "test-key" not in output


def criterion_38(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        for body in ({}, {"text": "bad", "speed": 5}, {"text": "bad", "audio_format": {}}):
            status, _, response = tts_request(BRIDGE_PORT, "/v1/tts/speak", body)
            assert status == 400 and response["error"]["code"] == "invalid_request"
        # unknown format strings are served as mp3 (the Player2 default) instead of rejected
        status, _, response = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "ok", "audio_format": "flac"})
        assert status == 200 and response["format"] == "mp3" and response["mimetype"] == "audio/mpeg"
        # JSON null for optional fields means "absent"; audio_format is case-insensitive
        status, _, response = tts_request(
            BRIDGE_PORT, "/v1/tts/speak",
            {"text": "ok", "voice_ids": None, "audio_format": None, "speed": None},
        )
        assert status == 200 and response["format"] == "mp3"
        status, _, response = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "ok", "audio_format": "MP3"})
        assert status == 200 and response["format"] == "mp3"


def criterion_39(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    command = [
        sys.executable, str(BRIDGE), "--provider", "grok", "--config", str(cfg),
        "--grok-command", str(FAKE_GROK), "--fish-base-url", "http://evil.example",
    ]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5, check=False)
    assert process.returncode == 2
    assert "--fish-base-url 은 https 또는 loopback http 만 허용합니다." in process.stderr
    assert_refused(BRIDGE_PORT)


def criterion_40(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts={"provider": "none"})
    with bridge_process(folder, cfg):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/stream", {"text": "hello"})
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"


def criterion_41(folder: Path) -> None:
    invalid_tts_values = [
        {"provider": []},
        {"provider": 1},
        {"fallback": "yes"},
        {"language": []},
        {"model": {}},
        {"sample_rate": 22050},
        {"sample_rate": "44100"},
        {"sample_rate": True},
    ]
    for index, tts in enumerate(invalid_tts_values):
        cfg = folder / f"invalid-tts-{index}.json"
        cfg.write_text(json.dumps({
            "port": BRIDGE_PORT,
            "player2": {"passthrough": "none"},
            "tts": tts,
        }), encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable, str(BRIDGE), "--provider", "grok", "--config", str(cfg),
                "--grok-command", str(FAKE_GROK),
            ],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=False,
        )
        assert process.returncode == 2
        assert "Bridge setup failed: config_invalid" in process.stderr
        assert "Traceback" not in process.stderr
        assert_refused(BRIDGE_PORT)


def criterion_42(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    invalid_bodies = [
        {"text": "bad", "audio_format": {}},
        {"text": "bad", "audio_format": []},
        {"text": "bad", "speed": "fast"},
        {"text": "bad", "speed": True},
        {"text": "bad", "voice_ids": "x"},
        {"text": 5},
    ]
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            for body in invalid_bodies:
                status, _, response = tts_request(BRIDGE_PORT, path, body)
                assert status == 400 and response["error"]["code"] == "invalid_request"


def criterion_43(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            status, _, body = tts_request(BRIDGE_PORT, path, {"text": "BADHTTP"})
            assert status == 501 and body["error"]["code"] == "feature_not_implemented"
    output = process.stdout.read() if process.stdout else ""
    assert output.count("code=network fallback=stub") == 2

    with fake_fish(folder) as (base_url, _, count_path):
        count_path.with_suffix(".bad").write_text("1", encoding="ascii")
        with bridge_process(folder, cfg, fish_base_url=base_url, fish_key=True) as (process, _):
            status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
            assert status == 200 and body == {"voices": []}
    output = process.stdout.read() if process.stdout else ""
    assert "code=network fallback=stub" in output

    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config(fallback=False))
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        for path in ("/v1/tts/speak", "/v1/tts/stream"):
            status, _, body = tts_request(BRIDGE_PORT, path, {"text": "BADHTTP"})
            assert status == 502 and body["error"]["code"] == "tts_failed"
    output = process.stdout.read() if process.stdout else ""
    assert output.count("code=network fallback=error") == 2

    with fake_fish(folder) as (base_url, _, count_path):
        count_path.with_suffix(".bad").write_text("1", encoding="ascii")
        with bridge_process(folder, cfg, fish_base_url=base_url, fish_key=True) as (process, _):
            status, _, body = json_request(BRIDGE_PORT, "GET", "/v1/tts/voices")
            assert status == 502 and body["error"]["code"] == "tts_failed"
    output = process.stdout.read() if process.stdout else ""
    assert "code=network fallback=error" in output


def criterion_44(folder: Path) -> None:
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/stream", {"text": "BIGLEN"})
        assert status == 501 and body["error"]["code"] == "feature_not_implemented"
        status, headers, body_bytes = request(
            BRIDGE_PORT,
            "POST",
            "/v1/tts/stream",
            json.dumps({"text": "MAXLEN"}).encode(),
            {"Content-Type": "application/json"},
        )
        assert status == 200 and headers["content-type"].startswith("audio/")
        assert len(body_bytes) == 65536
    output = process.stdout.read() if process.stdout else ""
    assert "code=output_too_large fallback=stub" in output
    assert "fallback=none stage=streaming" in output


def criterion_45(folder: Path) -> None:
    # pcm requests are served as raw 16-bit mono samples at tts.sample_rate; wav/pcm send sample_rate
    # to Fish, mp3 does not.
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config(sample_rate=16000))
    with fake_fish(folder) as (base_url, dump_path, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hi", "audio_format": "pcm"})
        assert status == 200 and body["format"] == "pcm" and body["mimetype"] == "audio/pcm"
        assert body["duration"] == 1.0
        assert len(base64.b64decode(body["audio"])) == 32000
        assert body["data"] == body["audio"]
        sent = json.loads(dump_path.read_text(encoding="utf-8"))["body"]
        assert sent["format"] == "pcm" and sent["sample_rate"] == 16000 and "mp3_bitrate" not in sent
        status, headers, body_bytes = request(
            BRIDGE_PORT,
            "POST",
            "/v1/tts/stream",
            json.dumps({"text": "hi", "audio_format": "pcm"}).encode(),
            {"Content-Type": "application/json"},
        )
        assert status == 200 and headers["content-type"] == "audio/pcm" and len(body_bytes) == 32000
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hi", "audio_format": "wav"})
        assert status == 200 and body["format"] == "wav"
        sent = json.loads(dump_path.read_text(encoding="utf-8"))["body"]
        assert sent["format"] == "wav" and sent["sample_rate"] == 16000
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hi", "audio_format": "mp3"})
        assert status == 200 and body["format"] == "mp3"
        sent = json.loads(dump_path.read_text(encoding="utf-8"))["body"]
        assert "sample_rate" not in sent and sent["mp3_bitrate"] == 128
    output = process.stdout.read() if process.stdout else ""
    assert "tts_format=" not in output


def criterion_46(folder: Path) -> None:
    # Image size semantics: request width/height or aspect_ratio, else the configured default; Codex
    # gets references as files plus the size in the prompt and must not post-process the tool output.
    spec = importlib.util.spec_from_file_location("bridge_for_criterion_46", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    none = module.parse_image_size({"prompt": "x"})
    assert none == {"aspect_ratio": None, "width": None, "height": None}
    assert module.parse_image_size({"width": 1100, "height": 640})["width"] == 1100
    assert module.parse_image_size({"aspect_ratio": "16:9", "width": 5, "height": 5})["aspect_ratio"] == "16:9"
    for bad in ({"width": 100, "height": 640}, {"width": 1100}, {"height": "640", "width": 640},
                {"aspect_ratio": "7:5"}, {"aspect_ratio": 1.5}, {"width": True, "height": 640}):
        try:
            module.parse_image_size(bad)
        except module.BridgeError as error:
            assert error.status == 400 and error.code == "invalid_request", bad
        else:
            raise AssertionError(f"accepted {bad}")

    class FakeSettings:
        image_width = 1100
        image_height = 640

    settings = FakeSettings()
    assert module.resolved_image_size(settings, none) == (1100, 640)
    assert module.resolved_image_size(settings, module.parse_image_size({"width": 800, "height": 800})) == (800, 800)
    w, h = module.resolved_image_size(settings, module.parse_image_size({"aspect_ratio": "9:16"}))
    assert w == 1100 and abs(w / h - 9 / 16) < 0.01
    assert module.grok_aspect_for(1100, 640) == "16:9"
    assert module.grok_aspect_for(1024, 1024) == "1:1"
    assert module.grok_aspect_for(2100, 900) == "20:9"
    assert "auto" not in {module.grok_aspect_for(w, h) for w, h in ((128, 4096), (4096, 128), (700, 700))}
    assert "{aspect_ratio}" in module.GROK_IMAGE_SYSTEM_PROMPT
    source = inspect.getsource(module.run_codex_image)
    assert "REFERENCE_IMAGE=" in source and '"-i"' not in source
    assert "resize" in source and "exactly once" in source and "WIDTH={width}" in source and "GENERATED_PNG=" in source
    assert "Do not read files" not in source
    # image.width/height are configuration keys again (128-4096)
    for index, image in enumerate((
        {"provider": "grok", "fallback_provider": None, "width": 100, "height": 640},
        {"provider": "grok", "fallback_provider": None, "width": "1100", "height": 640},
        {"provider": "grok", "fallback_provider": None, "width": 1100, "height": True},
    )):
        cfg = folder / f"invalid-image-size-{index}.json"
        cfg.write_text(json.dumps({"port": BRIDGE_PORT, "player2": {"passthrough": "none"}, "image": image}), encoding="utf-8")
        process = subprocess.run(
            [sys.executable, str(BRIDGE), "--provider", "grok", "--config", str(cfg), "--grok-command", str(FAKE_GROK)],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=False,
        )
        assert process.returncode == 2 and "Bridge setup failed: config_invalid" in process.stderr, image
        assert_refused(BRIDGE_PORT)


def criterion_47(folder: Path) -> None:
    # --dump-cli-events writes the raw CLI event lines of each run to a file (opt-in diagnostics).
    dump_dir = folder / "cli-events"
    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg, provider="codex", extra_args=["--dump-cli-events", str(dump_dir)]) as (process, _):
        chat = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        status, _, body = request(BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
        assert status == 200, body[:200]
    output = process.stdout.read() if process.stdout else ""
    assert "cli_event_dump=" in output
    files = sorted(dump_dir.glob("*.jsonl"))
    assert len(files) == 1, files
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines and all(json.loads(line).get("type") for line in lines)
    assert any(json.loads(line).get("type") == "turn.completed" for line in lines)
    # without the flag nothing is written
    cfg2 = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg2, provider="codex") as (process, _):
        status, _, _ = request(BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
        assert status == 200
    output = process.stdout.read() if process.stdout else ""
    assert "cli_event_dump=" not in output and len(sorted(dump_dir.glob("*.jsonl"))) == 1
    if os.name == "posix":
        assert stat.S_IMODE(dump_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    # Grok ACP runs are dumped too
    grok_dump = folder / "cli-events-grok"
    cfg3 = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg3, provider="grok", extra_args=["--dump-cli-events", str(grok_dump)]) as (process, _):
        status, _, body = request(BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
        assert status == 200, body[:200]
    grok_files = sorted(grok_dump.glob("*grok*.jsonl"))
    assert len(grok_files) == 1, grok_files
    grok_lines = [json.loads(line) for line in grok_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(item.get("method") == "session/update" for item in grok_lines)
    assert all(item.get("jsonrpc") == "2.0" for item in grok_lines)


def criterion_48(folder: Path) -> None:
    # validate_png waits for an output.png that exists but cannot be opened yet (Windows antivirus
    # lock) instead of calling it corrupt; a genuinely bad file is still rejected with diagnostics.
    spec = importlib.util.spec_from_file_location("bridge_for_criterion_48", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    png = folder / "locked.png"
    png.write_bytes(minimal_png_bytes())
    png.chmod(0)
    if os.access(png, os.R_OK):
        return  # running as root: the lock cannot be simulated here
    threading.Timer(1.0, lambda: png.chmod(0o600)).start()
    started = time.monotonic()
    data, width, height = module.validate_png(png, 10 * 1024 * 1024)
    assert (width, height) == (1, 1) and 0.5 < time.monotonic() - started < 10
    # a file that stays unreadable is given up at the request deadline, not after 15 s
    stuck = folder / "stuck.png"
    stuck.write_bytes(minimal_png_bytes())
    stuck.chmod(0)
    started = time.monotonic()
    try:
        module.validate_png(stuck, 10 * 1024 * 1024, time.monotonic() + 1.0)
    except module.BridgeError as error:
        assert error.code == "image_output_invalid"
    else:
        raise AssertionError("unreadable file accepted")
    finally:
        stuck.chmod(0o600)
    assert time.monotonic() - started < 3.0
    stuck.chmod(0)
    started = time.monotonic()
    try:
        module.validate_png(stuck, 10 * 1024 * 1024, time.monotonic() + 0.05)
    except module.BridgeError:
        pass
    else:
        raise AssertionError("unreadable file accepted")
    finally:
        stuck.chmod(0o600)
    assert time.monotonic() - started < 0.2  # never sleeps past the deadline
    # the first read fails, the wait reaches the deadline: no retry may start after it, even if
    # that retry would succeed
    calls = {"n": 0}
    real_reader = module.read_complete_png

    def flaky_reader(path, max_bytes, *, raise_os_error=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "locked")
        return real_reader(path, max_bytes, raise_os_error=raise_os_error)

    module.read_complete_png = flaky_reader
    try:
        module.validate_png(png, 10 * 1024 * 1024, time.monotonic() + 0.05)
    except module.BridgeError as error:
        assert error.code == "image_output_invalid"
    else:
        raise AssertionError("a retry after the deadline returned success")
    finally:
        module.read_complete_png = real_reader
    assert calls["n"] == 1
    bad = folder / "bad.png"
    bad.write_bytes(b"not a png at all, just text")
    try:
        module.validate_png(bad, 10 * 1024 * 1024)
    except module.BridgeError as error:
        assert error.code == "image_output_invalid" and "valid PNG" in str(error)
    else:
        raise AssertionError("bad file accepted")


def criterion_49(folder: Path) -> None:
    # Codex image handoff: Codex reports the PNG its tool saved under CODEX_HOME/generated_images and
    # the bridge reads it there; paths outside that folder are rejected; no report -> missing.
    codex_home = folder / "codex-home"
    cfg = folder / "codex-image.json"
    cfg.write_text(json.dumps({
        "port": BRIDGE_PORT, "player2": {"passthrough": "none"},
        "image": {"provider": "codex", "fallback_provider": None},
    }), encoding="utf-8")
    with bridge_process(folder, cfg, provider="codex", extra_env={"CODEX_HOME": str(codex_home)}) as (process, _):
        status, _, body = request(
            BRIDGE_PORT, "POST", "/v1/image/generate",
            json.dumps({"prompt": "a chapel", "width": 640, "height": 320}).encode(), {"Content-Type": "application/json"},
        )
        assert status == 200, body[:200]
        data = base64.b64decode(json.loads(body)["image"])
        assert data.startswith(b"\x89PNG") and struct.unpack(">II", data[16:24]) == (2, 1)
        assert list((codex_home / "generated_images").glob("*/exec-*.png"))
        status, _, body = request(
            BRIDGE_PORT, "POST", "/v1/image/generate",
            json.dumps({"prompt": "FAKE_OUTSIDE"}).encode(), {"Content-Type": "application/json"},
        )
        assert status == 502 and json.loads(body)["error"]["code"] == "image_output_invalid", body[:200]
        status, _, body = request(
            BRIDGE_PORT, "POST", "/v1/image/generate",
            json.dumps({"prompt": "FAKE_NOPATH"}).encode(), {"Content-Type": "application/json"},
        )
        assert status == 502 and json.loads(body)["error"]["code"] == "image_output_missing", body[:200]
        def generate(prompt: str) -> tuple[int, dict]:
            status, _, body = request(
                BRIDGE_PORT, "POST", "/v1/image/generate",
                json.dumps({"prompt": prompt}).encode(), {"Content-Type": "application/json"},
            )
            return status, json.loads(body)

        # an image of another (earlier or concurrent) Codex run is refused, even though it is new
        # to the folder listing: only this run's generated_images/<thread id>/ counts
        other = sorted((codex_home / "generated_images").glob("*/exec-*.png"))[0]
        status, body = generate(f"FAKE_REPORT={other}")
        assert status == 502 and body["error"]["code"] == "image_output_invalid", body
        # only .png files, inside this run's folder
        status, body = generate("FAKE_TXT")
        assert status == 502 and body["error"]["code"] == "image_output_invalid", body
        # the path must be in the final message, not an earlier one
        status, body = generate("FAKE_EARLY")
        assert status == 502 and body["error"]["code"] == "image_output_missing", body
        # no run id -> nothing can be tied to this run
        status, body = generate("FAKE_NOTHREAD")
        assert status == 502 and body["error"]["code"] == "image_output_invalid", body
        # a new file that is not a PNG: rejected, and the log never contains its bytes
        status, _, body = request(
            BRIDGE_PORT, "POST", "/v1/image/generate",
            json.dumps({"prompt": "FAKE_BADPNG"}).encode(), {"Content-Type": "application/json"},
        )
        assert status == 502 and json.loads(body)["error"]["code"] == "image_output_invalid", body[:200]
    output = process.stdout.read() if process.stdout else ""
    assert "generated_png=reported" in output
    assert output.count("generated_png=rejected reason=not_this_run") == 2  # FAKE_OUTSIDE + other run
    assert "generated_png=rejected reason=not_png" in output
    assert "generated_png=rejected reason=no_run_id" in output
    assert "output_invalid bytes=" in output and "kind=other" in output
    assert "head=" not in output and "SECRET-TOKEN" not in output and "534543524554" not in output.upper()
    assert "using=640x320" in output


def criterion_50(folder: Path) -> None:
    # ogg is served as mp3 and logged like any other unsupported format; a Grok ACP error's upstream
    # HTTP status is logged as a number with a hint (never the provider's text).
    cfg = config_file(folder, BRIDGE_PORT, "none", tts=fish_tts_config())
    with fake_fish(folder) as (base_url, _, _), bridge_process(
        folder, cfg, fish_base_url=base_url, fish_key=True,
    ) as (process, _):
        status, _, body = tts_request(BRIDGE_PORT, "/v1/tts/speak", {"text": "hi", "audio_format": "ogg"})
        assert status == 200 and body["format"] == "mp3"
    output = process.stdout.read() if process.stdout else ""
    assert "tts_format=ogg served=mp3" in output
    cfg2 = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg2, provider="grok") as (process, _):
        chat = json.dumps({"messages": [{"role": "user", "content": "FAKE_402"}]}).encode()
        status, _, body = request(BRIDGE_PORT, "POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
        assert status == 502 and json.loads(body)["error"]["code"] == "cli_failed", body[:200]
    output = process.stdout.read() if process.stdout else ""
    assert "acp_error method=session/prompt upstream_http_status=402 hint=usage_or_credits_exhausted" in output
    assert "usage balance exhausted" not in output
    # more than 8 reference images are refused for Grok as for Codex (then at most 3 are sent)
    cfg3 = folder / "grok-image-refs.json"
    cfg3.write_text(json.dumps({
        "port": BRIDGE_PORT, "player2": {"passthrough": "none"},
        "image": {"provider": "grok", "fallback_provider": None},
    }), encoding="utf-8")
    with bridge_process(folder, cfg3, provider="grok") as (process, _):
        nine = ["data:image/png;base64," + base64.b64encode(minimal_png_bytes()).decode()] * 9
        status, _, body = request(
            BRIDGE_PORT, "POST", "/v1/image/edit",
            json.dumps({"prompt": "x", "images": nine}).encode(), {"Content-Type": "application/json"},
        )
        assert status == 400 and json.loads(body)["error"]["code"] == "invalid_request", body[:200]


def criterion_51(folder: Path) -> None:
    # VOTC-style action schemas: a `const` without `type` gets its type filled in so Codex's strict
    # mode accepts it natively; a schema a CLI still rejects (Claude: unknown keyword) is retried
    # once with the schema in the prompt.
    spec = importlib.util.spec_from_file_location("bridge_for_criterion_51", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    typed = module.schema_with_inferred_types({
        "type": "object",
        "properties": {
            "const": {"type": "string"},  # a property named "const" is not a const keyword
            "pick": {"anyOf": [{"const": "attack"}, {"const": 3}, {"const": True}]},
            "mixed": {"enum": [1, None]},
            "colour": {"enum": ["red", "blue"]},
        },
    })
    assert typed["properties"]["const"] == {"type": "string"}
    assert [branch["type"] for branch in typed["properties"]["pick"]["anyOf"]] == ["string", "integer", "boolean"]
    assert "type" not in typed["properties"]["mixed"]
    assert typed["properties"]["colour"]["type"] == "string"

    action_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["actions"],
        "properties": {"actions": {"type": "array", "items": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["actionId"],
             "properties": {"actionId": {"const": "wave"}}},
        ]}}},
    }
    cfg = config_file(folder, BRIDGE_PORT, "none")
    with bridge_process(folder, cfg, provider="codex", jobs_dir=folder / "codex-typed-jobs") as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"name": "actions", "schema": action_schema, "strict": True}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200, body
        assert json.loads(body["choices"][0]["message"]["content"])["had_schema"] is True
    output = process.stdout.read() if process.stdout else ""
    assert "native_rejected" not in output

    rejected = {"type": "object", "properties": {"pick": {"const": "a", "x-fake-reject": True}}}
    with bridge_process(folder, cfg, provider="claude") as (process, _):
        status, _, body = json_request(
            BRIDGE_PORT, "POST", "/v1/chat/completions",
            structured_chat({"type": "json_schema", "json_schema": {"name": "actions", "schema": rejected, "strict": True}}),
            {"Content-Type": "application/json"},
        )
        assert status == 200, body
        content = json.loads(body["choices"][0]["message"]["content"])
        assert content["prompt_flag"] is True and content["prompt_has_schema"] is True
    output = process.stdout.read() if process.stdout else ""
    assert "structured_output=native_rejected provider=claude fallback=prompt" in output


def verify_ports_free() -> None:
    for port in (PLAYER2_PORT, 4316, BRIDGE_PORT, OVERRIDE_PORT):
        assert_refused(port)


def main() -> int:
    verify_ports_free()
    criteria = [
        criterion_1, criterion_2, criterion_3, criterion_4, criterion_5, criterion_6,
        criterion_7, criterion_8, criterion_9, criterion_10, criterion_11, criterion_12,
        criterion_13, criterion_14, criterion_15, criterion_16, criterion_17,
        criterion_18, criterion_19, criterion_20, criterion_21, criterion_22,
        criterion_23, criterion_24, criterion_25, criterion_26, criterion_27,
        criterion_28, criterion_29,
        criterion_30, criterion_31, criterion_32, criterion_33, criterion_34,
        criterion_35, criterion_36, criterion_37, criterion_38, criterion_39,
        criterion_40, criterion_41, criterion_42, criterion_43, criterion_44, criterion_45,
        criterion_46, criterion_47, criterion_48, criterion_49, criterion_50,
        criterion_51,
    ]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="p2-passthrough-test-") as temp:
        folder = Path(temp)
        for number, test in enumerate(criteria, 1):
            try:
                test(folder)
            except Exception as exc:
                failures += 1
                print(f"{number:>2}. FAIL {test.__name__}: {type(exc).__name__}: {exc}", flush=True)
            else:
                print(f"{number:>2}. PASS {test.__name__}", flush=True)
    print(f"\nAcceptance: {len(criteria) - failures}/{len(criteria)} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
