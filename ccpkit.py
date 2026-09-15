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

VERSION = "0.1.0"
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


# ---------------------------------------------------------------- commands

def cmd_add(a):
    db = load_db()
    name = a.name
    p = db["profiles"].get(name, {})
    fields = {
        "claude_base": a.claude_base, "claude_key": a.claude_key, "claude_model": a.claude_model,
        "codex_base": a.codex_base, "codex_key": a.codex_key, "codex_model": a.codex_model,
    }
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
        elif client == "claude":
            warn("该配置没有 claude_base / claude_key,跳过 Claude Code")
    if client in ("both", "codex"):
        w = write_codex(prof)
        if w:
            written += [("Codex CLI", x) for x in w]
        elif client == "codex":
            warn("该配置没有 codex_base / codex_key,跳过 Codex CLI")
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
    sp.add_argument("name")
    add_profile_args(sp)
    sp.add_argument("-y", "--yes", action="store_true", help="全部用命令行参数,不交互")
    sp.add_argument("--use", action="store_true", help="保存后立即切换")
    sp.add_argument("--client", choices=["both", "claude", "codex"], default="both")
    sp.set_defaults(func=cmd_add)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
