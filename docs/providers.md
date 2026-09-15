# Providers & presets

`ccpkit` works with **any endpoint that speaks the Anthropic Messages API or the
OpenAI-compatible API**. Presets only pre-fill the base URLs and a default model —
**you always bring your own key.**

List them with:

```bash
ccpkit presets
```

## Built-in presets

| Preset | Claude base | Codex base | Notes |
|---|---|---|---|
| `anthropic` | `https://api.anthropic.com` | – | official Anthropic API |
| `openai` | – | `https://api.openai.com/v1` | official OpenAI API |
| `mdd` (alias `mdd-relay`) | `https://api.mddtech.cn/p` | `https://api.mddtech.cn/p/v1` | third-party relay example | 

## Quick start with a preset

```bash
# official
ccpkit add official --preset anthropic --claude-key sk-ant-... --use

# relay example (mdd)
ccpkit add mdd --preset mdd --claude-key sk-xxx --codex-key sk-yyy --use
ccpkit doctor mdd --models
```

## About the `mdd` preset

`mdd` is included as a **working example** of a third-party relay that serves both
Claude and Codex compatible endpoints from one host. It is not required, endorsed,
or affiliated with this tool — remove it from `PRESETS` in `ccpkit.py` if you do not
want it shipped.

- Sign-up / key portal: <https://api.mddtech.cn/portal>
- Claude line: `ANTHROPIC_BASE_URL=https://api.mddtech.cn/p`
- Codex line: `OPENAI_BASE_URL=https://api.mddtech.cn/p/v1`

## Adding your own provider

Either use flags directly:

```bash
ccpkit add myrelay \
  --claude-base https://api.example.com/p    --claude-key sk-xxx \
  --codex-base  https://api.example.com/p/v1 --codex-key  sk-yyy
```

Or send a pull request adding an entry to `PRESETS` in `ccpkit.py`:

```python
"myrelay": {
    "label": "My Relay",
    "claude_base": "https://api.example.com/p",
    "claude_model": "claude-sonnet-4-6",
    "codex_base": "https://api.example.com/p/v1",
    "codex_model": "gpt-5.5",
    "homepage": "https://example.com/signup",
},
```

Requirements for a preset PR:

1. The provider must offer an Anthropic-compatible and/or OpenAI-compatible endpoint.
2. `homepage` must be a **key sign-up page**, not an ad or affiliate link.
3. No keys, tokens, or referral codes inside the preset.
