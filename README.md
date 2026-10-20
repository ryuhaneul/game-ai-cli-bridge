# Game AI CLI Bridge 0.4.17

> This is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by Elefant AI Inc. or Player2. "Player2" is used only to describe API compatibility.

A Windows bridge that accepts requests from any game or mod that talks to the Player2 API on `127.0.0.1` and serves them with the AI coding CLIs installed on the PC (Claude Code, Codex, Grok Build) under their own subscriptions. Point the game's Player2 URL at the bridge: chat and image requests go to the CLI, and anything the bridge does not implement (TTS/STT and other endpoints) can be forwarded to the real Player2 app. When `tts.provider` is `fish` and a Fish Audio key is configured, TTS is served by the Fish Audio API instead (see [TTS (Fish Audio)](#tts-fish-audio)).

**Use your own account on your own PC.** The bridge is meant for one person running a game on their own Windows PC with provider CLIs they installed and signed in to themselves. It never handles provider sign-in, never reads or stores CLI credentials, and must not be used to share one account or subscription with other people or to serve requests for anyone else. Whether a given use is allowed is decided by each vendor's terms (Anthropic, OpenAI, xAI, Fish Audio, Player2), and complying with them is the user's responsibility. Subscription plans often assume ordinary interactive, individual use; if you are unsure whether automated requests from a game fit your plan, use an API key instead (see [Credentials](#credentials)).

There are two ways to run it.

| Mode | Setting | Behavior |
|---|---|---|
| Replace | `port` equal to `player2.port` (both default to 4315) | The bridge takes the Player2 port instead of the Player2 app. Chat, image and embeddings only; the TTS voice list answers 200 with an empty list and TTS/STT actions answer HTTP 501 unless Fish Audio TTS is configured |
| Side by side | `port` different from `player2.port` (e.g. bridge 4316, Player2 app on 4315) | Bridge-owned routes go to the CLI; other routes are passed through to the Player2 app (including STT) when `player2.passthrough` allows them. TTS is passed through too unless Fish Audio TTS is active, in which case the bridge serves it and passes through only when Fish fails and `tts.fallback` is on |

## Providers

| Feature | Provider |
|---|---|
| Chat | Claude Code CLI, Codex CLI, Grok Build CLI |
| Image generate / edit | Codex CLI agent, Grok Build CLI agent |
| Embeddings | Optional OpenAI Platform API |
| TTS | Fish Audio API (optional, with API key); otherwise passed through to the Player2 app in side-by-side mode |
| STT / everything else | Passed through to the Player2 app (side-by-side mode only) |

The launcher you start (`start-claude.cmd`, `start-codex.cmd`, `start-grok.cmd`) selects the chat provider; `image.provider` in `bridge-config.json` selects the image provider. When the primary image provider fails with a retryable error the fallback provider is called at most once. Invalid input and ordinary HTTP 4xx errors are not retried. Primary and fallback share one 300-second deadline by default (`--timeout` changes it); a primary that uses it all leaves no time for the fallback.

## Configuration (`bridge-config.json`)

```json
{
  "port": 4315,
  "player2": {"port": 4315, "passthrough": "all", "allow": ["/v1/tts/", "/v1/stt/"]},
  "claude": {"model": "opus", "effort": "low"},
  "codex": {"model": "gpt-6.1-sol", "effort": "low"},
  "grok": {"model": "grok-4.7", "effort": "low"},
  "image": {
    "provider": "codex",
    "fallback_provider": "grok",
    "width": 1100,
    "height": 640
  }
}
```

| Key | Meaning |
|---|---|
| `port` | Listen port. Default `4315`. An explicit `--port` on the command line wins |
| `player2.port` | Port of the Player2 app that passthrough forwards to. Default `4315` |
| `player2.passthrough` | Forwarding to the Player2 app: `all` (every non-bridge route), `none`, `list` (only `allow` prefixes). Default `all` |
| `player2.allow` | Raw path prefixes forwarded in `list` mode. Default `/v1/tts/`, `/v1/stt/` |
| `claude` / `codex` / `grok` | `model` and `effort` handed to each CLI. They must be values the installed CLI and account actually support; the bridge never substitutes or downgrades them |
| `image.provider` / `image.fallback_provider` | Image CLI and fallback (`codex` or `grok`; fallback may be `null`) |
| `image.width` / `image.height` | Size used when a request carries no size (128–4096). A request's own `width`/`height` or `aspect_ratio` overrides it |

`EDIT-MODEL-EFFORT.cmd` opens the file in Notepad. TTS settings (`tts.*`) are described in [TTS (Fish Audio)](#tts-fish-audio).

## Supported Player2 endpoints

| Endpoint | Status |
|---|---|
| `GET /v1/health` | Served by the bridge. `tts` is true when Fish Audio TTS is active or passthrough allows the TTS routes; `stt` reflects passthrough only |
| `POST /v1/chat/completions` | Selected chat CLI. `response_format` (`json_schema` / `json_object`) is supported. Claude (`--json-schema`) and Codex (`--output-schema`) enforce a `json_schema` natively; a `const`/`enum` without `type` first gets the type of its value(s), which Codex's strict mode requires. If the CLI still rejects the schema (Claude: unknown keywords; Codex: anything outside OpenAI strict schemas, such as objects without `additionalProperties: false`), the bridge logs `structured_output=native_rejected` and retries once with the schema in the prompt. That retry, Grok (schema in the system prompt) and Codex `json_object` (prompt instruction) are best effort. The JSON text is returned as `message.content`; if the model returns something that is not valid JSON the bridge still answers 200 with that text and logs `structured_output=invalid`, so the client should validate |
| `POST /v1/image/generate`, `POST /v1/image/edit` | Native image tool of the image CLI |
| `POST /v1/embeddings` | OpenAI Platform API (optional, key required) |
| `GET /v1/tts/voices`, `POST /v1/tts/speak`, `POST /v1/tts/stream` | Fish Audio when `tts.provider` is `fish` and a key is configured. Otherwise: side by side: passed through to the Player2 app; replace mode: empty list / HTTP 501 |
| STT endpoints | Side by side: passed through to the Player2 app. Replace mode: HTTP 501 |
| Any other path | Side by side with passthrough allowed: forwarded (WebSocket included). Otherwise: 404. `OPTIONS` is always answered locally with 204 and never forwarded |

Bridge-owned routes are: `GET /`, `/health`, `/v1/health`, `/api/version`, `/api/tags`, `/v1/models`; `POST /api/show`, `/v1/embeddings`, `/embeddings`, `/v1/image/generate`, `/image/generate`, `/v1/image/edit`, `/image/edit`, `/api/chat`, `/api/generate`, `/v1/chat/completions`, `/chat/completions`. Every other path is forwarded only when `port` differs from `player2.port` **and** `player2.passthrough` allows it (`all`, or `list` with a matching prefix). When Fish Audio TTS is active, `GET /v1/tts/voices`, `POST /v1/tts/speak` and `POST /v1/tts/stream` (and their `/tts/...` aliases) are bridge-owned as well.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer runnable as `py -3` or `python`
- The chat provider CLI you want, installed and signed in
- For images: Codex CLI or Grok Build CLI with its image generation tool available
- A game or mod whose Player2 URL (host and port) can be changed
- Side-by-side mode: the Player2 desktop app running on `player2.port` (default 4315)

CLI subscriptions, CLI sign-in and provider API billing follow each vendor's terms. Each CLI can be signed in with a subscription account or with an API key; the bridge works the same either way. An OpenAI Platform API key is needed only for the optional embeddings endpoint.

## Install and sign in to provider CLIs

Install only the providers you use. With the default configuration images need Codex; Grok is only called as the fallback when Codex fails with a retryable error, so it is optional. Open a new PowerShell window after installing so `PATH` is refreshed. The commands below are the Windows methods from each vendor's official documentation; check the linked pages before running a remote install script.

### Claude Code CLI

```powershell
irm https://claude.ai/install.ps1 | iex
```

Alternatives: `winget install Anthropic.ClaudeCode`, or `npm install -g @anthropic-ai/claude-code` with Node.js 18+. Pick one method only. Verify and sign in with `claude --version` and `claude`. Docs: <https://code.claude.com/docs/en/setup>

### Codex CLI

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

Alternative: `npm install -g @openai/codex`. Verify with `codex --version`, run `codex` and choose **Sign in with ChatGPT**. Docs: <https://developers.openai.com/codex/cli/>

### Grok Build CLI

```powershell
irm https://x.ai/cli/install.ps1 | iex
```

Alternative: `npm install -g @xai-official/grok`. Verify and sign in with `grok version`, `grok login`, `grok models`. Without a browser see `grok login --device-auth` in the official docs. Images require the native `image_gen`/`image_edit` tools to be available to the signed-in account and installed CLI. Docs: <https://docs.x.ai/build/overview>, <https://docs.x.ai/build/cli/reference>

## First-time setup

1. Extract the ZIP into an ordinary writable folder. Do not run from inside the ZIP viewer.
2. Install and sign in to the provider CLIs you use.
3. Review `bridge-config.json`. For side-by-side mode set `port` to something other than `player2.port` (4315 by default).
4. Set the game's or mod's Player2 URL to `http://127.0.0.1:<port>`.
5. Run `start-claude.cmd`, `start-codex.cmd` or `start-grok.cmd` for the chat provider you want. The `Player2  :` line of the startup banner shows the passthrough state.
6. Run `check-bridge.cmd` for the health endpoint and `TEST-IMAGE.cmd` for image generation.

Every launch rewrites `LAST.log` in the bridge folder with safe diagnostics only. Chat and image prompts are shown on the launcher console but never written to `LAST.log`.

## TTS (Fish Audio)

Player2's voice list has no Korean voice: as of September 2026 the 54 voices returned by `https://api.player2.game/v1/tts/voices` are American and British English, Mandarin Chinese, Japanese, Hindi, Spanish, Brazilian Portuguese, Italian and French, and the `voice_language` option for choosing a default voice has no Korean entry either. Whether those voices can read Korean text, or whether the custom Inworld voices Player2 accounts can add can, was not tested. The bridge can serve `/v1/tts/speak`, `/v1/tts/stream` and `/v1/tts/voices` itself through the Fish Audio API instead.

1. Create a Fish Audio account and API key (https://fish.audio). TTS is billed per UTF-8 byte of text ($15 per million bytes at the time of writing; the `s2.1-pro-free` model is $0). The text of every TTS request is sent to Fish Audio.
2. Run `SET-FISH-KEY.cmd`; the key is stored with Windows DPAPI in `%LOCALAPPDATA%\Player2CLIBridge\fish-audio-key.dpapi` and injected into the bridge as `PLAYER2_FISH_AUDIO_KEY` by the launcher (never written to `bridge-config.json` or passed to provider CLIs). `REMOVE-FISH-KEY.cmd` deletes it.
3. `tts.provider` is `fish` by default, so nothing else is needed; set it to `none` to turn Fish Audio off even when a key is stored.

| Key | Meaning |
|---|---|
| `tts.provider` | `fish` (default) or `none` |
| `tts.model` | Fish model sent in the `model` header. Default `s2.1-pro-free` |
| `tts.reference_id` | Fish voice (model id) used when the game does not pick one. `null` = Fish default voice |
| `tts.language` | Language filter for the voice list, e.g. `ko`. Default `ko` |
| `tts.fallback` | `true` (default): when Fish fails, fall back to what the bridge would have done without Fish (passthrough in side-by-side mode, otherwise 501/empty list). `false`: answer 502 `tts_failed` |
| `tts.sample_rate` | Sample rate for `wav` and `pcm` output: 8000, 16000, 24000, 32000 or 44100. Default 24000, which is what Player2 clients assume for `pcm` (the AI Influence mod encodes the raw samples as 24 kHz mono). Ignored for mp3 |

Behavior with the key configured:

- `GET /v1/tts/voices` returns Fish's public voice list for `tts.language` (up to 50, sorted by score, cached for 60 seconds) with the configured `reference_id` inserted at the front when it is not already in the list (if it is, it keeps its position). These are community voices: entries can change, duplicate, or disappear, and their licensing is the uploader's responsibility. Every id the bridge has shown, plus the configured one, stays accepted for the lifetime of the process.
- `POST /v1/tts/speak` uses `voice_ids[0]` when it is one of those known ids, otherwise `tts.reference_id`; `speed` maps to Fish `prosody.speed`; `audio_format` `mp3`, `wav` and `pcm` are passed through (`pcm` is raw 16-bit mono samples at `tts.sample_rate`, no header, `mimetype` `audio/pcm`; `wav` uses the same rate); `ogg` and any value outside those are served as `mp3` (the response `format` says so, and the log records the requested value as a short token, e.g. `tts_format=ogg served=mp3`). The response carries the base64 audio as `data` (the field the current Player2 spec defines) and also as `audio`, plus `format`, `mimetype` and `duration`. `duration` is computed from the audio data for wav and pcm (rounded to milliseconds) and estimated from the byte count for mp3.
- `POST /v1/tts/stream` streams Fish's audio bytes as they arrive. Fallback applies only to failures before the first byte reaches the client (connection, 401, 402, 429, 5xx, first-chunk timeout); a failure after that closes the connection.
- Without the key the TTS routes behave exactly as before.

## Player2 passthrough

When `port` differs from `player2.port`, requests for paths the bridge does not own are forwarded to the Player2 app at `127.0.0.1:<player2.port>` (4315 by default). The method, path, query and body are forwarded as received. The `Host` header is rewritten to the Player2 app address, hop-by-hop headers (`Connection`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade` and the proxy/TE/trailer headers) are dropped in both directions, and a request that uses `Transfer-Encoding` is rejected with HTTP 411 because only `Content-Length` bodies are forwarded. The response status, remaining headers and body are relayed and redirects are not followed. `OPTIONS` requests are always answered locally with 204. WebSocket upgrade requests become a bidirectional byte tunnel. If the Player2 app is not running the bridge answers HTTP 502 (`upstream_unavailable`).

`player2.passthrough` has three modes: `all` forwards every non-bridge route, `none` disables forwarding, `list` forwards only when the raw path starts with an entry of `player2.allow`. When `port` equals `player2.port` passthrough is always disabled so the bridge never forwards to itself.

Passthrough routes deliberately carry none of the bridge's extra security checks: no Origin check, no body-size limit, and the `allow` prefixes are compared against the raw path without normalizing `..` or percent-encoded segments. An idle WebSocket tunnel holds one of the bridge's 32 request slots for its lifetime. The bridge is loopback-only and passthrough keeps exactly the trust boundary of talking to the Player2 app directly; this is intended.

## How Codex is driven

Codex runs `codex exec --ephemeral` (no session files are persisted) inside a private per-request directory that is deleted afterwards. Chat uses a read-only sandbox; the directory is empty except for `schema.json` when the request carries a `response_format` schema. Image requests also use a read-only sandbox: the reference images are placed in that directory, Codex calls its built-in image tool (which saves the PNG under `%CODEX_HOME%\generated_images\` (by default `%USERPROFILE%\.codex\generated_images\`)), and reports the saved path in its final message. The tool saves each run's image in a folder named after the run (`generated_images\<thread_id>\`, the id Codex prints when the run starts), and the bridge accepts only a `.png` directly in that folder, reported in Codex's final message, so a path steered by prompt injection to another image (an older one or one from another Codex run) or to any other file is refused. One exception: when Codex reports no path but a valid `output.png` exists in the request's own private directory (created fresh for each request), that file is accepted. Nothing is copied by a sandboxed shell command any more (on Windows such a copy produced a file the bridge could not read). In both modes Codex's built-in tools can still read files your Windows user can read, so a prompt-injection attempt in game text could make the model read and quote local files; this is the same trust model as running Codex yourself. Claude chat runs with tools disabled.

## How Grok Build is driven

Grok chat and image requests use Grok Build's official ACP JSON-RPC stdio interface (`grok agent stdio`). The game's system prompt is sent as `session/new._meta.systemPromptOverride` and the conversation as `session/prompt`.

Grok Build's ACP stdio mode ignores `--tools`, `--disallowed-tools`, `--deny`, `--permission-mode` and `--max-turns` (headless-only flags), so chat sessions are hardened twice:

- a "tools are disabled" rule precedes the system prompt;
- the bridge installs a user-global Grok hook file `%USERPROFILE%\.grok\hooks\player2-cli-bridge-deny-tools.json` (`$GROK_HOME\hooks\` when `GROK_HOME` is set). Its `PreToolUse` command denies every tool call only when the environment variable `PLAYER2_CLI_BRIDGE_DENY_TOOLS` is present; the bridge sets it only for its own chat child processes, so interactive Grok sessions are unaffected. Delete the file to uninstall. Denied calls appear in `LAST.log` as `[grok/chat] event session_update=tool_call_update status=failed`.

Image sessions do not set the variable; instead a short backend instruction makes the agent call `image_edit` (references attached) or `image_gen` (none) exactly once on the first turn. Without it Grok Build behaves like a coding agent, reads skill files and attachments, and exhausts its turn limit. Reference images travel as ACP `image` content blocks in the same `session/prompt` and are never written to the command line or temporary files. The bridge attaches at most the first 3 reference images to Grok (logged as `refs=N sent=3`): in testing with Grok Build 1.0.13 (September 2026) its image tool rejected a fourth with HTTP 400. Grok Build keeps its own session history and may persist request content; the bridge never scans or deletes that store.

## Image behavior

Output size: a request may carry `width` and `height` (128–4096) or `aspect_ratio` (one of 21:9, 16:9, 3:2, 4:3, 5:4, 1:1, 4:5, 3:4, 2:3, 9:16, 9:21; it takes precedence) as in the Player2 API; when it carries none, `image.width`/`image.height` from the configuration are used (the AI Influence mod sends no size with its edit requests). Codex receives the size and the reference images in the prompt, is told to ask its image tool for that size or the closest size the tool supports, must not run any command or post-process the image, and reports the path of the PNG the tool saved; the bridge validates and returns that file. Grok gets the nearest of its aspect ratios, never `auto`. Grok's native image tool produces JPEG, so the bridge converts it to PNG (the Player2 `/v1/image/generate` contract is base64 PNG) with Pillow when installed, otherwise with Windows PowerShell `System.Drawing`.

- Up to eight reference images per request, with base64/body-size limits; only PNG/JPEG/WebP are accepted. Codex receives all of them, Grok only the first 3 (see above). Decodability is the provider's responsibility.
- Results are read as bounded regular files and validated as PNG/JPEG (signature, chunks, CRC, markers, dimensions ≤ 4096) before being returned.
- Grok results are accepted only from one completed typed `ImageGen`/`ImageEdit` `rawOutput` matching both the current session ID and the random per-request `promptId`; only that absolute path is read.
- Provider launch, stdin, stdout/stderr, process-tree termination and cleanup share one absolute deadline.
- Unless the diagnostic flag `--keep-image-jobs` is used, bridge-created request files and references are removed on success and failure. The PNG that Codex's image tool saved under `%CODEX_HOME%\generated_images\` (by default `%USERPROFILE%\.codex\generated_images\`) is Codex's own file and stays there.

`TEST-IMAGE.cmd` sends a real image request and may consume credits or quota. The image is saved in the bridge folder and the log is `LAST-IMAGE-TEST.log`.

## Credentials

- Claude child: only `ANTHROPIC_API_KEY` may be passed
- Codex child: only `OPENAI_API_KEY`
- Grok child: only `XAI_API_KEY`

To run a provider CLI on API billing instead of a subscription, sign that CLI in with an API key using the vendor's documented method, or set the variable above in the environment the launcher is started from; the bridge passes only that one variable through to the matching CLI.
- Optional embeddings: internal `PLAYER2_OPENAI_EMBEDDING_KEY`
- Optional Fish Audio TTS: internal `PLAYER2_FISH_AUDIO_KEY` (not passed to any provider CLI)

`SET-OPENAI-KEY.cmd` and `SET-FISH-KEY.cmd` store the optional embedding key and Fish Audio key with Windows DPAPI for the current user under `%LOCALAPPDATA%\Player2CLIBridge\`. They are unrelated to Codex CLI sign-in. `REMOVE-OPENAI-KEY.cmd` / `REMOVE-FISH-KEY.cmd` delete them. Never put credentials into `bridge-config.json`, command lines, screenshots or bug reports.

## Security boundary

The bridge binds only to IPv4 loopback `127.0.0.1:<port>` and rejects other bind addresses. Bridge-owned routes reject external browser origins and limit POST JSON content type, body size, prompt size and concurrent provider jobs. Passthrough routes keep the trust boundary of direct access to the Player2 app, as described above. There is no authentication; another local process running as the same Windows user is not blocked. See `SECURITY.md` for the threat model.

## Logging and privacy

No telemetry. Requests are handled locally and only the content a request needs is sent to the selected CLI, the Player2 app (passthrough), the Fish Audio API (TTS text, when configured) or the optional embeddings API. Logs never contain prompts, reference images, credentials, raw provider output or absolute private job paths. See `PRIVACY.md`.

Diagnostics that are off by default: `--keep-image-jobs` keeps each image request directory (references, and a copy of the returned image as `output.png`; the log prints `cleanup=kept path=...`), and `--dump-cli-events DIR` writes the JSON lines that each Claude Code, Codex and Grok Build run prints on its standard output to `DIR` (one file per run, path printed as `cli_event_dump=...`): the model's messages, the tool calls and commands it made with their inputs and results, and for Grok the ACP messages it sends. What the bridge sends to the CLI (the prompt and reference images) is not written, but the model's messages and tool inputs can quote request content. Claude Code/Codex lines longer than 64 KiB are replaced by a `dump_omitted` placeholder. On POSIX the folder is created owner-only (0700) and each file 0600; on Windows they inherit the ACL of the chosen folder, so pick one that only you can read and delete it when done. When an image output fails validation the log records its byte count and a file-type label (`output_invalid bytes=... kind=png|jpeg|webp|gif|other|empty|unreadable`), never its bytes. If a result file exists but cannot be opened (on Windows a freshly written file can be held by an antivirus scan for a few seconds) the bridge keeps retrying for up to 15 seconds, within the request deadline, and if it still fails logs `output_unreadable error=... errno=... winerror=...`. The launcher accepts `-KeepImageJobs` and `-DumpCliEvents <dir>` and passes them through.

## Verification status

- **Tested in use**: the AI Influence mod for Mount & Blade II: Bannerlord on Windows — Codex chat, Grok Build chat, Codex image edit with reference images (0.4.14 path handoff), Grok Build image generate and edit (also as the fallback), Fish Audio TTS in Korean (`pcm` at 24 kHz), OpenAI embeddings, and side-by-side passthrough (TTS/STT) with the Player2 app. Claude Code chat has not been tested in-game. No other game or mod has been tested in use yet.
- **Source review only**: Voices of the Court (VOTC) for Crusader Kings III was reviewed against its source. Its Player2 provider is fixed to `127.0.0.1:4315`, so it needs replace mode, and it uses `response_format` (json_schema) for action decisions. It has not been tested in use. Emmersive (Elin with AI) for Elin was also reviewed against its source only: its Player2 provider is likewise fixed to `127.0.0.1:4315`, it uses a strict `json_schema` `response_format`, and its default request timeout is 15 seconds, which may be too short for slower providers.
- **Automated tests** (`tests/test_passthrough.py`, stdlib only), 51 cases: configuration and port precedence, the three passthrough modes, no redirect following, close-delimited responses, 502 when the app is down, WebSocket tunnel and slot release, Origin boundary, hook install, `player2.port`, the `response_format` paths (fake CLIs, including type filling for `const`/`enum` and the prompt retry after a CLI rejects a schema), Codex `--ephemeral` and private working directory, POSIX directory modes, `--openai-base-url` validation, chat route. Fish Audio TTS is covered with a fake Fish server (speak, stream, voices, fallback rules, key isolation, `pcm`/`wav`/`mp3` mapping and `tts.sample_rate`). Image requests are covered for size resolution (request `width`/`height` or `aspect_ratio`, else the configured default), the Grok aspect-ratio mapping, the Codex reported-path handoff with a fake Codex (accepted only in this run's `generated_images\<thread_id>\` folder and from the final message; rejected for another run's image, a path outside the folder, a non-`.png` file, a path only in an earlier message, a missing run id, or no report), a rejected non-PNG output never appearing in the log, retrying an output file that cannot be opened yet without passing the request deadline, the `--dump-cli-events` diagnostics for Codex and Grok (with owner-only permissions on POSIX), `ogg` logging, rounded WAV duration, refusing more than 8 reference images on the Grok path, and the upstream HTTP status of a Grok ACP error.
- Claude Code, Codex and Grok Build chat and `response_format` routes were verified against the real CLIs on Linux. On Windows the current `start-codex.cmd` launcher, key loading from DPAPI and the features listed above were confirmed in-game; `start-claude.cmd` and `start-grok.cmd` of the current version, `REMOVE-OPENAI-KEY.cmd`, `REMOVE-FISH-KEY.cmd`, `MANAGE-KEY.ps1 -Action Status`, `CHECK-BRIDGE`, `TEST-IMAGE` and structured output have not been re-checked on Windows yet.

### Test log

Dates and CLI versions as recorded during development. Versions that were not recorded are marked as such.

| Date | Bridge | What was verified | CLI version |
|---|---|---|---|
| 2026-10-03 | 0.4.17 | Voices of the Court's exact `response_format` requests through the bridge on real Codex (`gpt-6.1-sol`) and Claude Code (`opus`): all four answered 200 with valid JSON | Not recorded |
| 2026-10-03 | 0.4.16 | Real Codex chat through the bridge with `gpt-6.1-sol` | Not recorded |
| 2026-09-29 | 0.4.15 | Real Codex chat and image generate through the bridge (`gpt-6-sol`); `grok-4.7` confirmed as the Grok Build default with `grok models`, end-to-end Grok run not possible (usage exhausted, HTTP 402) | Grok Build 1.0.41; Codex not recorded |
| 2026-09-07 | 0.4.14 | Real Codex image generate through the bridge on Windows with the read-only sandbox and reported-path handoff (200 in 44 s) | Codex CLI 0.149.0 |
| 2026-09-03 | 0.1.0 | Grok Build chat (Korean roleplay system prompt) and image generate/edit on Windows | Grok Build 1.0.13 |

The in-game items listed above under "Tested in use" were confirmed between 2026-09-03 and 2026-09-29; the exact date of each in-game check was not recorded. No Claude Code CLI version was recorded for any test.

## Troubleshooting

- **Cannot connect:** make sure a `start-*.cmd` is running, rerun `check-bridge.cmd`, and check that `port` in `bridge-config.json` matches the game's Player2 URL.
- **CLI not found:** install it and confirm it is on `PATH` in a new window.
- **Provider sign-in errors:** sign in with the provider's own CLI.
- **TTS actions return 501 (voice list empty):** replace mode (`port` equal to `player2.port`) does not provide them unless Fish Audio TTS is active (`tts.provider` = `fish` plus a key).
- **STT actions return 501:** replace mode does not provide STT at all. Switch to side-by-side mode and run the Player2 app.
- **502 `upstream_unavailable`:** the Player2 app is not running on `player2.port`.
- **Image timeout:** primary and fallback share one timeout; raising it beyond the game's request lifetime will not help.
- **Embeddings unavailable:** set an OpenAI Platform key with `SET-OPENAI-KEY.cmd` if you need them.

- **Codex image requests fail with `image_output_invalid` or take minutes**: start with `.\START-BRIDGE.ps1 -Provider codex -KeepImageJobs -DumpCliEvents .\p2dump`, make the game request an image once, then look at the `.jsonl` in `p2dump` (whether Codex called its image tool, which path it reported, and whether it ran commands it was told not to run) and at the `generated_png=` lines in the log. Codex's own copy of every generated image stays under `%CODEX_HOME%\generated_images\` (by default `%USERPROFILE%\.codex\generated_images\`); the bridge never deletes it.
- **Grok requests fail with `cli_failed`**: look for `acp_error ... upstream_http_status=N hint=...` in the log. `402` with `hint=usage_or_credits_exhausted` means the Grok account's Grok Build usage is used up; `401` means Grok Build needs to be signed in again.

## License

MIT. See `LICENSE.txt` and `NOTICE.md`.
