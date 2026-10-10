#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ccpkit - Claude Code / Codex CLI provider profile switcher

Manage multiple API providers (official, relay/mirror, self-hosted) and switch
the config of Claude Code and Codex CLI with one command.

Zero dependencies. Python 3.8+.

Commands:
  add       add or update a profile
  use       apply a profile to Claude Code / Codex CLI
  list      list profiles
  show      show profile details and the config that would be written
  env       print environment variables (bash / powershell / cmd)
  doctor    check connectivity, latency and available models
  remove    delete a profile
  restore   restore the latest backup of a client config

Config:  $CCPKIT_HOME/profiles.json   (default ~/.ccpkit/profiles.json)
Targets: $CCPKIT_CLAUDE_DIR/.claude/settings.json
         $CCPKIT_CODEX_DIR/.codex/config.toml + auth.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "0.3.0"
MANAGED_BEGIN = "# >>> ccpkit begin"
MANAGED_END = "# <<< ccpkit end"

# --- console encoding: never crash on legacy Windows code pages (cp936 etc.) ---
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
_ENC = (getattr(sys.stdout, "encoding", "") or "").lower()
UNICODE_OK = ("utf" in _ENC)
OK_MARK, WARN_MARK, ERR_MARK = ("✓ ", "! ", "✗ ") if UNICODE_OK else ("[OK] ", "[!] ", "[x] ")

# ---------------------------------------------------------------- presets
# Presets only fill base_url / default model. The API key is always yours to provide.
PRESETS = {
    "anthropic": {
        "label": "Anthropic official",
        "claude_base": "https://api.anthropic.com",
        "homepage": "https://console.anthropic.com",
    },
    "openai": {
        "label": "OpenAI official",
        "codex_base": "https://api.openai.com/v1",
        "homepage": "https://platform.openai.com",
    },
    "mdd": {
        "label": "MDD API (relay example)",
        "claude_base": "https://api.mddtech.cn/p",
        "claude_model": "claude-sonnet-4-6",
        "codex_base": "https://api.mddtech.cn/p/v1",
        "codex_model": "gpt-5.5",
        "homepage": "https://api.mddtech.cn/portal",
    },
}
PRESET_ALIAS = {"mdd-relay": "mdd", "mdd-switch": "mdd", "mddswitch": "mdd"}


def resolve_preset(name: str):
    if not name:
        return None, None
    key = PRESET_ALIAS.get(name.strip().lower(), name.strip().lower())
    return key, PRESETS.get(key)

# ---------------------------------------------------------------- utils

def _home() -> Path:
    override = os.environ.get("CCPKIT_HOME_HOME")  # unused, kept for clarity
    return Path(os.path.expanduser("~"))


def config_dir() -> Path:
    d = os.environ.get("CCPKIT_HOME")
    return Path(d) if d else Path.home() / ".ccpkit"


def profiles_path() -> Path:
    return config_dir() / "profiles.json"


def claude_dir() -> Path:
    d = os.environ.get("CCPKIT_CLAUDE_DIR")
    return (Path(d) / ".claude") if d else Path.home() / ".claude"


def codex_dir() -> Path:
    d = os.environ.get("CCPKIT_CODEX_DIR")
    return (Path(d) / ".codex") if d else Path.home() / ".codex"


def use_color() -> bool:
    if os.environ.get("NO_COLOR") or "--no-color" in sys.argv:
        return False
    if os.name == "nt":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
                    or os.environ.get("TERM_PROGRAM") or os.environ.get("ConEmuANSI"))
    return sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color() else text


def ok(msg): print(c(OK_MARK, "32") + msg)
def warn(msg): print(c(WARN_MARK, "33") + msg)
def err(msg): print(c(ERR_MARK, "31") + msg, file=sys.stderr)
def dim(msg): return c(msg, "90")
def bold(msg): return c(msg, "1")


def load_db() -> dict:
    p = profiles_path()
    if not p.exists():
        return {"version": 1, "current": {}, "profiles": {}}
    try:
        db = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        err(f"配置文件损坏: {p} ({e})")
        sys.exit(2)
    db.setdefault("version", 1)
    db.setdefault("current", {})
    db.setdefault("profiles", {})
    return db


def save_db(db: dict) -> None:
    p = profiles_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(path.name + f".ccpkit-bak-{ts}")
    shutil.copy2(path, dst)
    return dst


def mask(secret: str) -> str:
    if not secret:
        return dim("(未设置)")
    if len(secret) <= 12:
        return secret[:3] + "***"
    return f"{secret[:8]}…{secret[-4:]}"


# ---------------------------------------------------------------- clients

def write_claude(prof: dict) -> list[str]:
    """Merge env into ~/.claude/settings.json, keep other keys untouched."""
    base, key, model = prof.get("claude_base"), prof.get("claude_key"), prof.get("claude_model")
    if not (base and key):
        return []
    d = claude_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "settings.json"
    data = {}
    if f.exists():
        b = backup(f)
        try:
            data = json.loads(f.read_text(encoding="utf-8") or "{}")
        except Exception:
            warn(f"原 settings.json 解析失败,已备份为 {b.name if b else '(无)'},按新文件写入")
            data = {}
    env = data.get("env") or {}
    env["ANTHROPIC_BASE_URL"] = base
    env["ANTHROPIC_AUTH_TOKEN"] = key
    if model:
        env["ANTHROPIC_MODEL"] = model
    data["env"] = env
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out = [str(f)]
    return out


def _strip_managed(text: str) -> str:
    lines, keep, skip = text.splitlines(), [], False
    for ln in lines:
        if ln.strip() == MANAGED_BEGIN:
            skip = True
            continue
        if ln.strip() == MANAGED_END:
            skip = False
            continue
        if not skip:
            keep.append(ln)
    return "\n".join(keep).strip()


def write_codex(prof: dict) -> list[str]:
    base, key, model = prof.get("codex_base"), prof.get("codex_key"), prof.get("codex_model")
    if not (base and key):
        return []
    d = codex_dir()
    d.mkdir(parents=True, exist_ok=True)
    cfg = d / "config.toml"
    existing = ""
    if cfg.exists():
        backup(cfg)
        existing = _strip_managed(cfg.read_text(encoding="utf-8", errors="replace"))
    block = [
        MANAGED_BEGIN,
        f'model = "{model or "gpt-5.5"}"',
        'model_provider = "ccpkit"',
        "",
        "[model_providers.ccpkit]",
        'name = "ccpkit"',
        f'base_url = "{base.rstrip("/")}"',
        'env_key = "OPENAI_API_KEY"',
        'wire_api = "responses"',
        MANAGED_END,
    ]
    cfg.write_text("\n".join(block) + ("\n\n" + existing if existing else "") + "\n", encoding="utf-8")
    # auth.json so the CLI can pick the key up without exporting env vars
    auth = d / "auth.json"
    if auth.exists():
        backup(auth)
        try:
            ad = json.loads(auth.read_text(encoding="utf-8") or "{}")
        except Exception:
            ad = {}
    else:
        ad = {}
    ad["OPENAI_API_KEY"] = key
    auth.write_text(json.dumps(ad, ensure_ascii=False, indent=2), encoding="utf-8")
    return [str(cfg), str(auth)]


