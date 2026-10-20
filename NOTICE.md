# Notices

Game AI CLI Bridge is distributed under the MIT License. See `LICENSE.txt`.

## Independence and trademarks

This bridge is an independent compatibility project. It is not affiliated with, sponsored by, or endorsed by:

- Elefant AI Inc. (Player2)
- Fish Audio
- AI Influence or its authors, or any other game or mod that uses the Player2 API
- Paradox Interactive (Crusader Kings III) and the authors of Voices of the Court
- Anthropic
- OpenAI
- xAI
- TaleWorlds Entertainment
- Valve Corporation

Player2, Fish Audio, AI Influence, Claude, Claude Code, Codex, OpenAI, Grok, Grok Build, Mount & Blade, Bannerlord, Crusader Kings, Steam, and other names may be trademarks of their respective owners. They are used only to identify compatibility targets.

## Third-party software and services

This package does not bundle the Player2 app, any game or mod, Python, Claude Code CLI, Codex CLI, Grok Build CLI, provider models, provider credentials, or game files. Users install and license those components separately and remain responsible for their terms, subscriptions, API usage, and generated content.

The bridge invokes locally installed provider CLIs and, when explicitly configured, the OpenAI Platform embedding API and the Fish Audio text-to-speech API. Provider CLIs and remote services are governed by their own licenses and privacy policies.

## Compatibility scope

The compatibility target is the Player2 local HTTP API as used by games and mods; AI Influence 6.0.2 was the original test client. This package does not modify or redistribute the AI Influence DLL, game binaries, save data, or prompt files.
