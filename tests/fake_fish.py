#!/usr/bin/env python3
"""Loopback-only fake Fish Audio server for acceptance tests."""

import json
import os
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def wav_bytes() -> bytes:
    samples = b"\0" * 32000
    return (
        b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data" + struct.pack("<I", len(samples)) + samples
    )


MP3_BYTES = b"ID3" + b"\0" * 15997
WAV_BYTES = wav_bytes()


def wav_stream_bytes() -> bytes:
    # Streamed WAV as Fish Audio sends it: RIFF/data sizes are placeholders, not the real length.
    samples = b"\0" * 32000
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data" + struct.pack("<I", 0xFFFFFF00) + samples
    )


WAV_STREAM_BYTES = wav_stream_bytes()
PCM_BYTES = b"\0" * 32000  # 1.0 s of 16-bit mono at 16 kHz


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        del fmt, args

    def do_GET(self):
        if urlsplit(self.path).path != "/model":
            self.send_error(404)
            return
        count_path = Path(os.environ["FAKE_FISH_COUNT"])
        if count_path.with_suffix(".bad").exists():
            self.connection.sendall(b"HTTP/1.1 ABC\r\n\r\n")
            self.close_connection = True
            return
        count = int(count_path.read_text(encoding="ascii")) + 1 if count_path.exists() else 1
        count_path.write_text(str(count), encoding="ascii")
        items = [{"_id": "a" * 32, "title": "Voice A", "languages": ["ko"]}]
        if count == 1:
            items.append({"_id": "b" * 32, "title": "Voice B", "languages": ["ko", "en"]})
        payload = json.dumps({"items": items}, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/v1/tts":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        Path(os.environ["FAKE_FISH_DUMP"]).write_text(json.dumps({
            "body": body,
            "model": self.headers.get("model"),
            "authorization": self.headers.get("Authorization"),
        }), encoding="utf-8")
        text = body.get("text")
        statuses = {"FAIL401": 401, "FAIL402": 402, "FAIL503": 503}
        if text in statuses:
            self.send_response(statuses[text])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if text == "HANG":
            time.sleep(35)
            return
        if text == "BADHTTP":
            self.connection.sendall(b"HTTP/1.1 ABC\r\n\r\n")
            self.close_connection = True
            return
        if text in {"BIGLEN", "MAXLEN"}:
            declared_length = 33554433 if text == "BIGLEN" else 33554432
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(declared_length))
            self.end_headers()
            self.wfile.write((MP3_BYTES * 5)[:65536])
            self.wfile.flush()
            self.close_connection = True
            return
        if text == "CUTMID":
            payload = MP3_BYTES * 7
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload[:65536])
            self.wfile.flush()
            self.close_connection = True
            return
        if text == "wavstream":
            payload = WAV_STREAM_BYTES
        elif body.get("format") == "pcm":
            payload = PCM_BYTES
        else:
            payload = WAV_BYTES if body.get("format") == "wav" else MP3_BYTES
        self.send_response(200)
        content_types = {"wav": "audio/wav", "pcm": "audio/pcm"}
        self.send_header("Content-Type", content_types.get(body.get("format"), "audio/mpeg"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(server.server_port, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
