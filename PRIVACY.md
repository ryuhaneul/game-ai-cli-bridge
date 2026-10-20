# Privacy

## Summary

Game AI CLI Bridge contains no telemetry, analytics, advertising, account system, or automatic update check.

## Data processed locally

The bridge receives Player2 requests on `127.0.0.1`. Depending on the requested feature, those requests may include dialogue text, system instructions, image prompts, reference images, or embedding input.

The bridge creates per-request job directories for image requests and Codex chat requests (POSIX mode 0700; on Windows they inherit the ACL of the jobs directory's parent). Reference images written for Codex are created with mode 0600 on POSIX. For a Codex chat request that carries a `response_format` JSON schema, the schema (not the prompt) is written to a per-request directory as `schema.json` and removed when the request ends. On POSIX the directory is created with owner-only permissions (0700) and the file with 0600. On Windows the directory inherits the ACL of its parent: by default the `jobs` folder under the bridge folder; if `--jobs-dir` points elsewhere, check that location's ACL, because other local users may otherwise read the schema while a request is running. Grok reference images are sent as ACP stdin content blocks and are not copied into those directories; for Codex the bridge writes the reference images into the private job directory. The job directories and their files are removed on success and failure unless the diagnostic `--keep-image-jobs` flag is used (see Diagnostics below).

Codex chat and image requests run `codex exec --ephemeral`, so Codex does not persist session files for them. Codex's built-in image tool keeps every generated image under `%CODEX_HOME%\generated_images\` (by default `%USERPROFILE%\.codex\generated_images\`); the bridge reads the image of the current request there (only a `.png` in the folder the image tool created for that run) and never deletes anything in that folder. Claude runs with `--no-session-persistence`. Grok Build owns and manages its ACP session history and may persist request content; the bridge never scans or deletes that store.

## Data sent to providers

- Chat and image content required for a request is provided to the selected locally installed Claude Code, Codex, or Grok Build CLI.
- When `tts.provider` is `fish` and a Fish Audio key is configured, the text of every TTS request (and the chosen voice id, speed and format) is sent to `https://api.fish.audio` (or the `--fish-base-url` override, which accepts any `https://` host or loopback `http://`) with the stored key as a bearer token, and the voice list is fetched from the same host. Fish Audio's terms and privacy policy apply to that content.
- Embedding input, together with the stored embedding key as a bearer token, is sent to the configured OpenAI Platform endpoint only when the optional embedding endpoint is called and an embedding key is configured. The endpoint can be changed with `--openai-base-url`; only `https://` URLs with a hostname, or `http://` URLs whose host is `localhost` or a loopback IP address (`127.0.0.0/8`, `::1`), are accepted, and whoever controls that URL receives the key, so do not set it to an address you do not trust.
- The bridge does not independently call a Grok image REST API or expose a separate Grok image model setting.

The selected CLI or API may transmit content to its vendor. Review that vendor's terms, retention controls, and privacy policy before use.

- In side-by-side mode (`port` different from `player2.port`), requests for paths the bridge does not own are forwarded to the Player2 app on `127.0.0.1:<player2.port>` (4315 by default) with their body as received (only the `Host` header is rewritten and hop-by-hop headers are dropped), including TTS/STT content. The bridge logs only method, path (without query), status, and elapsed time for those requests. The Player2 app and its vendor handle that content under their own policies.

## Credentials and local secrets

The Windows launchers store only the optional embedding key and the optional Fish Audio key with Windows DPAPI for the current Windows user, in `%LOCALAPPDATA%\Player2CLIBridge\openai-key.dpapi` and `fish-audio-key.dpapi`. Neither key is passed to a provider CLI. Provider CLI authentication is managed by each provider CLI.

## Files written outside the bridge folder

- `%USERPROFILE%\.grok\hooks\player2-cli-bridge-deny-tools.json` (or `$GROK_HOME\hooks\`): a Grok Build `PreToolUse` hook installed the first time a Grok chat request is handled. Its only command exits with code 2 (deny) when the environment variable `PLAYER2_CLI_BRIDGE_DENY_TOOLS` is present and exits 0 otherwise; the bridge sets that variable only for its own Grok chat child processes. It contains no user data. Delete the file to uninstall.
- The DPAPI key files above, only when `SET-OPENAI-KEY.cmd` or `SET-FISH-KEY.cmd` is used.
- Grok Build's own session store under `~/.grok/sessions/`, written by Grok Build itself for every ACP session; the bridge never scans or deletes it.

## Diagnostics (off by default)

Two launch options write request content to disk and exist only for troubleshooting. Remove what they wrote when done.

- `--dump-cli-events DIR` (`START-BRIDGE.ps1 -DumpCliEvents DIR`) writes the JSON lines that every Claude Code, Codex and Grok Build run prints on its standard output to one file per run in `DIR`: the model's messages, the tool calls and commands it made with their inputs and results, file paths, and for Grok the ACP messages it sends. What the bridge sends to the CLI (the prompt and reference images) is not written, but model messages and tool inputs can quote request content. On POSIX the folder is created 0700 and each file 0600; on Windows they inherit the ACL of the chosen folder.
- `--keep-image-jobs` (`START-BRIDGE.ps1 -KeepImageJobs`) keeps each image request directory: the reference images and a copy of the returned image as `output.png`.

While either option is on, the log also prints the dump file path (`cli_event_dump=...`) and the kept directory (`cleanup=kept path=...`).

## Logs

Bridge logs contain timestamps, request type, selected provider, TTS outcome tokens (status, failure class, fallback path), safe progress states, elapsed time, HTTP and provider-process exit codes, allowlisted structured machine error codes, image dimensions, and output byte counts. Unknown provider-controlled codes are suppressed. Logs are designed not to contain:

- prompts or dialogue content
- reference image bytes
- credentials
- raw provider stdout/stderr
- provider-generated text on parse errors
- absolute private job paths (except when the diagnostic options above are on)

When an image output fails validation, the log records its byte count and a file-type label (`output_invalid bytes=... kind=...`), never its contents, or the operating-system error class and codes when the file cannot be opened (`output_unreadable ...`). When a Grok ACP call fails, the log records the upstream HTTP status number and a fixed hint (`acp_error ... upstream_http_status=402 hint=usage_or_credits_exhausted`), not the provider's message.

Each bridge launch replaces `LAST.log` with the safe console diagnostics for that run. `TEST-IMAGE.cmd` writes `LAST-IMAGE-TEST.log` and may save the generated test image in the bridge folder. Users should inspect and remove diagnostic artifacts before sharing or redistributing a folder.

On Windows, chat and image prompts are deliberately displayed in the launcher's console through the `CONOUT$` console handle. This console-only display bypasses stdout and stderr, so `Tee-Object` does not copy the prompt into `LAST.log`.

## Network listeners

The bridge binds only to IPv4 loopback (`127.0.0.1`) and refuses non-loopback bind addresses. It does not expose a public network service.
