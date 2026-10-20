# Game AI CLI Bridge 0.4.17

> 이 프로젝트는 Elefant AI Inc. 및 Player2 와 무관한 비공식 독립 프로젝트이며, 어떠한 제휴·보증·지원 관계도 없습니다. "Player2" 는 API 호환 대상을 설명하기 위해서만 사용합니다.

Player2 API 를 쓰는 게임·모드의 요청을 로컬(`127.0.0.1`)에서 받아, PC 에 설치된 AI coding CLI(Claude Code, Codex, Grok Build) 구독으로 처리하는 Windows 용 bridge 입니다. 게임 쪽에서 Player2 URL 만 이 bridge 로 바꾸면 chat·image 요청은 CLI 로 가고, bridge 가 구현하지 않은 요청(TTS/STT 등)은 원래 Player2 앱으로 전달할 수 있습니다. `tts.provider` 를 `fish` 로 두고 Fish Audio 키를 설정하면 TTS 는 Fish Audio API 로 직접 처리합니다([TTS (Fish Audio)](#tts-fish-audio) 절).

**본인 계정으로, 본인 PC 에서만 사용하세요.** 이 bridge 는 한 사람이 자신의 Windows PC 에서, 직접 설치하고 직접 로그인한 provider CLI 로 게임을 돌리는 용도입니다. Provider 로그인 과정을 대신하지 않고 CLI 자격증명을 읽거나 저장하지 않으며, 하나의 계정·구독을 다른 사람과 공유하거나 다른 사람의 요청을 처리하는 데 써서는 안 됩니다. 어떤 사용이 허용되는지는 각 vendor(Anthropic, OpenAI, xAI, Fish Audio, Player2)의 약관이 정하며, 약관 준수는 사용자 책임입니다. 구독 플랜은 보통 개인의 일반적인 대화형 사용을 전제로 하므로, 게임이 보내는 자동 요청이 본인 플랜에 맞는지 확실하지 않다면 API key 를 사용하세요([Credential](#credential) 절).

두 가지 사용 방식이 있습니다.

| 방식 | 설정 | 동작 |
|---|---|---|
| 대체 | `port` 와 `player2.port` 가 같음 (둘 다 기본 4315) | Player2 앱 대신 bridge 가 Player2 port 를 차지. chat·image·embedding 만 제공. TTS voice 목록은 200 빈 목록, TTS/STT 동작은 HTTP 501 (Fish Audio TTS 를 설정한 경우 제외) |
| 병행 | `port` 와 `player2.port` 가 다름 (예: bridge 4316, Player2 앱 4315) | bridge 고유 경로는 CLI 로 처리, 그 외 요청은 `player2.passthrough` 가 허용하는 범위에서 Player2 앱으로 passthrough (STT 포함). TTS 도 passthrough 되지만 Fish Audio TTS 가 활성이면 bridge 가 먼저 처리하고, Fish 실패 시 `tts.fallback` 이 켜져 있을 때만 passthrough |

## Provider

| 기능 | Provider |
|---|---|
| Chat | Claude Code CLI, Codex CLI, Grok Build CLI |
| Image 생성·편집 | Codex CLI agent, Grok Build CLI agent |
| Embedding | 선택적 OpenAI Platform API |
| TTS | Fish Audio API (선택, API 키 필요). 없으면 병행 방식에서 Player2 앱으로 passthrough |
| STT / 그 외 | Player2 앱으로 passthrough (병행 방식에서만) |

Chat provider 는 실행하는 launcher(`start-claude.cmd`, `start-codex.cmd`, `start-grok.cmd`)가 정하고, image provider 는 `bridge-config.json` 의 `image.provider` 가 정합니다. Image primary provider 가 재시도 가능한 오류로 실패하면 fallback provider 를 최대 한 번 호출합니다. 잘못된 입력과 일반적인 HTTP 4xx 오류에는 fallback 하지 않습니다. Primary 와 fallback 은 기본 300초 deadline 하나를 공유합니다(`--timeout` 으로 변경). primary 가 다 쓰면 fallback 은 시간이 남지 않습니다.

## 설정 (`bridge-config.json`)

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

| 설정 | 의미 |
|---|---|
| `port` | bridge 가 listen 하는 port. 기본 `4315`. 실행 시 `--port` 를 주면 그 값이 우선 |
| `player2.port` | passthrough 가 전달할 Player2 앱의 port. 기본 `4315` |
| `player2.passthrough` | Player2 앱 전달 모드: `all`(고유 경로 외 전부), `none`(전달 안 함), `list`(`allow` 접두사만). 기본 `all` |
| `player2.allow` | `list` 모드에서 전달할 raw path 접두사 목록. 기본 `/v1/tts/`, `/v1/stt/` |
| `claude` / `codex` / `grok` | 각 CLI 에 넘길 `model` 과 `effort`. 설치된 CLI 와 계정이 실제 지원하는 값이어야 하며 bridge 가 임의로 바꾸지 않음 |
| `image.provider` / `image.fallback_provider` | image 요청을 처리할 CLI 와 fallback (`codex` 또는 `grok`, fallback 은 `null` 가능) |
| `image.width` / `image.height` | 요청에 크기가 없을 때 쓰는 크기(128~4096). 요청 자체의 `width`/`height` 나 `aspect_ratio` 가 있으면 그것이 우선 |

`EDIT-MODEL-EFFORT.cmd` 로 메모장에서 열 수 있습니다. TTS 설정(`tts.*`)은 [TTS (Fish Audio)](#tts-fish-audio) 절을 보세요.

## 지원 endpoint

| Player2 endpoint | 상태 |
|---|---|
| `GET /v1/health` | bridge 가 응답. `tts` 는 Fish Audio TTS 활성 또는 TTS 경로 passthrough 허용 시 true, `stt` 는 passthrough 여부만 반영 |
| `POST /v1/chat/completions` | 선택한 chat CLI. `response_format`(`json_schema` / `json_object`)을 지원합니다. Claude(`--json-schema`)와 Codex(`--output-schema`)는 `json_schema` 를 네이티브로 강제하며, `type` 이 없는 `const`/`enum` 에는 먼저 값의 type 을 채워 넣습니다(Codex strict 모드가 요구). 그래도 CLI 가 스키마를 거부하면(Claude: 알 수 없는 키워드, Codex: `additionalProperties: false` 가 없는 객체 등 OpenAI strict 스키마 밖) `structured_output=native_rejected` 를 로그에 남기고 스키마를 프롬프트에 넣어 한 번 다시 시도합니다. 이 재시도와 Grok(시스템 프롬프트에 스키마 삽입), Codex `json_object`(프롬프트 지시)는 형식을 강제하지 않습니다. JSON 텍스트는 `message.content` 로 돌려주며, 모델이 유효하지 않은 JSON 을 내도 200 으로 그 텍스트를 주고 `structured_output=invalid` 를 로그에 남기므로 클라이언트가 검증해야 합니다 |
| `POST /v1/image/generate`, `POST /v1/image/edit` | image CLI 의 native image tool |
| `POST /v1/embeddings` | OpenAI Platform API (선택, key 필요) |
| `GET /v1/tts/voices`, `POST /v1/tts/speak`, `POST /v1/tts/stream` | `tts.provider` 가 `fish` 이고 키가 있으면 Fish Audio. 아니면 병행 방식은 Player2 앱으로 passthrough, 대체 방식은 빈 목록 / HTTP 501 |
| STT endpoint | 병행 방식이면 Player2 앱으로 passthrough. 대체 방식이면 HTTP 501 |
| 그 외 경로 | 병행 방식 + passthrough 허용이면 전달 (WebSocket 포함). 아니면 404. `OPTIONS` 는 항상 bridge 가 204 로 직접 응답하고 전달하지 않음 |

bridge 고유 경로는 `GET /`, `/health`, `/v1/health`, `/api/version`, `/api/tags`, `/v1/models`; `POST /api/show`, `/v1/embeddings`, `/embeddings`, `/v1/image/generate`, `/image/generate`, `/v1/image/edit`, `/image/edit`, `/api/chat`, `/api/generate`, `/v1/chat/completions`, `/chat/completions` 입니다. 그 외 경로는 `port` 가 `player2.port` 와 다르고 **동시에** `player2.passthrough` 가 허용할 때만(`all`, 또는 `list` 에서 접두사 일치) 전달됩니다. Fish Audio TTS 가 활성이면 `GET /v1/tts/voices`, `POST /v1/tts/speak`, `POST /v1/tts/stream`(및 `/tts/...` alias)도 고유 경로가 됩니다.

## 준비물

- Windows 10 또는 11
- `py -3` 또는 `python` 으로 실행 가능한 Python 3.10 이상
- 사용할 chat provider CLI 설치 및 로그인
- Image 기능: image generation tool 을 쓸 수 있는 Codex CLI 또는 Grok Build CLI
- Player2 URL(주소·port)을 바꿀 수 있는 게임 또는 모드
- 병행 방식: Player2 데스크톱 앱 (`player2.port`, 기본 4315 에서 실행)

CLI 구독, CLI 로그인, provider API 과금은 각 vendor 정책을 따릅니다. 각 CLI 는 구독 계정이나 API key 중 어느 쪽으로 로그인해도 되며 bridge 동작은 같습니다. OpenAI Platform API key 는 선택적인 embedding endpoint 에만 필요합니다.

## Provider CLI 설치 및 로그인

사용할 provider 만 설치하면 됩니다. 기본 설정에서 image 기능은 Codex 가 필요하고, Grok 은 Codex 가 재시도 가능한 오류로 실패했을 때만 fallback 으로 호출되므로 선택입니다. 설치 후 새 PowerShell 창을 열어 `PATH` 가 반영됐는지 확인하세요. 아래는 각 vendor 공식 문서의 Windows 설치 방법입니다. 원격 설치 script 를 실행하기 전에 함께 적힌 공식 문서에서 직접 확인하세요.

### Claude Code CLI

```powershell
irm https://claude.ai/install.ps1 | iex
```

대안: `winget install Anthropic.ClaudeCode` 또는 Node.js 18+ 환경에서 `npm install -g @anthropic-ai/claude-code`. 한 가지 방식만 사용하세요. 확인과 로그인: `claude --version`, `claude`. 공식 문서: <https://code.claude.com/docs/en/setup>

### Codex CLI

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

대안: `npm install -g @openai/codex`. 확인과 로그인: `codex --version`, `codex` 실행 후 **Sign in with ChatGPT**. 공식 문서: <https://developers.openai.com/codex/cli/>

### Grok Build CLI

```powershell
irm https://x.ai/cli/install.ps1 | iex
```

대안: `npm install -g @xai-official/grok`. 확인과 로그인: `grok version`, `grok login`, `grok models`. Browser 를 쓸 수 없으면 공식 문서의 `grok login --device-auth` 를 확인하세요. Image 기능을 쓰려면 로그인한 계정과 설치된 CLI 에서 Grok Build native `image_gen`/`image_edit` tool 을 쓸 수 있어야 합니다. 공식 문서: <https://docs.x.ai/build/overview>, <https://docs.x.ai/build/cli/reference>

## 최초 설정

1. ZIP 을 일반 쓰기 가능 폴더에 압축 해제합니다. ZIP viewer 안에서 바로 실행하지 마세요.
2. 사용할 provider CLI 를 설치하고 로그인합니다.
3. `bridge-config.json` 을 확인합니다. 병행 방식이면 `port` 를 `player2.port`(기본 4315)와 다른 값으로 바꿉니다.
4. 게임·모드의 Player2 URL 을 `http://127.0.0.1:<port>` 로 설정합니다.
5. chat provider 에 맞춰 `start-claude.cmd`, `start-codex.cmd`, `start-grok.cmd` 중 하나를 실행합니다. 시작 배너의 `Player2  :` 줄에서 passthrough 상태를 확인할 수 있습니다.
6. `check-bridge.cmd` 로 health endpoint 를, `TEST-IMAGE.cmd` 로 image 생성을 확인합니다.

Launcher 를 실행할 때마다 bridge 폴더의 `LAST.log` 를 새로 만들고 안전한 진단 내용만 기록합니다. Chat 과 image prompt 는 launcher console 에만 표시되고 `LAST.log` 에는 기록되지 않습니다.

## TTS (Fish Audio)

Player2 목소리 목록엔 한국어 목소리가 없습니다. 2026년 9월 기준 `https://api.player2.game/v1/tts/voices` 가 돌려주는 54개 목소리는 미국·영국 영어, 중국어(만다린), 일본어, 힌디어, 스페인어, 브라질 포르투갈어, 이탈리아어, 프랑스어이고, 기본 목소리를 고르는 `voice_language` 옵션에도 한국어가 없습니다. 이 목소리들이 한국어 문장을 읽을 수 있는지, Player2 계정에 추가할 수 있는 Inworld 커스텀 목소리가 읽을 수 있는지는 테스트하지 않았습니다. bridge 가 `/v1/tts/speak`, `/v1/tts/stream`, `/v1/tts/voices` 를 Fish Audio API 로 직접 처리할 수 있습니다.

1. Fish Audio 계정과 API 키를 만듭니다(https://fish.audio). TTS 는 텍스트의 UTF-8 바이트 단위 종량제이며(작성 시점 100만 바이트당 $15, `s2.1-pro-free` 모델은 $0) TTS 요청의 텍스트가 Fish Audio 로 전송됩니다.
2. `SET-FISH-KEY.cmd` 를 실행합니다. 키는 Windows DPAPI 로 `%LOCALAPPDATA%\Player2CLIBridge\fish-audio-key.dpapi` 에 저장되고 launcher 가 `PLAYER2_FISH_AUDIO_KEY` 로 bridge 에 넣어줍니다(`bridge-config.json` 에 쓰이거나 provider CLI 로 전달되지 않음). 삭제는 `REMOVE-FISH-KEY.cmd`.
3. `tts.provider` 기본값이 `fish` 라 추가 설정은 없습니다. 키가 있어도 Fish Audio 를 끄려면 `none` 으로 바꾸세요.

| 설정 | 의미 |
|---|---|
| `tts.provider` | `fish`(기본) 또는 `none` |
| `tts.model` | Fish `model` 헤더로 보낼 모델. 기본 `s2.1-pro-free` |
| `tts.reference_id` | 게임이 목소리를 고르지 않았을 때 쓸 Fish 목소리(model id). `null` 이면 Fish 기본 목소리 |
| `tts.language` | 목소리 목록의 언어 필터(예: `ko`). 기본 `ko` |
| `tts.fallback` | `true`(기본): Fish 실패 시 Fish 가 없을 때의 동작으로 폴백(병행 모드면 passthrough, 아니면 501/빈 목록). `false`: 502 `tts_failed` |
| `tts.sample_rate` | `wav`·`pcm` 출력의 샘플레이트: 8000, 16000, 24000, 32000, 44100 중 하나. 기본 24000 — Player2 클라이언트가 `pcm` 을 그렇게 가정합니다(AI Influence 모드는 raw 샘플을 24 kHz mono 로 인코딩). mp3 에는 적용 안 됨 |

키가 설정된 상태의 동작:

- `GET /v1/tts/voices` 는 `tts.language` 의 Fish 공개 목소리 목록(최대 50, 점수순, 60초 캐시)을 돌려주고 설정 `reference_id` 가 목록에 없으면 맨 앞에 넣습니다(이미 있으면 원래 위치 유지). 커뮤니티 목소리라 항목이 바뀌거나 중복되거나 사라질 수 있고 라이선스는 업로더 책임입니다. bridge 가 한 번 보여준 id 와 설정 id 는 프로세스가 사는 동안 계속 받아들입니다.
- `POST /v1/tts/speak` 는 `voice_ids[0]` 이 그 알려진 id 면 그것을, 아니면 `tts.reference_id` 를 씁니다. `speed` 는 Fish `prosody.speed` 로, `audio_format` 의 `mp3`·`wav`·`pcm` 은 그대로(`pcm` 은 헤더 없는 16-bit mono raw 샘플, 샘플레이트는 `tts.sample_rate`, `mimetype` 은 `audio/pcm`. `wav` 도 같은 샘플레이트), `ogg` 와 그 밖의 값은 `mp3` 로 응답합니다(응답 `format` 에 표시되고 요청값은 짧은 토큰으로 로그에 남음, 예: `tts_format=ogg served=mp3`). 응답은 base64 오디오를 `data`(현재 Player2 스펙의 필드명)와 `audio` 두 키로 담고 `format`, `mimetype`, `duration` 을 더한 형태입니다. `duration` 은 wav·pcm 은 오디오 데이터로 계산(밀리초 반올림), mp3 는 바이트 수로 추정한 값입니다.
- `POST /v1/tts/stream` 은 Fish 의 오디오 바이트를 받는 대로 스트리밍합니다. 폴백은 첫 바이트가 클라이언트에 가기 전의 실패(연결, 401, 402, 429, 5xx, 첫 chunk 타임아웃)에만 적용되고, 그 뒤의 실패는 연결을 닫습니다.
- 키가 없으면 TTS 경로는 이전과 완전히 같게 동작합니다.

## Player2 passthrough

`port` 가 `player2.port` 와 다르면 bridge 고유 경로가 아닌 요청을 `127.0.0.1:<player2.port>`(기본 4315)의 Player2 앱으로 전달합니다. method·경로·query·본문은 받은 그대로 넘깁니다. `Host` 헤더는 Player2 앱 주소로 바꾸고, hop-by-hop 헤더(`Connection`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade`, proxy/TE/trailer 계열)는 양방향에서 제거하며, `Transfer-Encoding` 을 쓰는 요청은 `Content-Length` 본문만 전달하므로 HTTP 411 로 거부합니다. 응답 상태·나머지 헤더·본문은 그대로 돌려주고 redirect 는 따라가지 않습니다. `OPTIONS` 요청은 항상 bridge 가 204 로 직접 응답합니다. WebSocket upgrade 요청은 양방향 byte tunnel 로 전달합니다. Player2 앱이 꺼져 있으면 HTTP 502 (`upstream_unavailable`)를 돌려줍니다.

`player2.passthrough` 는 세 모드입니다. `all` 은 고유 경로 외 전부, `none` 은 전달 안 함, `list` 는 raw path 가 `player2.allow` 항목으로 시작할 때만 전달합니다. `port` 가 `player2.port` 와 같으면 자기 자신에게 전달하는 것을 막기 위해 passthrough 가 항상 꺼집니다.

Passthrough 경로에는 bridge 의 추가 보안 검사를 적용하지 않습니다. Origin 을 검사하지 않고, 본문 한도가 없으며, `allow` 접두사는 raw path 문자열 비교라 `..` 나 인코딩된 경로를 정규화하지 않습니다. Idle WebSocket tunnel 은 수명 내내 bridge 의 요청 slot(32개) 중 하나를 점유합니다. 이 bridge 는 loopback 전용이며 passthrough 는 bridge 없이 Player2 앱에 직접 접근할 때와 같은 신뢰 경계를 그대로 두는 것이 의도된 동작입니다.

## Codex 연동 방식

Codex 는 `codex exec --ephemeral`(session 파일을 남기지 않음)을 요청마다 새로 만든 비공개 디렉터리에서 실행하고 끝나면 지웁니다. chat 은 read-only sandbox 이고, 디렉터리는 `response_format` 스키마가 있는 요청일 때 `schema.json` 이 있는 것 외엔 비어 있습니다. image 요청도 read-only sandbox 입니다. reference 이미지를 그 디렉터리에 두면 Codex 가 내장 image tool 을 호출해 PNG 를 `%CODEX_HOME%\generated_images\`(기본값 `%USERPROFILE%\.codex\generated_images\`) 아래에 저장하고 마지막 메시지에 그 경로를 보고합니다. image tool 은 실행마다 그 실행 이름의 폴더(`generated_images\<thread_id>\`, Codex 가 실행을 시작할 때 알려주는 ID)에 저장하며, bridge 는 Codex 의 마지막 메시지에 보고된 경로가 그 폴더 바로 안의 `.png` 일 때만 받아들입니다. 그래서 prompt injection 으로 다른 이미지(예전 이미지나 다른 Codex 실행의 이미지)나 다른 파일을 가리키게 해도 거부됩니다. 예외가 하나 있습니다. Codex 가 경로를 보고하지 않았는데 요청 전용 비공개 디렉터리(요청마다 새로 만듦)에 유효한 `output.png` 가 있으면 그 파일을 받아들입니다. 더 이상 sandbox 안 셸 명령으로 파일을 복사하지 않습니다(Windows 에서 그렇게 복사한 파일은 bridge 가 읽지 못했습니다). 두 경우 모두 Codex 내장 도구는 Windows 사용자가 읽을 수 있는 파일을 읽을 수 있으므로, 게임 텍스트에 섞인 prompt injection 이 로컬 파일을 읽어 인용하게 만들 수 있습니다. 이는 Codex 를 직접 쓸 때와 같은 신뢰 모델입니다. Claude chat 은 도구를 끄고 실행합니다.

## Grok Build 연동 방식

Grok chat 과 image 요청은 Grok Build 의 공식 ACP JSON-RPC stdio interface(`grok agent stdio`)를 사용합니다. 게임의 system prompt 는 `session/new._meta.systemPromptOverride` 로, 대화 본문은 `session/prompt` 로 분리해 전달합니다.

Grok Build 의 ACP stdio 모드는 `--tools`, `--disallowed-tools`, `--deny`, `--permission-mode`, `--max-turns` 를 무시합니다(headless 전용 flag). 그래서 chat session 은 두 겹으로 막습니다.

- system prompt 앞에 "tool 사용 금지" 규칙을 붙입니다.
- bridge 가 사용자 전역 Grok hook 파일 `%USERPROFILE%\.grok\hooks\player2-cli-bridge-deny-tools.json`(`GROK_HOME` 설정 시 `$GROK_HOME\hooks\`)을 설치합니다. 이 `PreToolUse` hook 은 환경변수 `PLAYER2_CLI_BRIDGE_DENY_TOOLS` 가 있을 때만 모든 tool 호출을 거부하고, bridge 는 자기 chat child process 에만 이 변수를 넣으므로 직접 쓰는 Grok session 에는 영향이 없습니다. 파일을 지우면 제거됩니다. 거부된 호출은 `LAST.log` 에 `[grok/chat] event session_update=tool_call_update status=failed` 로 남습니다.

Image session 은 hook 변수를 넣지 않고, 대신 짧은 backend 지시문으로 첫 turn 에 `image_edit`(reference 있음) 또는 `image_gen`(없음)을 정확히 한 번 호출하게 합니다. 지시문이 없으면 Grok Build 가 coding agent 처럼 파일과 skill 문서를 읽다가 turn 제한을 소진해 실패합니다. Reference image 는 같은 `session/prompt` 의 ACP `image` content block 으로 전달하며 command-line 이나 임시 파일에 저장하지 않습니다. bridge 는 Grok 에 reference image 를 앞의 3장까지만 첨부합니다(로그 `refs=N sent=3`). Grok Build 1.0.13 으로 시험했을 때(2026년 9월) image tool 이 4장째를 HTTP 400 으로 거부했기 때문입니다. Grok Build 는 자체 session history 를 관리하고 요청 내용을 저장할 수 있으며, bridge 는 그 store 를 탐색하거나 삭제하지 않습니다.

## Image 처리

출력 크기: 요청에 Player2 API 대로 `width`·`height`(128~4096) 또는 `aspect_ratio`(21:9, 16:9, 3:2, 4:3, 5:4, 1:1, 4:5, 3:4, 2:3, 9:16, 9:21 중 하나, 우선 적용)를 담을 수 있고, 없으면 설정의 `image.width`/`image.height` 를 씁니다(AI Influence 모드는 편집 요청에 크기를 보내지 않습니다). Codex 에는 그 크기와 reference 이미지를 프롬프트로 전달하고, image tool 에 그 크기 또는 tool 이 지원하는 가장 가까운 크기를 요청하되 명령 실행·후처리는 금지하며, tool 이 저장한 PNG 의 경로를 보고하게 합니다. bridge 는 그 파일을 검증해 돌려줍니다. Grok 은 가장 가까운 aspect ratio 를 쓰며 `auto` 는 쓰지 않습니다. Grok 은 native image tool 이 JPEG 를 만들므로 bridge 가 PNG 로 변환해 돌려줍니다(Player2 `/v1/image/generate` 계약은 base64 PNG). 변환은 Pillow 가 설치돼 있으면 Pillow, 없으면 Windows PowerShell `System.Drawing` 을 사용합니다.

- Reference image 는 요청당 최대 8개, base64·body 크기 제한 적용, PNG/JPEG/WebP 만 허용. Codex 는 전부 받고 Grok 은 앞의 3장만 받습니다(위 참고). Decode 가능 여부는 provider 가 판단합니다.
- 결과는 bounded regular file 로 읽고 PNG/JPEG 구조(signature·chunk·CRC·marker·dimension ≤ 4096)를 검증한 뒤에만 반환합니다.
- Grok 결과는 현재 session ID 와 요청별 무작위 `promptId` 가 모두 일치하는 완료 tool update 의 typed `rawOutput`(`ImageGen`/`ImageEdit`) 하나만 받고, 그 절대 경로만 읽습니다.
- Provider launch, stdin, stdout/stderr, process-tree 종료, cleanup 은 하나의 absolute deadline 을 공유합니다.
- 진단용 `--keep-image-jobs` 를 쓰지 않으면 bridge 가 만든 요청 파일과 reference 는 성공·실패 모두 정리합니다. Codex image tool 이 `%CODEX_HOME%\generated_images\`(기본값 `%USERPROFILE%\.codex\generated_images\`) 에 저장한 PNG 는 Codex 의 파일이라 그대로 남습니다.

`TEST-IMAGE.cmd` 는 실제 image provider 요청을 보내므로 credit 이나 quota 를 쓸 수 있습니다. 성공 이미지는 bridge 폴더에 저장되고 로그는 `LAST-IMAGE-TEST.log` 입니다.

## Credential

- Claude child: `ANTHROPIC_API_KEY` 만 전달 가능
- Codex child: `OPENAI_API_KEY` 만 전달 가능
- Grok child: `XAI_API_KEY` 만 전달 가능
- 선택 embedding: 내부 `PLAYER2_OPENAI_EMBEDDING_KEY`
- 선택 Fish Audio TTS: 내부 `PLAYER2_FISH_AUDIO_KEY` (provider CLI 에 전달되지 않음)

Provider CLI 를 구독 대신 API 과금으로 쓰려면, vendor 문서의 방법으로 해당 CLI 에 API key 로 로그인하거나 launcher 를 실행하는 환경에 위 변수를 설정하세요. Bridge 는 해당 CLI 에 그 변수 하나만 전달합니다.

`SET-OPENAI-KEY.cmd` 와 `SET-FISH-KEY.cmd` 는 선택적인 embedding key 와 Fish Audio key 를 현재 Windows 사용자의 DPAPI 로 `%LOCALAPPDATA%\Player2CLIBridge\` 에 저장합니다. Codex CLI 로그인과는 무관합니다. 삭제는 `REMOVE-OPENAI-KEY.cmd` / `REMOVE-FISH-KEY.cmd` 입니다. Credential 을 `bridge-config.json`, command line, screenshot, bug report 에 넣지 마세요.

## 보안 경계

Bridge 는 IPv4 loopback `127.0.0.1:<port>` 에만 bind 하고 외부 bind 를 거부합니다. 고유 경로는 외부 browser Origin 을 차단하고 POST JSON content type·body size·prompt size·동시 provider job 수를 제한합니다. Passthrough 경로는 위에서 설명한 대로 Player2 앱 직접 접근과 같은 신뢰 경계를 유지합니다. 별도 인증은 없으며, 같은 Windows 사용자 권한으로 실행되는 다른 local process 를 막지는 않습니다. 자세한 threat model 은 `SECURITY.md` 를 확인하세요.

## Log 와 privacy

Telemetry 는 없습니다. 요청은 로컬에서 처리된 뒤 선택한 CLI, Player2 앱(passthrough), Fish Audio API(설정 시 TTS 텍스트), 선택 embedding API 에 필요한 내용만 전달됩니다. 로그에는 prompt, reference image, credential, provider raw output, private job 의 절대 경로를 기록하지 않습니다. 자세한 내용은 `PRIVACY.md` 를 확인하세요.

기본으로 꺼져 있는 진단 옵션: `--keep-image-jobs` 는 image 요청 디렉터리(reference 와, 돌려준 이미지의 복사본 `output.png`)를 지우지 않고 남깁니다(로그에 `cleanup=kept path=...`). `--dump-cli-events DIR` 은 Claude Code·Codex·Grok Build 실행마다 CLI 가 표준 출력으로 내보낸 JSON 줄을 `DIR` 에 파일 하나로 남깁니다(경로는 `cli_event_dump=...` 로 출력). 여기엔 모델 메시지, 모델이 호출한 tool·명령과 그 입력·결과, Grok 이면 Grok 이 보내는 ACP 메시지가 들어갑니다. bridge 가 CLI 에 보내는 내용(prompt 와 reference 이미지)은 기록하지 않지만, 모델 메시지와 tool 입력에 요청 내용이 인용될 수 있습니다. Claude Code·Codex 의 64 KiB 가 넘는 줄은 `dump_omitted` 표시로 대신합니다. POSIX 에서는 폴더를 본인 전용(0700), 파일을 0600 으로 만들고, Windows 에서는 고른 폴더의 ACL 을 따르므로 본인만 읽을 수 있는 폴더를 고르고 다 쓰면 지우세요. image 출력이 검증에 실패하면 로그에 파일 크기와 파일 종류만 남기고 내용은 남기지 않습니다(`output_invalid bytes=... kind=png|jpeg|webp|gif|other|empty|unreadable`). 결과 파일이 있는데 열리지 않으면(Windows 에서는 방금 쓴 파일을 백신 검사가 몇 초 잡고 있을 수 있음) 요청 제한 시간 안에서 최대 15초까지 다시 시도하고, 그래도 안 되면 `output_unreadable error=... errno=... winerror=...` 를 남깁니다. launcher 는 `-KeepImageJobs`, `-DumpCliEvents <dir>` 를 받아 그대로 전달합니다.

## 검증 상태

- **실사용 테스트**: Mount & Blade II: Bannerlord 의 AI Influence 모드(Windows)에서 Codex 채팅, Grok Build 채팅, Codex 참조 이미지 편집(0.4.14 경로 보고 방식), Grok Build 이미지 생성·편집(폴백 포함), Fish Audio 한국어 TTS(`pcm` 24 kHz), OpenAI 임베딩, Player2 앱 병행 passthrough(TTS/STT)를 확인했습니다. Claude Code 채팅은 인게임 테스트를 하지 않았습니다. 다른 게임·모드에서의 실사용 테스트는 아직 없습니다.
- **소스 검토만**: Crusader Kings III 의 Voices of the Court(VOTC)는 소스 기준으로 호환성을 검토했습니다. VOTC 의 Player2 provider 는 주소가 `127.0.0.1:4315` 로 고정이라 대체 방식으로 써야 하고, actions 판정에 `response_format`(json_schema)을 씁니다. 실사용 테스트는 하지 않았습니다. Elin 의 Emmersive(Elin with AI)도 소스 검토만 했습니다: Player2 provider 주소가 마찬가지로 `127.0.0.1:4315` 고정이고, strict `json_schema` `response_format` 을 쓰며, 기본 요청 timeout 이 15초라 느린 provider 에는 짧을 수 있습니다.
- **자동 테스트**(`tests/test_passthrough.py`, stdlib 만) 51개: 설정·port 우선순위, passthrough 세 모드, redirect 비추종, close-delimited 응답, upstream 미기동 502, WebSocket tunnel 과 slot 반환, Origin 경계, hook 설치, `player2.port`, `response_format` 경로(가짜 CLI, `const`/`enum` type 보충과 CLI 가 스키마를 거부했을 때의 프롬프트 재시도 포함), Codex `--ephemeral` 과 비공개 작업 디렉터리, POSIX 디렉터리 권한, `--openai-base-url` 검증, chat 경로. Fish Audio TTS 는 가짜 Fish 서버로 speak·stream·voices·폴백 규칙·키 격리·`pcm`/`wav`/`mp3` 매핑·`tts.sample_rate` 를 검증. image 요청은 크기 결정(요청 `width`/`height` 나 `aspect_ratio`, 없으면 설정 기본값), Grok 비율 매핑, 가짜 Codex 로 경로 보고 방식(이번 실행의 `generated_images\<thread_id>\` 폴더에 있고 마지막 메시지에 보고된 경로만 수락, 다른 실행의 이미지·폴더 밖·`.png` 아님·앞선 메시지에만 있는 경로·실행 ID 없음·보고 없음은 거부), 거부된 비 PNG 출력 내용이 로그에 남지 않음, 요청 제한 시간을 넘기지 않는 출력 파일 재시도, Codex·Grok 의 `--dump-cli-events` 진단(POSIX 본인 전용 권한), `ogg` 로그, WAV 길이 반올림, Grok 경로의 참조 이미지 8장 초과 거부, Grok ACP 오류의 upstream HTTP 상태 로그를 검증.
- Claude Code·Codex·Grok Build 의 chat 과 `response_format` 경로는 Linux 에서 실제 CLI 로 확인했습니다. Windows 에서는 현재 버전의 `start-codex.cmd`, DPAPI 키 로드, 위에 적은 기능을 인게임으로 확인했고, 현재 버전의 `start-claude.cmd`·`start-grok.cmd`, `REMOVE-OPENAI-KEY.cmd`, `REMOVE-FISH-KEY.cmd`, `MANAGE-KEY.ps1 -Action Status`, `CHECK-BRIDGE`, `TEST-IMAGE`, 구조화 출력은 아직 Windows 재확인 전입니다.

### 테스트 기록

개발 중 기록된 날짜와 CLI 버전입니다. 기록되지 않은 버전은 그렇게 표시했습니다.

| 날짜 | Bridge | 확인한 내용 | CLI 버전 |
|---|---|---|---|
| 2026-10-03 | 0.4.17 | Voices of the Court 의 실제 `response_format` 요청을 bridge 를 거쳐 실제 Codex(`gpt-6.1-sol`)와 Claude Code(`opus`)로 처리: 4건 모두 200, 유효한 JSON | 기록 없음 |
| 2026-10-03 | 0.4.16 | `gpt-6.1-sol` 로 실제 Codex chat | 기록 없음 |
| 2026-09-29 | 0.4.15 | 실제 Codex chat 과 image generate(`gpt-6-sol`); `grok models` 로 `grok-4.7` 이 Grok Build 기본값임을 확인, Grok 종단 테스트는 사용량 소진(HTTP 402)으로 불가 | Grok Build 1.0.41, Codex 기록 없음 |
| 2026-09-07 | 0.4.14 | Windows 에서 read-only sandbox·경로 보고 방식으로 실제 Codex image generate(44초, 200) | Codex CLI 0.149.0 |
| 2026-09-03 | 0.1.0 | Windows 에서 Grok Build chat(한국어 롤플레이 system prompt)과 image generate/edit | Grok Build 1.0.13 |

위 "실사용 테스트" 항목은 2026-09-03 ~ 2026-09-29 사이에 인게임으로 확인했으며, 항목별 정확한 날짜는 기록되지 않았습니다. Claude Code CLI 버전은 어떤 테스트에서도 기록되지 않았습니다.

## 문제 해결

- **Bridge 연결 실패:** `start-*.cmd` 가 실행 중인지 확인하고 `check-bridge.cmd` 를 다시 실행하세요. `bridge-config.json` 의 `port` 와 게임의 Player2 URL 이 같은지 확인하세요.
- **CLI 를 찾지 못함:** 해당 CLI 를 설치하고 새 창에서 `PATH` 에 잡히는지 확인하세요.
- **Provider login 오류:** provider 자체 CLI 에서 로그인하세요.
- **TTS 동작이 501 (voice 목록은 빈 목록):** 대체 방식(`port` 와 `player2.port` 가 같음)에서는 제공하지 않습니다(Fish Audio TTS 활성 — `tts.provider` = `fish` + 키 — 이면 제외).
- **STT 동작이 501:** 대체 방식에서는 STT 를 제공하지 않습니다. 병행 방식으로 바꾸고 Player2 앱을 실행하세요.
- **502 `upstream_unavailable`:** Player2 앱이 `player2.port` 에서 실행 중인지 확인하세요.
- **Image timeout:** primary 와 fallback 이 같은 timeout 을 공유합니다. 게임의 요청 수명보다 길게 잡아도 성공하지 않습니다.
- **Embedding unavailable:** 필요하면 `SET-OPENAI-KEY.cmd` 로 OpenAI Platform key 를 설정하세요.

- **Codex image 요청이 `image_output_invalid` 로 실패하거나 몇 분씩 걸림**: `.\START-BRIDGE.ps1 -Provider codex -KeepImageJobs -DumpCliEvents .\p2dump` 로 띄우고 게임에서 이미지 요청을 한 번 발생시킨 뒤, `p2dump` 의 `.jsonl` 에서 Codex 가 image tool 을 호출했는지, 어떤 경로를 보고했는지, 금지된 명령을 실행하진 않았는지 보고, 로그의 `generated_png=` 줄도 확인하세요. Codex 가 만든 이미지 원본은 `%CODEX_HOME%\generated_images\`(기본값 `%USERPROFILE%\.codex\generated_images\`) 에 남으며 bridge 는 지우지 않습니다.
- **Grok 요청이 `cli_failed` 로 실패함**: 로그에서 `acp_error ... upstream_http_status=N hint=...` 를 찾으세요. `402` 와 `hint=usage_or_credits_exhausted` 는 Grok 계정의 Grok Build 사용량이 바닥났다는 뜻이고, `401` 은 Grok Build 에 다시 로그인해야 한다는 뜻입니다.

## License

MIT. `LICENSE.txt` 와 `NOTICE.md` 를 확인하세요.
