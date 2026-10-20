# Security Policy

## Supported version

Security fixes are developed for the latest published Game AI CLI Bridge release. Older releases should be replaced rather than exposed or reused.

## Reporting a vulnerability

Do not post credentials, private prompts, game data, or provider output in a public report. Use GitHub's private vulnerability reporting on the repository (or the contact listed there) and provide:

- bridge version and file hashes
- Windows and Python versions
- affected endpoint or launcher
- minimal reproduction steps using redacted or synthetic content
- expected and observed behavior

Never include API keys, CLI login files, or DPAPI blobs.

## Threat model

The bridge is intended for one user on one Windows PC. Its IPv4 loopback bind at `127.0.0.1:<configured port>` protects against LAN and Internet access, and bridge-owned routes reject common external browser-origin requests. Player2 passthrough routes keep the same trust boundary as direct local access to Player2 and do not add an Origin check, body-size limit, or normalized allow-prefix comparison. The bridge does not authenticate another local process and does not claim to protect against malware, an administrator, code injected into the game or provider CLI, or another process already running as the same Windows user. Codex chat runs read-only but its built-in tools can read files the Windows user can read, so game text that contains a prompt injection could cause local file contents to be sent to the provider. Grok image sessions are not covered by the deny-tools hook: they run with `--always-approve` and `--sandbox workspace` (capped at four turns), so every tool call the agent makes is approved automatically, and a prompt injection in an image prompt could likewise make the agent use its tools, including reading local files, within whatever that sandbox allows.

## Implemented controls

- IPv4 loopback-only bind and non-loopback configuration rejection
- external `Origin` rejection and JSON content-type enforcement
- request body, prompt, image dimension, output size, timeout, and concurrency limits
- subprocess argument lists without `shell=True`
- provider-specific credential allowlists
- Grok chat sessions run with a user-global `PreToolUse` hook that denies every tool call when the bridge's marker variable is present (Grok Build's ACP stdio mode ignores `--tools`, `--deny`, and `--permission-mode`); the marker is set only for the bridge's own chat child processes, and the game's system prompt is preceded by a tools-disabled rule
- one absolute request deadline across provider launch, stdin, bounded stdout/stderr transport, process-tree termination, and worker cleanup; the measured outbound prompt snapshot is deep-frozen before launch and the same snapshot is sent; ACP stdout has a finite outbound prompt-content echo allowance while retained provider output remains independently capped
- embedding credential removed from the Python bridge process environment after loading; the launcher process removes its temporary copy when the bridge exits
- prompt, reference image, credential, raw provider output, and absolute job-path log suppression (the opt-in diagnostics described in PRIVACY.md are the exception)
- Grok image-tool failure diagnostics reduced to fixed allowlisted categories and content-free argument shape; raw tool messages, reference tokens, and HTTP bodies remain suppressed
- private bridge-owned per-request image directories and success/failure cleanup; provider-owned session stores are not scanned or deleted
- up to eight reference images per edit, with base64/body-size limits and supported PNG/JPEG/WebP transport-signature classification; reference-image decodability remains the selected provider's responsibility
- normal Grok agent receives the original image prompt and bounded ACP image blocks without a custom profile or bridge-constructed native-tool arguments; execution is capped at four turns with planning, subagents, memory, and web search disabled; tool calls are auto-approved (`--always-approve`) inside Grok's `workspace` sandbox and the deny-tools hook is not applied, see the threat model above
- Grok generated-output acceptance only from one completed typed `ImageGen`/`ImageEdit` `rawOutput` correlated to both an established non-empty current session ID and the exact random per-request `session/prompt._meta.promptId` during the active prompt/drain window; pre-session, pre-prompt, post-window, missing or mismatched session ID, and missing or mismatched prompt ID updates are ignored; ACP content blocks remain the original prompt plus images, the prompt ID is not logged, and there is no provider-session directory scanning or deletion
- generated-output bounded non-reparse regular-file reads plus PNG/JPEG signatures and dimensions; complete PNG chunk/zlib/palette validation and bounded JPEG SOF/SOS/EOI marker validation
- at most one retryable image fallback under a shared deadline
- Codex runs with `--ephemeral` inside a private per-request directory and a read-only sandbox for both chat and image requests (the directory holds only an optional `schema.json` for chat, or the reference images for image edits). The image tool saves under `%CODEX_HOME%\generated_images\` (by default `%USERPROFILE%\.codex\generated_images\`); the tool saves each run's image in `generated_images\<thread_id>\` (the id Codex prints when the run starts), and the bridge reads only a `.png` path from Codex's final message that resolves directly into that run's folder, or, when no path is reported, a valid `output.png` in the request's own freshly created private directory. Codex's built-in tools can still read files the Windows user can read.
- `--openai-base-url` accepts only `https://` URLs with a hostname, or `http://` URLs whose host is `localhost` or a loopback IP address (`127.0.0.0/8`, `::1`), because the stored embedding key is sent to it as a bearer token
- Fish Audio TTS calls go to `https://api.fish.audio` by default; `--fish-base-url` (meant for tests) accepts any `https://` host or loopback `http://`, and whoever controls that URL receives the key and the TTS text, exactly like `--openai-base-url`; the Fish key is read from the launcher environment once and removed, and is never passed to provider CLIs

## Operational guidance

- Run the bridge and game as a normal user, not as Administrator.
- Do not place the extracted bridge folder in a shared or web-served directory.
- Stop the bridge when it is not needed; any same-user local process can reach it while it is running.
- Remove optional embedding keys with `REMOVE-OPENAI-KEY.cmd` when they are no longer needed.
- Authenticate provider CLIs using their supported login mechanisms and keep them updated.
- Review diagnostic logs and generated test images before sharing.
