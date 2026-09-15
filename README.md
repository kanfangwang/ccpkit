# ccpkit

**One command to switch between API providers for Claude Code and Codex CLI.**
Official API, relay/mirror, or your own self-hosted gateway — keep several profiles, switch instantly, verify with a built-in doctor.

[English](README.md) | [中文说明](#中文说明)

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)]()

---

## Why

Claude Code reads `~/.claude/settings.json`, Codex CLI reads `~/.codex/config.toml`.
Switching provider means editing those files by hand, remembering which key goes where,
and breaking things when you switch back. `ccpkit` keeps every provider as a named
profile and writes the right config for you — with automatic backups.

## Features

- **Named profiles** for any provider: official, relay/mirror, self-hosted gateway
- **One-command switch** — `ccpkit use <name>`
- **Writes both clients** (Claude Code + Codex CLI) from a single profile
- **`doctor` self-check** — connectivity, latency, and model availability for every line
- **Automatic backups** of `settings.json` / `config.toml` before every write, `ccpkit restore` to roll back
- **`env` output** for people who prefer environment variables (bash / PowerShell / cmd)
- **Zero dependencies**, single file, Python 3.8+, Windows / macOS / Linux

## Install

Pick one:

```bash
# 1) pipx (recommended)
pipx install git+https://github.com/kanfangwang/ccpkit.git

# 2) uv
uvx --from git+https://github.com/kanfangwang/ccpkit.git ccpkit --help

# 3) plain python — just download the file
curl -LO https://raw.githubusercontent.com/kanfangwang/ccpkit/main/ccpkit.py
python ccpkit.py --help

# 4) Windows PowerShell one-liner
irm https://raw.githubusercontent.com/kanfangwang/ccpkit/main/ccpkit.py -OutFile ccpkit.py; python ccpkit.py --help
```

## Quick start

```bash
# add a provider profile (interactive if you omit the flags)
ccpkit add myrelay \
  --claude-base https://api.example.com/p        --claude-key sk-xxx --claude-model claude-sonnet-4-6 \
  --codex-base  https://api.example.com/p/v1     --codex-key  sk-yyy --codex-model gpt-5.5

# save and switch to it
ccpkit add myrelay ... --use

# start from a preset instead (presets only pre-fill URLs; the key is always yours)
ccpkit presets
ccpkit add mdd --preset mdd --claude-key sk-xxx --codex-key sk-yyy --use
ccpkit add official --preset anthropic --claude-key sk-ant-... --use

# switch later
ccpkit use myrelay

# only one client
ccpkit use myrelay --client codex

# check every line: alive? latency? is my model there?
ccpkit doctor myrelay --models

# list / inspect / remove
ccpkit list
ccpkit show myrelay
ccpkit remove myrelay

# prefer env vars instead of writing files?
ccpkit env myrelay --shell powershell
```

Example `doctor` output:

```
[myrelay]
  Claude OK  延迟 43 ms     (接口可用,未返回模型清单)
  Codex  OK  延迟 50 ms     模型 1 个 模型可用
          - gpt-5.5
全部正常
```

## Providers & presets

Works with any Anthropic-compatible or OpenAI-compatible endpoint: official APIs,
relays/mirrors, or your own gateway. Built-in presets only pre-fill base URLs and a
default model — **your key is always your own**: [`docs/providers.md`](docs/providers.md).

| Preset | Claude base | Codex base |
|---|---|---|
| `anthropic` | `https://api.anthropic.com` | – |
| `openai` | – | `https://api.openai.com/v1` |
| `mdd` (alias `mdd-relay`) | `https://api.mddtech.cn/p` | `https://api.mddtech.cn/p/v1` |

## What it writes

| Client | File | What changes |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | merges an `env` block: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL` |
| Codex CLI | `~/.codex/config.toml` | adds a managed block (`model`, `model_provider`, `[model_providers.ccpkit]` with `base_url` / `env_key` / `wire_api`) |
| Codex CLI | `~/.codex/auth.json` | sets `OPENAI_API_KEY` |

Existing files are **never destroyed**: a timestamped `*.ccpkit-bak-<time>` copy is made first,
and everything outside the managed block is left untouched.

```toml
# >>> ccpkit begin
model = "gpt-5.5"
model_provider = "ccpkit"

[model_providers.ccpkit]
name = "ccpkit"
base_url = "https://api.example.com/p/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
# <<< ccpkit end
```

## Environment variables

| Variable | Meaning |
|---|---|
| `CCPKIT_HOME` | where `profiles.json` lives (default `~/.ccpkit`) |
| `CCPKIT_CLAUDE_DIR` | override the Claude home dir (default `~`) |
| `CCPKIT_CODEX_DIR` | override the Codex home dir (default `~`) |
| `NO_COLOR` | disable colored output |

Handy for testing without touching your real config:

```bash
CCPKIT_HOME=/tmp/t CCPKIT_CLAUDE_DIR=/tmp/t CCPKIT_CODEX_DIR=/tmp/t ccpkit add test --use ...
```

## Privacy

- No telemetry, no analytics, no network calls except the ones you trigger with `doctor` / model listing.
- Keys are stored locally in `~/.ccpkit/profiles.json` (chmod 600 on POSIX).
- Nothing is uploaded anywhere.

## FAQ

**How do I go back to the official API?**
`ccpkit add official --claude-base https://api.anthropic.com --claude-key sk-ant-...` then `ccpkit use official`.
Prefer your previous file? `ccpkit restore --client claude`.

**Codex CLI ignores my model provider.**
Codex CLI must be restarted (open a new terminal). Also make sure `~/.codex/auth.json` or `OPENAI_API_KEY` is set.

**`doctor` says "接口可用,未返回模型清单".**
Some gateways don't expose a model list. The auth check still passed — that is normal.

**Windows shows garbled characters.**
Use Windows Terminal, or set `chcp 65001`. The tool falls back to ASCII markers on legacy code pages.

## Roadmap

- [ ] `ccpkit bench` — measure first-token latency and throughput
- [ ] provider presets (`ccpkit add --preset openai|anthropic`)
- [ ] `ccpkit usage` — daily token / cost report from local session logs
- [ ] Homebrew / Scoop formula

## License

MIT

---

## 中文说明

`ccpkit` 用来管理 **Claude Code / Codex CLI 的多套 API 线路**(官方、中转、自建网关),一条命令切换。

```bash
# 添加一条线路(不加参数会进入交互式问答)
ccpkit add 我的中转 \
  --claude-base https://api.example.com/p     --claude-key sk-xxx --claude-model claude-sonnet-4-6 \
  --codex-base  https://api.example.com/p/v1  --codex-key  sk-yyy --codex-model gpt-5.5

ccpkit use 我的中转        # 切换(写入两个客户端配置)
ccpkit doctor 我的中转      # 自检:连通性 / 延迟 / 模型是否可用
ccpkit list                # 查看所有线路,带 * 的是当前生效
ccpkit env 我的中转 --shell powershell   # 只想用环境变量,不写文件
ccpkit restore --client claude           # 恢复上次的配置备份
```

**它写的文件**
- Claude Code:`~/.claude/settings.json` 的 `env` 段(`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`)
- Codex CLI:`~/.codex/config.toml` 的托管块 + `~/.codex/auth.json` 的 `OPENAI_API_KEY`

**安全**:每次写入前自动备份为 `*.ccpkit-bak-<时间>`,只改托管块,不动你其它配置;key 只存在本地 `~/.ccpkit/profiles.json`,无任何上报。