# ---------------------------------------------------------------- network

def http_get(url: str, key: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "Accept": "application/json",
        "User-Agent": f"ccpkit/{VERSION}",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body, (time.time() - t0) * 1000, None
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400], (time.time() - t0) * 1000, None
    except Exception as e:
        return None, b"", (time.time() - t0) * 1000, str(e)


def fetch_models(base: str, key: str):
    """Probe the model list. Handles bases that already end with /v1."""
    base = base.rstrip("/")
    if base.endswith("/v1"):
        urls = [f"{base}/models"]
    else:
        urls = [f"{base}/v1/models", f"{base}/models"]
    last = None
    for url in urls:
        status, body, ms, exc = http_get(url, key)
        if exc:
            return {"url": url, "status": None, "ms": ms, "error": exc, "models": []}
        models = parse_models(body)
        last = {"url": url, "status": status, "ms": ms, "error": None, "models": models}
        if status == 200:
            return last
    return last or {"url": urls[0], "status": None, "ms": 0, "error": "no endpoint", "models": []}


def parse_models(body: bytes):
    """Accept several shapes: {"data":[{"id":..}]}, {"models":[...]}, plain list."""
    try:
        data = json.loads(body.decode("utf-8", "ignore"))
    except Exception:
        return []
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for k in ("data", "models", "model_list"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    out = []
    for it in items or []:
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
        elif isinstance(it, dict):
            for k in ("id", "name", "model", "display_name"):
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
    # 去重保序
    seen, uniq = set(), []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


# ---------------------------------------------------------------- bench

DEFAULT_BENCH_PROMPT = "Reply with exactly: OK"


def _claude_messages_url(base: str) -> str:
    b = base.rstrip("/")
    return f"{b}/messages" if b.endswith("/v1") else f"{b}/v1/messages"


def _codex_responses_url(base: str) -> str:
    b = base.rstrip("/")
    return f"{b}/responses" if b.endswith("/v1") else f"{b}/v1/responses"


def _sse_post(url: str, key: str, payload: dict, timeout: float = 120.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "User-Agent": f"ccpkit/{VERSION}",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _bench_once(kind: str, base: str, key: str, model: str, prompt: str,
                stream: bool, timeout: float) -> dict:
    """One measurement. Returns {ttft, total, out, ok, err, url}."""
    if kind == "claude":
        url = _claude_messages_url(base)
        payload = {
            "model": model or "claude-sonnet-4-6",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        url = _codex_responses_url(base)
        payload = {
            "model": model or "gpt-5.5",
            "input": prompt,
            "max_output_tokens": 64,
        }
    if stream:
        payload["stream"] = True
    t0 = time.time()
    ttft = None
    out_tokens = 0
    err = None
    try:
        resp = _sse_post(url, key, payload, timeout)
        if stream:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    if body == "[DONE]":
                        break
                    continue
                try:
                    ev = json.loads(body)
                except Exception:
                    continue
                if isinstance(ev, dict):
                    if ev.get("error"):
                        err = str(ev["error"])[:200]
                        break
                    if kind == "claude":
                        if ttft is None and ev.get("type") == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("text"):
                                ttft = time.time() - t0
                        usage = ev.get("usage") or (ev.get("message") or {}).get("usage")
                        if isinstance(usage, dict) and usage.get("output_tokens"):
                            out_tokens = usage["output_tokens"]
                        if ev.get("type") == "message_stop":
                            break
                    else:
                        if ttft is None and ev.get("type") in (
                                "response.output_text.delta", "response.output_text.done"):
                            if ev.get("delta") or ev.get("type", "").endswith(".done"):
                                ttft = time.time() - t0
                        if ev.get("type") == "response.completed":
                            usage = ((ev.get("response") or {}).get("usage") or {})
                            out_tokens = usage.get("output_tokens") or out_tokens
                            break
            if ttft is None and err is None:
                ttft = time.time() - t0
        else:
            body = json.loads(resp.read().decode("utf-8", "ignore"))
            ttft = time.time() - t0
            usage = body.get("usage") or ((body.get("response") or {}).get("usage")) or {}
            out_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "ignore")
        except Exception:
            detail = ""
        err = f"HTTP {e.code} {detail}"
    except Exception as e:
        err = str(e)[:200]
    total = time.time() - t0
    return {"ttft": ttft, "total": total, "out": out_tokens, "ok": err is None,
            "err": err, "url": url}


def cmd_bench(a):
    db = load_db()
    p = db["profiles"].get(a.name)
    if not p:
        err(f"没有名为 {a.name} 的配置")
        sys.exit(2)
    lines = []
    if p.get("claude_key") and a.client in ("both", "claude"):
        lines.append(("Claude", p.get("claude_base"), p.get("claude_key"), p.get("claude_model")))
    if p.get("codex_key") and a.client in ("both", "codex"):
        lines.append(("Codex", p.get("codex_base"), p.get("codex_key"), p.get("codex_model")))
    if not lines:
        err("该配置没有可测的线路")
        sys.exit(2)
    if not a.quiet:
        warn(f"将发起 {len(lines) * a.runs} 次真实请求(会产生少量用量),prompt: {a.prompt!r}")
    report = {}
    for kind, base, key, model in lines:
        rows = []
        for i in range(a.runs):
            r = _bench_once(kind.lower(), base, key, model, a.prompt,
                            not a.no_stream, a.timeout)
            rows.append(r)
            if not a.json:
                if r["ok"]:
                    ttft = f"{r['ttft'] * 1000:.0f} ms" if r["ttft"] is not None else "-"
                    gen = r["total"] - (r["ttft"] or 0)
                    tps = (r["out"] / gen) if (r["out"] >= 2 and gen > 0.2) else None
                    tps_s = f"{tps:.1f} tok/s" if tps else "-"
                    print(f"  {kind} #{i + 1}  首字 {ttft:<10} 总耗时 {r['total']:.2f}s   "
                          f"输出 {r['out']} tok   {tps_s}")
                else:
                    print(f"  {kind} #{i + 1}  {c('失败', '31')}  {r['err']}")
        ok_rows = [r for r in rows if r["ok"]]
        report[kind] = {
            "url": rows[0]["url"] if rows else "",
            "runs": len(rows),
            "ok": len(ok_rows),
            "ttft_ms_avg": round(sum(r["ttft"] for r in ok_rows) / len(ok_rows) * 1000, 1) if ok_rows else None,
            "ttft_ms_min": round(min(r["ttft"] for r in ok_rows) * 1000, 1) if ok_rows else None,
            "total_s_avg": round(sum(r["total"] for r in ok_rows) / len(ok_rows), 2) if ok_rows else None,
            "out_tokens_avg": round(sum(r["out"] for r in ok_rows) / len(ok_rows), 1) if ok_rows else None,
            "errors": [r["err"] for r in rows if not r["ok"]],
        }
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print()
        print(bold(f"{'线路':<8}{'成功率':<10}{'首字均值':<12}{'首字最快':<12}{'总耗时均值':<12}输出tok"))
        print(dim("-" * 68))
        for kind, r in report.items():
            rate = f"{r['ok']}/{r['runs']}"
            t_avg = f"{r['ttft_ms_avg']:.0f} ms" if r["ttft_ms_avg"] is not None else "-"
            t_min = f"{r['ttft_ms_min']:.0f} ms" if r["ttft_ms_min"] is not None else "-"
            tot = f"{r['total_s_avg']:.2f} s" if r["total_s_avg"] is not None else "-"
            print(f"{kind:<8}{rate:<10}{t_avg:<12}{t_min:<12}{tot:<12}{r['out_tokens_avg'] or 0:g}")
    return 0 if all(r["ok"] == r["runs"] for r in report.values()) else 1


# ---------------------------------------------------------------- commands

def cmd_add(a):
    db = load_db()
    pname, preset = resolve_preset(getattr(a, "preset", None))
    if getattr(a, "preset", None) and not preset:
        err(f"未知的 preset: {a.preset}(可用: {', '.join(PRESETS)})")
        sys.exit(2)
    name = a.name or pname or "default"
    p = db["profiles"].get(name, {})
    fields = {
        "claude_base": a.claude_base, "claude_key": a.claude_key, "claude_model": a.claude_model,
        "codex_base": a.codex_base, "codex_key": a.codex_key, "codex_model": a.codex_model,
    }
    if preset:
        for k, v in preset.items():
            if k in fields and not fields.get(k):
                fields[k] = v
        print(f"使用预设 {bold(pname)} — {preset.get('label','')}")
        if preset.get("homepage"):
            print(dim(f"   该线路的 API key 请到 {preset['homepage']} 获取"))
    if not a.yes and not any(fields.values()):
        print(bold(f"添加配置 [{name}]") + dim("  (直接回车跳过某一项)"))
        if confirm("配置 Claude Code 线路?"):
            fields["claude_base"] = ask("  Claude base_url", "https://api.anthropic.com")
            fields["claude_key"] = ask("  Claude api key", "")
            fields["claude_model"] = ask("  默认模型(可空)", "")
        if confirm("配置 Codex CLI 线路?"):
            fields["codex_base"] = ask("  Codex base_url(含 /v1)", "")
            fields["codex_key"] = ask("  Codex api key", "")
            fields["codex_model"] = ask("  默认模型(可空)", "")
    for k, v in fields.items():
        if v is not None and v != "":
            p[k] = v.strip() if isinstance(v, str) else v
    p["name"] = name
    if not (p.get("claude_key") or p.get("codex_key")):
        err("至少要配置一条线路(key 不能为空)")
        sys.exit(2)
    db["profiles"][name] = p
    save_db(db)
    ok(f"已保存配置 {bold(name)} → {profiles_path()}")
    if a.use:
        apply_profile(db, name, a.client)
        save_db(db)


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def ask(prompt: str, default: str) -> str:
    try:
        v = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        return default
    return v or default


def apply_profile(db: dict, name: str, client: str = "both") -> None:
    prof = db["profiles"].get(name)
    if not prof:
        err(f"没有名为 {name} 的配置")
        sys.exit(2)
    written = []
    if client in ("both", "claude"):
        w = write_claude(prof)
        if w:
            written += [("Claude Code", x) for x in w]
        else:
            warn(_missing_hint(prof, "claude", client))
    if client in ("both", "codex"):
        w = write_codex(prof)
        if w:
            written += [("Codex CLI", x) for x in w]
        else:
            warn(_missing_hint(prof, "codex", client))
    db["current"] = db.get("current") or {}
    if prof.get("claude_key"):
        db["current"]["claude"] = name
    if prof.get("codex_key"):
        db["current"]["codex"] = name
    if written:
        ok(f"已切换到 {bold(name)}")
        for who, path in written:
            print(f"   {dim(who)} → {path}")
    else:
        warn("没有写入任何配置")


def _missing_hint(prof: dict, kind: str, client: str) -> str:
    base, key = prof.get(f"{kind}_base"), prof.get(f"{kind}_key")
    who = "Claude Code" if kind == "claude" else "Codex CLI"
    if not key and not base:
        return f"该配置没有 {kind} 线路,跳过 {who}"
    if not base:
        return (f"{who} 缺少 {kind}_base,未写入。请补上:"
                f"ccpkit add {prof.get('name', '<名字>')} --{kind}-base <地址>")
    return f"{who} 缺少 {kind}_key,未写入,跳过"


def cmd_use(a):
    db = load_db()
    apply_profile(db, a.name, a.client)
    save_db(db)
    if a.client in ("both", "claude") and db["profiles"][a.name].get("claude_key"):
        print(dim("   提示:Claude Code 重启后生效;Codex CLI 若已在运行,请重开终端"))
    if a.client in ("both", "codex") and db["profiles"][a.name].get("codex_key"):
        print(dim("   提示:Codex CLI 也支持环境变量 OPENAI_API_KEY(见 ccpkit env)"))


def cmd_list(a):
    db = load_db()
    profs = db["profiles"]
    if not profs:
        print("还没有配置。用 " + bold("ccpkit add <名字>") + " 添加,例如:")
        print(dim('   ccpkit add myrelay --claude-base https://example.com/p --claude-key sk-xxx '
                  '--codex-base https://example.com/p/v1 --codex-key sk-yyy'))
        return
    cur = db.get("current", {})
    tick = "✓" if UNICODE_OK else "Y"
    dash = " -"
    print(bold(f"{'名称':<16}{'Claude':<10}{'Codex':<10}模型"))
    print(dim("-" * 64))
    for name, p in profs.items():
        cm = "* " if cur.get("claude") == name else "  "
        xm = "* " if cur.get("codex") == name else "  "
        cl = (cm + tick) if p.get("claude_key") else "  " + dash
        cx = (xm + tick) if p.get("codex_key") else "  " + dash
        model = p.get("claude_model") or p.get("codex_model") or ""
        print(f"{name:<16}{cl:<10}{cx:<10}{model}")
    print(dim("\n* = 当前生效    Y = 已配置该线路"))


def cmd_show(a):
    db = load_db()
    p = db["profiles"].get(a.name)
    if not p:
        err(f"没有名为 {a.name} 的配置")
        sys.exit(2)
    print(bold(a.name))
    for k in ("claude_base", "claude_key", "claude_model", "codex_base", "codex_key", "codex_model", "notes"):
        if p.get(k):
            v = mask(p[k]) if k.endswith("_key") else p[k]
            print(f"  {k:<14} {v}")
    print(dim("\n会被写入的文件:"))
    if p.get("claude_key"):
        print(f"  {claude_dir() / 'settings.json'}")
    if p.get("codex_key"):
        print(f"  {codex_dir() / 'config.toml'}")
        print(f"  {codex_dir() / 'auth.json'}")


def cmd_env(a):
    db = load_db()
    p = db["profiles"].get(a.name)
    if not p:
        err(f"没有名为 {a.name} 的配置")
        sys.exit(2)
    shell = a.shell
    if shell == "auto":
        shell = "powershell" if os.name == "nt" else "bash"
    lines = []
    if p.get("claude_key"):
        if shell == "powershell":
            lines += [f'$env:ANTHROPIC_BASE_URL="{p["claude_base"]}"',
                      f'$env:ANTHROPIC_AUTH_TOKEN="{p["claude_key"]}"']
            if p.get("claude_model"):
                lines.append(f'$env:ANTHROPIC_MODEL="{p["claude_model"]}"')
        elif shell == "cmd":
            lines += [f'set ANTHROPIC_BASE_URL={p["claude_base"]}',
                      f'set ANTHROPIC_AUTH_TOKEN={p["claude_key"]}']
            if p.get("claude_model"):
                lines.append(f'set ANTHROPIC_MODEL={p["claude_model"]}')
        else:
            lines += [f'export ANTHROPIC_BASE_URL={p["claude_base"]}',
                      f'export ANTHROPIC_AUTH_TOKEN={p["claude_key"]}']
            if p.get("claude_model"):
                lines.append(f'export ANTHROPIC_MODEL={p["claude_model"]}')
    if p.get("codex_key"):
        if shell == "powershell":
            lines += [f'$env:OPENAI_BASE_URL="{p["codex_base"]}"',
                      f'$env:OPENAI_API_KEY="{p["codex_key"]}"']
        elif shell == "cmd":
            lines += [f'set OPENAI_BASE_URL={p["codex_base"]}',
                      f'set OPENAI_API_KEY={p["codex_key"]}']
        else:
            lines += [f'export OPENAI_BASE_URL={p["codex_base"]}',
                      f'export OPENAI_API_KEY={p["codex_key"]}']
    print("\n".join(lines))


def cmd_presets(a):
    print(bold("内置预设(只预填地址和默认模型,key 需要你自己提供)\n"))
    for key, p in PRESETS.items():
        print(f"  {bold(key):<20}{p.get('label','')}")
        if p.get("claude_base"):
            print(f"      Claude: {p['claude_base']}")
        if p.get("codex_base"):
            print(f"      Codex : {p['codex_base']}")
        if p.get("homepage"):
            print(dim(f"      key   : {p['homepage']}"))
    if PRESET_ALIAS:
        print(dim("\n别名: " + ", ".join(f"{k}→{v}" for k, v in PRESET_ALIAS.items())))
    print(dim("\n用法: ccpkit add <名字> --preset <预设> --claude-key sk-xxx --codex-key sk-yyy --use"))


def cmd_doctor(a):
    db = load_db()
    names = [a.name] if a.name else list(db["profiles"].keys())
    if not names:
        err("没有可检查的配置")
        sys.exit(2)
    all_ok = True
    for name in names:
        p = db["profiles"].get(name)
        if not p:
            err(f"没有名为 {name} 的配置")
            all_ok = False
            continue
        print(bold(f"\n[{name}]"))
        for kind, base, key, model in (
            ("Claude", p.get("claude_base"), p.get("claude_key"), p.get("claude_model")),
            ("Codex", p.get("codex_base"), p.get("codex_key"), p.get("codex_model")),
        ):
            if not (base and key):
                print(f"  {kind:<7}{dim('未配置,跳过')}")
                continue
            r = fetch_models(base, key)
            if r["status"] == 200:
                ms = f"{r['ms']:.0f} ms"
                n = len(r["models"])
                if n == 0:
                    print(f"  {kind:<7}{c('OK', '32')}  延迟 {ms:<9} {dim('(接口可用,未返回模型清单)')}")
                else:
                    extra = ""
                    if model:
                        extra = (c(" 模型可用", "32") if model in r["models"]
                                 else c(f" 但不含 {model}", "33"))
                    print(f"  {kind:<7}{c('OK', '32')}  延迟 {ms:<9} 模型 {n} 个{extra}")
                if a.models and r["models"]:
                    for m in r["models"][:40]:
                        print(f"          - {m}")
            elif r["status"] is None:
                print(f"  {kind:<7}{c('连接失败', '31')}  {r['error']}")
                all_ok = False
            else:
                print(f"  {kind:<7}{c('HTTP ' + str(r['status']), '31')}  {r['url']}")
                all_ok = False
    print()
    print(c("全部正常", "32") if all_ok else c("存在异常,见上面输出", "31"))
    sys.exit(0 if all_ok else 1)


def cmd_remove(a):
    db = load_db()
    if a.name not in db["profiles"]:
        err(f"没有名为 {a.name} 的配置")
        sys.exit(2)
    if not a.yes and not confirm(f"确认删除配置 {a.name}?"):
        return
    db["profiles"].pop(a.name, None)
    for k, v in list(db.get("current", {}).items()):
        if v == a.name:
            db["current"].pop(k, None)
    save_db(db)
    ok(f"已删除 {a.name}")


def cmd_restore(a):
    target = claude_dir() / "settings.json" if a.client == "claude" else codex_dir() / "config.toml"
    cands = sorted(target.parent.glob(target.name + ".ccpkit-bak-*"))
    if a.list:
        if not cands:
            print("没有备份")
        for f in cands:
            print(f)
        return
    if not cands:
        err("没有找到备份")
        sys.exit(2)
    latest = cands[-1]
    shutil.copy2(latest, target)
    ok(f"已从 {latest.name} 恢复 {target}")


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- web UI

UI_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ccpkit · 配置管理</title>
<style>
:root{--bg:#0f1115;--card:#171b23;--card2:#1d222c;--line:#2a3040;--fg:#e6e9f0;--dim:#8b93a7;
      --acc:#4f8cff;--ok:#3ecf8e;--warn:#e8a66a;--bad:#f2555a;--mint:#6ae0b0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--acc)}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;
       padding:15px 26px;border-bottom:1px solid var(--line);background:var(--bg);
       position:sticky;top:0;z-index:9}
.brand{font-size:19px;font-weight:700;letter-spacing:.2px}
.brand em{font-style:normal;font-weight:400;font-size:13px;color:var(--dim);margin-left:9px}
.cur{font-size:13px;color:var(--dim)}
.cur b{color:var(--mint);font-weight:600}
main{max-width:920px;margin:0 auto;padding:24px 20px 80px}
h2{font-size:13px;font-weight:600;color:var(--dim);margin:26px 0 12px;letter-spacing:.6px}
h2:first-child{margin-top:0}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:13px;
       padding:16px 18px;margin-bottom:11px}
.pcard.on{border-color:#2f5a3f;background:linear-gradient(180deg,#16241d,var(--card))}
.prow{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.pname{font-size:16px;font-weight:600;display:flex;align-items:center;gap:9px}
.dot{width:8px;height:8px;border-radius:50%;background:#3a4152;flex:0 0 auto}
.pcard.on .dot{background:var(--ok);box-shadow:0 0 0 4px rgba(62,207,142,.14)}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.line{display:flex;gap:10px;font-size:12.5px;color:var(--dim);margin-top:9px;flex-wrap:wrap}
.line .k{flex:0 0 52px;color:#6d7488}
.line .v{color:#b9c0d0;word-break:break-all}
.line .mono{font-family:ui-monospace,Consolas,monospace}
.btns{display:flex;gap:7px;flex-wrap:wrap}
button{font:inherit;font-size:12.5px;padding:6px 13px;border-radius:9px;cursor:pointer;
       border:1px solid var(--line);background:var(--card2);color:var(--fg);transition:.13s}
button:hover{border-color:#3d465c;background:#252b37}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.pri:hover{background:#3f7cf0}
button.gh{background:transparent}
button.dg{color:var(--bad);border-color:#3a2529}
button.dg:hover{background:#2a1a1d;border-color:#5a2b31}
button:disabled{opacity:.45;cursor:not-allowed}
.modal{position:fixed;inset:0;background:rgba(5,7,10,.72);display:none;z-index:50;
       align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto}
.modal.show{display:flex}
.sheet{background:var(--card);border:1px solid var(--line);border-radius:15px;
       width:100%;max-width:660px;padding:22px 24px}
.sheet h3{margin:0 0 4px;font-size:17px}
.sheet p.sub{margin:0 0 18px;font-size:12.5px;color:var(--dim)}
fieldset{border:1px solid var(--line);border-radius:11px;padding:13px 15px 15px;margin:0 0 14px}
legend{font-size:12px;color:var(--dim);padding:0 7px}
label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
label:first-child{margin-top:0}
input,select{width:100%;font:inherit;font-size:13px;padding:8px 11px;border-radius:8px;
       border:1px solid var(--line);background:#12161d;color:var(--fg);outline:none}
input:focus,select:focus{border-color:var(--acc)}
.hint{font-size:11.5px;color:#6d7488;margin-top:4px}
pre{background:#0b0e13;border:1px solid var(--line);border-radius:10px;padding:12px 14px;
    font:12px/1.6 ui-monospace,Consolas,monospace;overflow:auto;max-height:320px;
    white-space:pre-wrap;word-break:break-all;margin:9px 0 0;color:#c5cbd8}
.bar{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.res{font-size:12.5px;margin-top:9px;padding:9px 12px;border-radius:9px;background:#12161d;
     border:1px solid var(--line);white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.toast{position:fixed;right:20px;bottom:20px;background:var(--card2);border:1px solid var(--line);
       border-left:3px solid var(--acc);border-radius:10px;padding:11px 16px;font-size:13px;
       opacity:0;transform:translateY(8px);transition:.2s;pointer-events:none;max-width:420px;z-index:99}
.toast.show{opacity:1;transform:none}
.toast.good{border-left-color:var(--ok)}
.toast.bad{border-left-color:var(--bad)}
.foot{margin-top:26px;font-size:11.5px;color:#5d6478;line-height:1.9}
.foot code{background:#12161d;padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
</style></head>
<body>
<header>
  <div class="brand">ccpkit<em>配置管理</em></div>
  <div class="cur">当前生效 <b id="curName">—</b></div>
</header>
<main>
  <h2>所有配置</h2>
  <div id="list"></div>
  <div style="margin-top:14px"><button class="pri" onclick="openEditor('')">+ 新增配置</button></div>

  <h2>客户端配置文件</h2>
  <div class="pcard">
    <div class="btns">
      <button onclick="showFile('claude')">查看 Claude Code 配置</button>
      <button onclick="showFile('codex')">查看 Codex CLI 配置</button>
      <button class="gh" onclick="doRestore('claude')">恢复 Claude 备份</button>
      <button class="gh" onclick="doRestore('codex')">恢复 Codex 备份</button>
    </div>
    <pre id="fileBox" style="display:none"></pre>
  </div>

  <div class="foot">
    切换后 <b>需要重开终端 / 重启客户端</b> —— 它们只在启动时读一次配置。<br>
    所有 key 只存在本地 <code id="pProfiles"></code>,只有当前生效的那把会被写进客户端配置。
  </div>
</main>

<div class="modal" id="editor"><div class="sheet">
  <h3 id="edTitle">新增配置</h3>
  <p class="sub">一个配置 = 一组 Claude 和/或 Codex 的地址 + key。两边都可以只填一边。</p>
  <label>配置名</label>
  <input id="f_name" placeholder="例如:麦多 / 官方 / 自建">
  <label>从预设填入(可选)</label>
  <select id="f_preset" onchange="applyPreset()"><option value="">— 不选 —</option></select>

  <fieldset><legend>Claude Code</legend>
    <label>接口地址 ANTHROPIC_BASE_URL</label>
    <input id="f_cbase" placeholder="https://api.example.com/p">
    <label>Key ANTHROPIC_AUTH_TOKEN</label>
    <input id="f_ckey" placeholder="留空 = 不改动已有的 key">
    <label>默认模型 ANTHROPIC_MODEL(可选)</label>
    <input id="f_cmodel" placeholder="claude-sonnet-5">
  </fieldset>

  <fieldset><legend>Codex CLI</legend>
    <label>接口地址 OPENAI_BASE_URL</label>
    <input id="f_obase" placeholder="https://api.example.com/p/v1">
    <label>Key OPENAI_API_KEY</label>
    <input id="f_okey" placeholder="留空 = 不改动已有的 key">
    <label>默认模型(可选)</label>
    <input id="f_omodel" placeholder="gpt-5.5">
  </fieldset>

  <div class="bar">
    <button class="gh" onclick="closeEditor()">取消</button>
    <button class="pri" onclick="saveProfile()">保存</button>
  </div>
</div></div>

<div class="toast" id="toast"></div>

<script>
const T = new URLSearchParams(location.search).get('t') || '';
let ST = null, ORIG = '';

function toast(msg, kind){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast', 2600);
}
function esc(s){ return (s == null ? '' : String(s)).replace(/[&<>"]/g,
  m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }
function mask(base, tail){
  if (!base) return '<span class="warn">未配置</span>';
  return esc(base) + (tail ? ' <span class="mono">…' + esc(tail) + '</span>' : ' <span class="warn">无 key</span>');
}

async function api(path, body){
  const opt = { method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json'} };
  if (body) opt.body = JSON.stringify(body);
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path + sep + 't=' + encodeURIComponent(T), opt);
  return await r.json().catch(() => ({ok:false, error:'返回不是 JSON'}));
}

async function load(){
  const r = await api('/api/state');
  if (!r.ok){ toast('读取失败:' + (r.error || ''), 'bad'); return; }
  ST = r.state;
  render();
}

function render(){
  const cur = ST.current || {};
  document.getElementById('curName').textContent =
    cur.claude || cur.codex ? (cur.claude || cur.codex) : '(未切换过)';
  document.getElementById('pProfiles').textContent = ST.paths.profiles;

  const box = document.getElementById('list');
  if (!ST.profiles.length){
    box.innerHTML = '<div class="pcard" style="color:var(--dim)">还没有配置,点下面的「新增配置」。</div>';
    return;
  }
  box.innerHTML = ST.profiles.map(p => {
    const on = (cur.claude === p.name) || (cur.codex === p.name);
    const which = [cur.claude === p.name ? 'Claude' : '', cur.codex === p.name ? 'Codex' : '']
      .filter(Boolean).join(' + ');
    return '<div class="pcard' + (on ? ' on' : '') + '">'
      + '<div class="prow"><div class="pname"><span class="dot"></span>' + esc(p.name)
      + (on ? ' <span class="tag ok">当前' + (which ? ' · ' + which : '') + '</span>' : '') + '</div>'
      + '<div class="btns">'
      + '<button class="pri" onclick="doUse(\'' + esc(p.name) + '\',\'both\')">切换</button>'
      + '<button onclick="doDoctor(\'' + esc(p.name) + '\')">自检</button>'
      + '<button onclick="openEditor(\'' + esc(p.name) + '\')">编辑</button>'
      + '<button class="dg" onclick="doDelete(\'' + esc(p.name) + '\')">删除</button>'
      + '</div></div>'
      + '<div class="line"><span class="k">Claude</span><span class="v">'
      + mask(p.claude_base, p.claude_key_tail) + (p.claude_model ? ' · ' + esc(p.claude_model) : '') + '</span></div>'
      + '<div class="line"><span class="k">Codex</span><span class="v">'
      + mask(p.codex_base, p.codex_key_tail) + (p.codex_model ? ' · ' + esc(p.codex_model) : '') + '</span></div>'
      + '<div id="res_' + esc(p.name) + '"></div>'
      + '</div>';
  }).join('');

  const sel = document.getElementById('f_preset');
  if (sel.options.length <= 1){
    ST.presets.forEach(n => {
      const o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o);
    });
  }
}

async function doUse(name, client){
  const r = await api('/api/use', {name: name, client: client});
  if (!r.ok){ toast('切换失败:' + (r.error || ''), 'bad'); return; }
  ST = r.state; render();
  toast('已切换到 ' + name + ' —— 记得重开终端', 'good');
}
async function doDelete(name){
  if (!confirm('删除配置「' + name + '」?\n(不会动客户的配置文件,只删这条记录)')) return;
  const r = await api('/api/delete', {name: name});
  if (!r.ok){ toast('删除失败:' + (r.error || ''), 'bad'); return; }
  ST = r.state; render(); toast('已删除 ' + name);
}
async function doDoctor(name){
  const el = document.getElementById('res_' + name);
  el.innerHTML = '<div class="res">检查中…(最多十几秒)</div>';
  const r = await api('/api/doctor', {name: name});
  if (!r.ok){ el.innerHTML = '<div class="res bad">' + esc(r.error || '失败') + '</div>'; return; }
  el.innerHTML = '<div class="res">' + r.lines.map(l =>
      '<span class="' + l.cls + '">' + esc(l.text) + '</span>').join('\n') + '</div>';
}
async function showFile(which){
  const r = await api('/api/file?which=' + which);
  const b = document.getElementById('fileBox');
  b.style.display = 'block';
  b.textContent = r.ok ? (r.path + '\n\n' + r.text) : ('读取失败:' + (r.error || ''));
}
async function doRestore(client){
  if (!confirm('把 ' + client + ' 的配置恢复到最近一次备份?')) return;
  const r = await api('/api/restore', {client: client});
  toast(r.ok ? (r.message || '已恢复') : ('恢复失败:' + (r.error || '')), r.ok ? 'good' : 'bad');
}

function openEditor(name){
  ORIG = name;
  const p = name ? ST.profiles.find(x => x.name === name) : null;
  document.getElementById('edTitle').textContent = name ? ('编辑「' + name + '」') : '新增配置';
  document.getElementById('f_name').value = name || '';
  document.getElementById('f_preset').value = '';
  document.getElementById('f_cbase').value = p ? (p.claude_base || '') : '';
  document.getElementById('f_cmodel').value = p ? (p.claude_model || '') : '';
  document.getElementById('f_obase').value = p ? (p.codex_base || '') : '';
  document.getElementById('f_omodel').value = p ? (p.codex_model || '') : '';
  document.getElementById('f_ckey').value = '';
  document.getElementById('f_okey').value = '';
  document.getElementById('f_ckey').placeholder = p && p.claude_key_set
    ? '留空 = 保持现在的 key(…' + p.claude_key_tail + ')' : 'sk-…';
  document.getElementById('f_okey').placeholder = p && p.codex_key_set
    ? '留空 = 保持现在的 key(…' + p.codex_key_tail + ')' : 'sk-…';
  document.getElementById('editor').classList.add('show');
}
function closeEditor(){ document.getElementById('editor').classList.remove('show'); }
function applyPreset(){
  const v = document.getElementById('f_preset').value;
  if (!v) return;
  const nm = document.getElementById('f_name');
  if (!nm.value) nm.value = v;
  if (PRESET_MAP[v]){
    document.getElementById('f_cbase').value = PRESET_MAP[v].claude_base || '';
    document.getElementById('f_obase').value = PRESET_MAP[v].codex_base || '';
    document.getElementById('f_cmodel').value = PRESET_MAP[v].claude_model || '';
    document.getElementById('f_omodel').value = PRESET_MAP[v].codex_model || '';
  }
}
async function saveProfile(){
  const b = {
    name: document.getElementById('f_name').value.trim(),
    orig: ORIG,
    claude_base: document.getElementById('f_cbase').value.trim(),
    claude_key: document.getElementById('f_ckey').value.trim(),
    claude_model: document.getElementById('f_cmodel').value.trim(),
    codex_base: document.getElementById('f_obase').value.trim(),
    codex_key: document.getElementById('f_okey').value.trim(),
    codex_model: document.getElementById('f_omodel').value.trim()
  };
  if (!b.name){ toast('配置名不能为空', 'bad'); return; }
  const r = await api('/api/save', b);
  if (!r.ok){ toast('保存失败:' + (r.error || ''), 'bad'); return; }
  ST = r.state; render(); closeEditor(); toast('已保存 ' + b.name, 'good');
}
let PRESET_MAP = {};
document.addEventListener('DOMContentLoaded', async () => {
  await load();
  const r = await api('/api/presets');
  if (r.ok) PRESET_MAP = r.presets || {};
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEditor(); });
</script>
</body></html>
"""


def _ui_state() -> dict:
    db = load_db()
    profs = []
    for name, p in db["profiles"].items():
        profs.append({
            "name": name,
            "claude_base": p.get("claude_base", ""),
            "claude_model": p.get("claude_model", ""),
            "claude_key_set": bool(p.get("claude_key")),
            "claude_key_tail": (p.get("claude_key") or "")[-4:],
            "codex_base": p.get("codex_base", ""),
            "codex_model": p.get("codex_model", ""),
            "codex_key_set": bool(p.get("codex_key")),
            "codex_key_tail": (p.get("codex_key") or "")[-4:],
        })
    cur = db.get("current") or {}
    return {
        "profiles": profs,
        "current": {"claude": cur.get("claude", ""), "codex": cur.get("codex", "")},
        "presets": sorted(PRESETS.keys()),
        "paths": {
            "profiles": str(profiles_path()),
            "claude": str(claude_dir() / "settings.json"),
            "codex": str(codex_dir() / "config.toml"),
        },
    }


def cmd_ui(a):
    """本地可视化界面。

    只绑 127.0.0.1,并且每次启动生成一个随机 token —— 因为浏览器里任何一个
    网页都能往 127.0.0.1 发请求(CSRF / DNS rebinding),没有 token 的话
    别人一个网页就能把你的 key 换掉。
    """
    import contextlib
    import http.server
    import io as _io
    import secrets
    import socket
    import threading
    import webbrowser
    from urllib.parse import urlparse, parse_qs

    token = secrets.token_urlsafe(18)

    def free_port(start):
        for p in range(start, start + 60):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                    return p
                except OSError:
                    continue
        raise SystemExit("找不到空闲端口")

    port = a.port or free_port(8787)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ccpkit-ui"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):        # 别把访问日志打到终端
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _host_ok(self):
            h = (self.headers.get("Host") or "").split(":")[0].strip("[]")
            return h in ("127.0.0.1", "localhost", "::1")

        def _auth(self, q):
            if not self._host_ok():
                self._json({"ok": False, "error": "host not allowed"}, 403)
                return False
            if q.get("t", [""])[0] != token:
                self._json({"ok": False, "error": "token 不正确"}, 403)
                return False
            return True

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                if q.get("t", [""])[0] != token:
                    self._send(403, "token 不正确", "text/plain; charset=utf-8")
                    return
                self._send(200, UI_HTML, "text/html; charset=utf-8")
                return
            if not self._auth(q):
                return
            if u.path == "/api/state":
                self._json({"ok": True, "state": _ui_state()})
            elif u.path == "/api/presets":
                self._json({"ok": True, "presets": PRESETS})
            elif u.path == "/api/file":
                which = q.get("which", ["claude"])[0]
                f = (claude_dir() / "settings.json") if which == "claude" else (codex_dir() / "config.toml")
                if f.exists():
                    try:
                        txt = f.read_text(encoding="utf-8")
                    except Exception as e:
                        txt = "(读取失败: %s)" % e
                else:
                    txt = "(文件还不存在 —— 还没有切换过)"
                self._json({"ok": True, "path": str(f), "text": txt})
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._auth(q):
                return
            b = self._body()
            try:
                if u.path == "/api/use":
                    db = load_db()
                    buf = _io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                        apply_profile(db, b.get("name", ""), b.get("client", "both"))
                    save_db(db)
                    self._json({"ok": True, "log": buf.getvalue(), "state": _ui_state()})

                elif u.path == "/api/save":
                    db = load_db()
                    name = (b.get("name") or "").strip()
                    if not name:
                        self._json({"ok": False, "error": "配置名不能为空"}, 400)
                        return
                    old = (b.get("orig") or "").strip()
                    if old and old != name:
                        db["profiles"].pop(old, None)
                        for k in ("claude", "codex"):
                            if (db.get("current") or {}).get(k) == old:
                                db["current"][k] = name
                    prof = db["profiles"].get(name) or {}
                    prof["name"] = name
                    for side, pre in (("claude", "claude_"), ("codex", "codex_")):
                        base = (b.get(side + "_base") or "").strip()
                        key = (b.get(side + "_key") or "").strip()
                        model = (b.get(side + "_model") or "").strip()
                        if base:
                            prof[pre + "base"] = base
                        elif not old:
                            prof.pop(pre + "base", None)
                        if key:                      # 留空 = 保持原来的 key
                            prof[pre + "key"] = key
                        if model:
                            prof[pre + "model"] = model
                        else:
                            prof.pop(pre + "model", None)
                    db["profiles"][name] = prof
                    save_db(db)
                    self._json({"ok": True, "state": _ui_state()})

                elif u.path == "/api/delete":
                    db = load_db()
                    name = b.get("name", "")
                    if name not in db["profiles"]:
                        self._json({"ok": False, "error": "没有这个配置"}, 404)
                        return
                    db["profiles"].pop(name, None)
                    for k in ("claude", "codex"):
                        if (db.get("current") or {}).get(k) == name:
                            db["current"].pop(k, None)
                    save_db(db)
                    self._json({"ok": True, "state": _ui_state()})

                elif u.path == "/api/doctor":
                    db = load_db()
                    p = db["profiles"].get(b.get("name", ""))
                    if not p:
                        self._json({"ok": False, "error": "没有这个配置"}, 404)
                        return
                    lines = []
                    for kind, base, key, model in (
                        ("Claude", p.get("claude_base"), p.get("claude_key"), p.get("claude_model")),
                        ("Codex", p.get("codex_base"), p.get("codex_key"), p.get("codex_model")),
                    ):
                        if not (base and key):
                            lines.append({"cls": "warn", "text": "%-7s未配置,跳过" % kind})
                            continue
                        r = fetch_models(base, key)
                        if r["status"] == 200:
                            n = len(r["models"])
                            if n == 0:
                                lines.append({"cls": "ok", "text": "%-7sOK    延迟 %.0f ms  (接口可用,未返回模型清单)"
                                              % (kind, r["ms"])})
                            else:
                                extra = ""
                                if model:
                                    extra = ("  模型可用" if model in r["models"]
                                             else "  但不含 %s" % model)
                                lines.append({"cls": "ok", "text": "%-7sOK    延迟 %.0f ms  模型 %d 个%s"
                                              % (kind, r["ms"], n, extra)})
                        elif r["status"] is None:
                            lines.append({"cls": "bad", "text": "%-7s连接失败  %s" % (kind, r["error"])})
                        else:
                            lines.append({"cls": "bad", "text": "%-7sHTTP %s  %s" % (kind, r["status"], r["url"])})
                    self._json({"ok": True, "lines": lines})

                elif u.path == "/api/restore":
                    client = b.get("client", "claude")
                    if client == "claude":
                        target = claude_dir() / "settings.json"
                    else:
                        target = codex_dir() / "config.toml"
                    baks = sorted(target.parent.glob(target.name + ".ccpkit-bak-*"))
                    if not baks:
                        self._json({"ok": False, "error": "没有找到备份"})
                        return
                    shutil.copy2(baks[-1], target)
                    self._json({"ok": True, "message": "已恢复 %s(来源 %s)" % (target.name, baks[-1].name)})

                else:
                    self._json({"ok": False, "error": "not found"}, 404)
            except SystemExit:
                self._json({"ok": False, "error": "配置有误(缺少地址或 key)"}, 400)
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/?t=%s" % (port, token)
    print(bold("ccpkit 配置管理"))
    print("  " + url)
    print(dim("  只监听本机(127.0.0.1),关掉这个窗口就停止。Ctrl+C 退出。"))
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n" + dim("已停止"))


def build_parser():
    p = argparse.ArgumentParser(
        prog="ccpkit",
        description="Claude Code / Codex CLI 多线路配置管理(切换官方 / 中转 / 自建)",
        epilog="示例: ccpkit add relay --claude-base https://api.example.com/p --claude-key sk-xxx "
               "--codex-base https://api.example.com/p/v1 --codex-key sk-yyy --use",
    )
    p.add_argument("--version", action="version", version=f"ccpkit {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    def add_profile_args(sp):
        sp.add_argument("--claude-base"), sp.add_argument("--claude-key"), sp.add_argument("--claude-model")
        sp.add_argument("--codex-base"), sp.add_argument("--codex-key"), sp.add_argument("--codex-model")
        sp.add_argument("--notes")

    sp = sub.add_parser("add", help="新增/修改一个配置")
    sp.add_argument("name", nargs="?", help="配置名(配合 --preset 可省略)")
    add_profile_args(sp)
    sp.add_argument("--preset", help=f"内置预设: {', '.join(PRESETS)}")
    sp.add_argument("-y", "--yes", action="store_true", help="全部用命令行参数,不交互")
    sp.add_argument("--use", action="store_true", help="保存后立即切换")
    sp.add_argument("--client", choices=["both", "claude", "codex"], default="both")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("presets", help="列出内置预设线路")
    sp.set_defaults(func=cmd_presets)

    sp = sub.add_parser("ui", help="打开本地可视化界面(浏览器操作,不用记命令)")
    sp.add_argument("--port", type=int, default=0, help="指定端口(默认自动挑一个空闲的)")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("use", help="切换到某个配置")
    sp.add_argument("name")
    sp.add_argument("--client", choices=["both", "claude", "codex"], default="both")
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser("list", help="列出所有配置")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="查看配置详情")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("env", help="打印环境变量(不写文件)")
    sp.add_argument("name")
    sp.add_argument("--shell", choices=["auto", "bash", "powershell", "cmd"], default="auto")
    sp.set_defaults(func=cmd_env)

    sp = sub.add_parser("doctor", help="连通性 / 延迟 / 模型自检")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--models", action="store_true", help="列出模型清单")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("bench", help="首字延迟 / 吞吐实测(会产生少量用量)")
    sp.add_argument("name")
    sp.add_argument("--client", choices=["both", "claude", "codex"], default="both")
    sp.add_argument("--runs", type=int, default=2, help="每条线路测几次(默认 2)")
    sp.add_argument("--prompt", default=DEFAULT_BENCH_PROMPT, help="测试用的提示词")
    sp.add_argument("--no-stream", action="store_true", help="非流式(只测总耗时)")
    sp.add_argument("--timeout", type=float, default=120.0)
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("-q", "--quiet", action="store_true", help="不打印费用提醒")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("remove", help="删除配置")
    sp.add_argument("name")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("restore", help="恢复客户端配置备份")
    sp.add_argument("--client", choices=["claude", "codex"], default="claude")
    sp.add_argument("--list", action="store_true", help="只列出备份")
    sp.set_defaults(func=cmd_restore)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 打包成 exe 之后,双击启动是没有参数的 —— 那种情况下直接开可视化界面,
    # 而不是甩一屏命令行帮助出来。pip 装的命令行版本保持原样(在 SSH 里
    # 莫名其妙拉起浏览器会很讨厌)。
    if not argv and getattr(sys, "frozen", False):
        class _UIArgs:
            port = 0
            no_browser = False
        return cmd_ui(_UIArgs()) or 0

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
