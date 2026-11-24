#!/usr/bin/env python3
"""Small, dependency-free subscription and node filter manager for dae."""

from __future__ import annotations

import argparse
import ast
import base64
import difflib
import glob
import json
import os
import pwd
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


VERSION = "0.8.0"
MAX_SUBSCRIPTION_SIZE = 10 * 1024 * 1024
DEFAULT_GROUP = "proxy"
DEFAULT_OUTPUT = "/etc/dae/config.d/50-group.dae"
DAE_SERVICE = "dae.service"
DEFAULT_RELOAD_TIMEOUT = 20
DEFAULT_RESTART_TIMEOUT = 90


class CliError(Exception):
    pass


def real_user_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_state_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home and not os.environ.get("SUDO_USER"):
        return Path(config_home) / "dae-cli" / "state.json"
    return real_user_home() / ".config" / "dae-cli" / "state.json"


def strip_comments(text: str) -> str:
    out: list[str] = []
    quote: str | None = None
    comment = False
    escaped = False
    for char in text:
        if comment:
            if char == "\n":
                comment = False
                out.append(char)
            else:
                out.append(" ")
            continue
        if escaped:
            out.append(char)
            escaped = False
            continue
        if quote and char == "\\":
            out.append(char)
            escaped = True
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "#":
            comment = True
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def find_blocks(text: str, name: str) -> list[str]:
    clean = strip_comments(text)
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"\s*\{")
    blocks: list[str] = []
    for match in pattern.finditer(clean):
        start = match.end()
        depth = 1
        quote: str | None = None
        escaped = False
        for pos in range(start, len(clean)):
            char = clean[pos]
            if escaped:
                escaped = False
            elif quote and char == "\\":
                escaped = True
            elif quote:
                if char == quote:
                    quote = None
            elif char in "'\"":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(clean[start:pos])
                    break
    return blocks


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    return value


def config_files(entry: Path) -> list[Path]:
    entry = entry.expanduser().resolve()
    try:
        entry.stat()
    except PermissionError as exc:
        raise CliError(f"cannot read dae config {entry}; run the command with sudo") from exc
    except FileNotFoundError as exc:
        raise CliError(f"dae config not found: {entry}") from exc
    if not entry.is_file():
        raise CliError(f"dae config not found: {entry}")
    found: list[Path] = []
    seen: set[Path] = set()

    def visit(path: Path) -> None:
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        found.append(path)
        text = path.read_text(encoding="utf-8")
        for block in find_blocks(text, "include"):
            for raw in block.splitlines():
                pattern = unquote(raw.strip())
                if not pattern:
                    continue
                full_pattern = pattern if os.path.isabs(pattern) else str(entry.parent / pattern)
                for child in sorted(glob.glob(full_pattern)):
                    child_path = Path(child)
                    if child_path.is_file():
                        visit(child_path)

    visit(entry)
    return found


ITEM_RE = re.compile(
    r"^\s*(?:(?P<tag>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[\w.-]+)\s*:\s*)?"
    r"(?P<url>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*')\s*$"
)


def read_subscriptions(entry: Path) -> dict[str, dict[str, str]]:
    subscriptions: dict[str, dict[str, str]] = {}
    untagged = 0
    for path in config_files(entry):
        text = path.read_text(encoding="utf-8")
        for block in find_blocks(text, "subscription"):
            for line in block.splitlines():
                match = ITEM_RE.match(line)
                if not match:
                    continue
                tag_raw = match.group("tag")
                if tag_raw:
                    tag = unquote(tag_raw)
                else:
                    untagged += 1
                    tag = f"<untagged:{untagged}>"
                subscriptions[tag] = {
                    "url": unquote(match.group("url")),
                    "file": str(path),
                    "tagged": bool(tag_raw),
                }
    return subscriptions


def b64decode_loose(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    raw = re.sub(rb"\s+", b"", raw)
    raw += b"=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw)
    except (ValueError, base64.binascii.Error) as exc:
        raise CliError("invalid Base64 subscription data") from exc


def fetch_subscription(item: dict[str, str], entry: Path) -> bytes:
    url = item["url"]
    if url.startswith("https-file://"):
        url = "https://" + url[len("https-file://") :]
    elif url.startswith("http-file://"):
        url = "http://" + url[len("http-file://") :]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        if not parsed.netloc:
            raise CliError("dae file subscriptions must be relative, for example file://subs/nodes.txt")
        relative = Path(urllib.parse.unquote(parsed.netloc)) / urllib.parse.unquote(parsed.path).lstrip("/")
        path = entry.parent / relative
        data = path.read_bytes()
    elif parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "dae-cli/0.1 (like dae/1.0; like v2rayN/1.0)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(MAX_SUBSCRIPTION_SIZE + 1)
        except OSError as exc:
            raise CliError(f"failed to fetch subscription: {exc}") from exc
    else:
        raise CliError(f"unsupported subscription scheme: {parsed.scheme or '<empty>'}")
    if len(data) > MAX_SUBSCRIPTION_SIZE:
        raise CliError("subscription exceeds 10 MiB")
    return data.strip()


def json_name(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("ps", "remarks", "name", "tag", "server"):
            value = data.get(key)
            if value:
                return str(value)
    return fallback


def node_name(link: str, number: int) -> str:
    fallback = f"unnamed-{number}"
    if link.startswith("vmess://"):
        payload = link[len("vmess://") :].split("#", 1)[0]
        try:
            return json_name(json.loads(b64decode_loose(payload)), fallback)
        except (CliError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    if link.startswith("ssr://"):
        try:
            decoded = b64decode_loose(link[len("ssr://") :]).decode("utf-8")
            query = decoded.split("/?", 1)[1] if "/?" in decoded else ""
            remarks = urllib.parse.parse_qs(query).get("remarks", [""])[0]
            if remarks:
                return b64decode_loose(remarks).decode("utf-8")
        except (CliError, UnicodeDecodeError, IndexError):
            pass
    if "#" in link:
        name = urllib.parse.unquote(link.rsplit("#", 1)[1]).strip()
        if name:
            return name
    try:
        parsed = urllib.parse.urlsplit(link)
        return parsed.hostname or fallback
    except ValueError:
        return fallback


def parse_nodes(data: bytes) -> list[str]:
    try:
        document = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("servers"), list):
        return [json_name(server, f"unnamed-{i}") for i, server in enumerate(document["servers"], 1)]

    text = data.decode("utf-8", errors="replace").strip()
    if "://" not in text:
        try:
            decoded = b64decode_loose(data).decode("utf-8")
            if "://" in decoded:
                text = decoded
        except (CliError, UnicodeDecodeError):
            pass
    links = [line.strip() for line in text.splitlines() if "://" in line]
    if not links:
        if "proxies:" in text:
            raise CliError("this looks like a Clash YAML subscription; use a URI/Base64 or SIP008 endpoint")
        raise CliError("no supported node links found in subscription")
    return [node_name(link, i) for i, link in enumerate(links, 1)]


def empty_state() -> dict[str, Any]:
    return {"version": 1, "nodes": {}, "rules": {}, "profiles": {}}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read state file {path}: {exc}") from exc
    base = empty_state()
    base.update(state)
    return base


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SUDO_UID") and os.environ.get("SUDO_GID"):
        os.chown(path.parent, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        if os.environ.get("SUDO_UID") and os.environ.get("SUDO_GID"):
            os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def get_nodes(args: argparse.Namespace, state: dict[str, Any], refresh: bool = True) -> list[str]:
    if not refresh and args.tag in state["nodes"]:
        return list(state["nodes"][args.tag])
    subscriptions = read_subscriptions(Path(args.config))
    item = subscriptions.get(args.tag)
    if not item:
        raise CliError(f"tagged subscription not found: {args.tag}")
    if not item["tagged"]:
        raise CliError("untagged subscriptions cannot be selected with subtag(); add a tag in dae config")
    nodes = parse_nodes(fetch_subscription(item, Path(args.config).resolve()))
    state["nodes"][args.tag] = nodes
    save_state(Path(args.state), state)
    return nodes


def select_names(nodes: list[str], selectors: list[str], matches: list[str]) -> list[str]:
    selected: list[str] = []
    for pattern in matches:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise CliError(f"invalid regex {pattern!r}: {exc}") from exc
        selected.extend(name for name in nodes if regex.search(name))
    for selector in selectors:
        if selector in nodes:
            selected.append(selector)
            continue
        range_match = re.fullmatch(r"(\d+)-(\d+)", selector)
        indexes: list[int]
        if range_match:
            start, end = map(int, range_match.groups())
            indexes = list(range(start, end + 1))
        elif selector.isdigit():
            indexes = [int(selector)]
        else:
            raise CliError(f"node not found: {selector!r} (use an exact name, number, range, or --match)")
        for index in indexes:
            if not 1 <= index <= len(nodes):
                raise CliError(f"node number out of range: {index}")
            selected.append(nodes[index - 1])
    return list(dict.fromkeys(selected))


def dae_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_filter(tag: str, rule: dict[str, Any]) -> str:
    base = f"subtag({dae_quote(tag)})"
    names = list(rule.get("names", []))
    if not names:
        return base
    params = ", ".join(dae_quote(name) for name in names)
    selected = f"name({params})"
    return f"{base} && {selected}" if rule.get("mode") == "only" else f"{base} && !{selected}"


def generated_config(tag: str, group: str, policy: str, rule: dict[str, Any]) -> str:
    return (
        "# Generated by dae-cli. Manual changes will be overwritten.\n"
        "group {\n"
        f"    {group} {{\n"
        f"        filter: {render_filter(tag, rule)}\n"
        f"        policy: {policy}\n"
        "    }\n"
        "}\n"
    )


def existing_groups(entry: Path, exclude: Path | None = None) -> set[str]:
    groups: set[str] = set()
    for path in config_files(entry):
        if exclude and path.resolve() == exclude.resolve():
            continue
        for body in find_blocks(path.read_text(encoding="utf-8"), "group"):
            groups.update(re.findall(r"(?m)^\s*([\w.-]+)\s*\{", body))
    return groups


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except PermissionError as exc:
        raise CliError(f"cannot write {path}; run this command with sudo") from exc
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def reload_dae(args: argparse.Namespace) -> None:
    """`dae reload` deadlocks when the node it is switching away from is already dead.

    So it always runs under a timeout and says exactly what to do instead.
    """
    dae = shutil.which(args.dae_bin)
    if not dae:
        raise CliError(f"{args.dae_bin!r} not found")
    timeout = getattr(args, "reload_timeout", None) or DEFAULT_RELOAD_TIMEOUT
    try:
        result = subprocess.run([dae, "reload"], text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"dae reload 超过 {timeout}s 没返回 —— 当前节点多半已经不可用，热重载卡住了。\n"
            f"改用重启服务：sudo dm apply <订阅> --restart（等价 systemctl restart {DAE_SERVICE}）"
        ) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(f"dae reload failed:\n{detail}")
    print("dae reloaded（热重载成功，没重启进程）")


def restart_dae(args: argparse.Namespace) -> None:
    """stop + start: cannot deadlock on a broken node, costs a few seconds offline."""
    service = getattr(args, "service", None) or DAE_SERVICE
    print(f"{service}: 重启中…")
    try:
        result = subprocess.run(
            ["systemctl", "restart", service], text=True, capture_output=True, timeout=DEFAULT_RESTART_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"systemctl restart {service} 超时（{DEFAULT_RESTART_TIMEOUT}s）") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(f"systemctl restart {service} failed:\n{detail}")
    state = subprocess.run(["systemctl", "is-active", service], text=True, capture_output=True).stdout.strip()
    print(f"{service}: {state or 'unknown'}")
    if state != "active":
        raise CliError(f"{service} 重启后不是 active（{state}）—— 看 journalctl -u {service} -n 50")


def validate_and_reload(
    args: argparse.Namespace, reload_requested: bool = False, restart_requested: bool = False
) -> None:
    dae = shutil.which(args.dae_bin)
    if not dae:
        print(f"warning: {args.dae_bin!r} not found; skipped validation", file=sys.stderr)
        return
    result = subprocess.run([dae, "validate", "-c", args.config], text=True, capture_output=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(f"dae validation failed:\n{detail}")
    print("dae config validation: OK")

    if restart_requested:
        restart_dae(args)
        return
    if not reload_requested:
        return
    try:
        reload_dae(args)
    except CliError as exc:
        print(f"\n{exc}", file=sys.stderr)
        if not sys.stdin.isatty() or not prompt_yes_no(f"现在改成重启 {DAE_SERVICE}（会断几秒）？", True):
            raise
        restart_dae(args)


def command_subs(args: argparse.Namespace, state: dict[str, Any]) -> None:
    del state
    subscriptions = read_subscriptions(Path(args.config))
    if not subscriptions:
        print("No subscriptions found.")
        return
    for tag, item in subscriptions.items():
        suffix = "" if item["tagged"] else " (untagged; cannot filter)"
        print(f"{tag}{suffix}\t{item['file']}")


def command_nodes(args: argparse.Namespace, state: dict[str, Any]) -> None:
    nodes = get_nodes(args, state, refresh=not args.cached)
    counts: dict[str, int] = {}
    for name in nodes:
        counts[name] = counts.get(name, 0) + 1
    for index, name in enumerate(nodes, 1):
        duplicate = f"  [duplicate x{counts[name]}]" if counts[name] > 1 else ""
        print(f"{index:4d}  {name}{duplicate}")
    print(f"\n{len(nodes)} node(s)")


def command_rule(args: argparse.Namespace, state: dict[str, Any]) -> None:
    nodes = get_nodes(args, state, refresh=not args.cached)
    names = select_names(nodes, args.selectors, args.match)
    if not names:
        raise CliError("selection is empty")
    current = state["rules"].get(args.tag, {"mode": args.rule_command, "names": []})
    if args.rule_command == "only":
        current = {"mode": "only", "names": names}
    else:
        if current.get("mode") != "exclude":
            current = {"mode": "exclude", "names": []}
        current["names"] = list(dict.fromkeys(current.get("names", []) + names))
    state["rules"][args.tag] = current
    save_state(Path(args.state), state)
    print(f"{current['mode']}: {len(current['names'])} node(s)")
    print("Run 'dae-cli apply TAG' to update the generated dae group.")


def command_restore(args: argparse.Namespace, state: dict[str, Any]) -> None:
    rule = state["rules"].get(args.tag)
    if not rule:
        raise CliError(f"no rule exists for {args.tag}")
    nodes = get_nodes(args, state, refresh=not args.cached)
    names = set(select_names(nodes, args.selectors, args.match))
    rule["names"] = [name for name in rule.get("names", []) if name not in names]
    state["rules"][args.tag] = rule
    save_state(Path(args.state), state)
    print(f"{rule['mode']}: {len(rule['names'])} node(s)")


def command_reset(args: argparse.Namespace, state: dict[str, Any]) -> None:
    state["rules"].pop(args.tag, None)
    save_state(Path(args.state), state)
    print(f"Rules cleared for {args.tag}.")


def command_status(args: argparse.Namespace, state: dict[str, Any]) -> None:
    rule = state["rules"].get(args.tag, {"mode": "all", "names": []})
    print(f"subscription: {args.tag}")
    print(f"mode: {rule['mode']}")
    for name in rule.get("names", []):
        print(f"  - {name}")
    profile = state["profiles"].get(args.tag)
    if profile:
        print(f"group: {profile['group']}")
        print(f"output: {profile['output']}")


def choose_apply_mode() -> tuple[bool, bool]:
    """(reload, restart) -- hot reload is fast, restart survives a dead current node."""
    choice = pick(
        "写好了，dae 怎么生效？",
        [
            "热重载 dae reload —— 快、不重启进程（当前节点还能用时选这个）",
            "重启服务 systemctl restart dae —— 慢几秒（当前节点已经坏了时选这个）",
            "先不动 —— 我稍后自己处理",
        ],
    )
    if choice == 0:
        return True, False
    if choice == 1:
        return False, True
    return False, False


def command_apply(args: argparse.Namespace, state: dict[str, Any]) -> None:
    group = args.group or DEFAULT_GROUP
    output = Path(args.output or DEFAULT_OUTPUT)
    policy = args.policy or "min_moving_avg"
    if not re.fullmatch(r"[A-Za-z_][\w.-]*", group):
        raise CliError(f"invalid dae group name: {group!r}")
    if not re.fullmatch(r"(?:random|min|min_moving_avg|min_avg10|fixed\(\d+\))", policy):
        raise CliError(f"unsupported dae group policy: {policy!r}")
    if group in existing_groups(Path(args.config), exclude=output):
        raise CliError(f"group {group!r} already exists outside {output}; choose another --group")
    rule = state["rules"].get(args.tag, {"mode": "all", "names": []})
    content = generated_config(args.tag, group, policy, rule)
    previous = output.read_bytes() if output.exists() else None
    atomic_write(output, content)
    print(f"wrote {output}")
    included = output.resolve() in {path.resolve() for path in config_files(Path(args.config))}
    if not included:
        print(
            f"warning: {output} is not included by {args.config}; add it to an include block",
            file=sys.stderr,
        )
    else:
        try:
            validate_and_reload(args, getattr(args, "reload", False), getattr(args, "restart", False))
        except CliError:
            if previous is None:
                output.unlink(missing_ok=True)
            else:
                atomic_write(output, previous.decode("utf-8"))
            print(f"restored previous {output}", file=sys.stderr)
            raise
    state["profiles"][args.tag] = {"group": group, "output": str(output), "policy": policy}
    save_state(Path(args.state), state)


def prompt(prompt_text: str) -> str:
    try:
        return input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        print()
        raise CliError("operation cancelled") from exc


def prompt_index(prompt_text: str, count: int) -> int:
    while True:
        answer = prompt(prompt_text)
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        print(f"请输入 1-{count}。")


def prompt_yes_no(prompt_text: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = prompt(prompt_text + suffix).lower()
        if not answer:
            return default
        if answer in {"y", "yes", "是"}:
            return True
        if answer in {"n", "no", "否"}:
            return False
        print("请输入 y 或 n。")


# ---------------------------------------------------------------- full-screen picker

KEY_SEQUENCES = {
    b"[A": "up",
    b"[B": "down",
    b"[H": "home",
    b"[F": "end",
    b"[1~": "home",
    b"[4~": "end",
    b"[C": "right",
    b"[D": "back",
    b"OA": "up",
    b"OB": "down",
    b"OD": "back",
    b"OH": "home",
    b"OF": "end",
}
TUI_HINT = "↑↓ / j k 移动    Enter 选择    Esc / ← 返回上一级    q 退出"


def tui_available() -> bool:
    """Full-screen UI only on a real terminal; pipes and tests fall back to numbers."""
    if os.environ.get("DAE_CLI_PLAIN"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"


def clip(text: str, width: int) -> str:
    if width <= 1:
        return text
    if len(text) <= width - 1:
        return text
    return text[: width - 2] + "…"


def read_key(fd: int = 0) -> str:
    try:
        char = os.read(fd, 1)
    except OSError:
        return "quit"
    if not char:
        return "back"
    if char == b"\x03":
        return "abort"
    if char == b"\x1b":
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return "back"
        seq = os.read(fd, 1)
        if seq in (b"[", b"O"):
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                return "other"
            seq += os.read(fd, 1)
            if seq[1:2].isdigit():
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    seq += os.read(fd, 1)
        return KEY_SEQUENCES.get(seq, "other")
    if char in (b"\r", b"\n", b" "):
        return "enter"
    if char in (b"\x08", b"\x7f"):  # Backspace
        return "back"
    if char in (b"q", b"Q"):
        return "quit"
    if char in (b"h", b"H"):
        return "back"
    if char in (b"j", b"J"):
        return "down"
    if char in (b"k", b"K"):
        return "up"
    if char == b"g":
        return "home"
    if char == b"G":
        return "end"
    return "other"


def draw_screen(title: str, items: list[str], index: int, hint: str) -> None:
    width, height = shutil.get_terminal_size((80, 24))
    rows = max(3, height - 4)
    start = 0
    if len(items) > rows:
        start = min(max(0, index - rows // 2), len(items) - rows)
    out = [f"\x1b[H\x1b[2K\x1b[1m{clip(title, width)}\x1b[0m", "\x1b[2K" + "─" * min(width, 64)]
    for offset, text in enumerate(items[start : start + rows]):
        line = clip(f"  {text}", width)
        if start + offset == index:
            out.append("\x1b[2K\x1b[7m" + line.ljust(min(width, 64)) + "\x1b[0m")
        else:
            out.append("\x1b[2K" + line)
    out.append("\x1b[2K" + clip(hint, width))
    out.append("\x1b[J")
    sys.stdout.write("\r\n".join(out))
    sys.stdout.flush()


def pick_numbered(title: str, items: list[str], hint: str = "") -> int | None:
    """The old line-by-line menu，used when there is no terminal."""
    print(f"\n{title}")
    for number, text in enumerate(items, 1):
        print(f"   {number}) {text}")
    if hint:
        print(f"   {hint}")
    answer = (prompt("选择（0 返回）[0]: ") or "0").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(items):
        return None
    return int(answer) - 1


def pick(title: str, items: list[str], hint: str = TUI_HINT, index: int = 0) -> int | None:
    """Arrow-key menu: returns the chosen index, or None on q / Esc."""
    if not items:
        return None
    if not tui_available():
        return pick_numbered(title, items, hint if hint != TUI_HINT else "")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    index = max(0, min(index, len(items) - 1))
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?25l")
        while True:
            draw_screen(title, items, index, hint)
            key = read_key(fd)
            if key in ("abort", "quit"):
                raise SystemExit(0)
            if key == "back":
                return None
            if key == "enter":
                return index
            if key == "up":
                index = (index - 1) % len(items)
            elif key == "down":
                index = (index + 1) % len(items)
            elif key == "home":
                index = 0
            elif key == "end":
                index = len(items) - 1
    finally:
        sys.stdout.write("\x1b[?25h\x1b[H\x1b[J")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

def print_node_selection(nodes: list[str], rule: dict[str, Any]) -> None:
    mode = rule.get("mode", "all")
    selected = set(rule.get("names", []))
    for index, name in enumerate(nodes, 1):
        enabled = mode == "all" or (name in selected if mode == "only" else name not in selected)
        marker = "[x]" if enabled else "[ ]"
        print(f"{marker} {index:4d}  {name}")


NODE_MODES = ["使用全部节点", "排除部分节点", "仅使用选中的节点", "保持当前规则"]


def _node_rule_step(args: argparse.Namespace, state: dict[str, Any], tag: str) -> bool:
    """One pass of the node-rule wizard.

    True  = finished (group file written), back to whatever called us.
    False = user pressed 返回 / cancelled, caller decides where to go back to.
    """
    args.tag = tag
    print(f"\n正在获取 {tag} 的节点...")
    nodes = get_nodes(args, state, refresh=True)
    print(f"获取到 {len(nodes)} 个节点。\n")

    mode_choice = pick(f"{tag} — 规则模式", NODE_MODES)
    if mode_choice is None:
        return False

    if mode_choice == 0:
        state["rules"].pop(tag, None)
        rule = {"mode": "all", "names": []}
    elif mode_choice in {1, 2}:
        for index, name in enumerate(nodes, 1):
            print(f"{index:4d}  {name}")
        raw = prompt("\n输入节点编号或范围，例如 1 3-5（可留空）: ")
        selectors = [part for part in re.split(r"[,\s]+", raw) if part]
        regex = prompt("名称正则（可留空，例如 香港|日本）: ")
        names = select_names(nodes, selectors, [regex] if regex else [])
        if mode_choice == 2 and not names:
            print("“仅使用”模式至少要选一个节点，回到模式选择。")
            return False
        rule = {"mode": "exclude" if mode_choice == 1 else "only", "names": names}
        state["rules"][tag] = rule
    else:
        rule = state["rules"].get(tag, {"mode": "all", "names": []})

    print("\n生效预览（[x] 表示会被使用）:\n")
    print_node_selection(nodes, rule)
    print(f"\n目标文件: {DEFAULT_OUTPUT}")
    print(f"分组名称: {DEFAULT_GROUP}")
    if not prompt_yes_no("写入配置吗？"):
        print("未写入配置，回到模式选择。")
        return False

    args.group = DEFAULT_GROUP
    args.output = DEFAULT_OUTPUT
    args.policy = "min_moving_avg"
    args.reload, args.restart = choose_apply_mode()
    command_apply(args, state)
    return True


def command_node_flow(args: argparse.Namespace, state: dict[str, Any]) -> None:
    subscriptions = {
        tag: item for tag, item in read_subscriptions(Path(args.config)).items() if item["tagged"]
    }
    if not subscriptions:
        raise CliError("配置中没有带标签的订阅")
    tags = list(subscriptions)
    given = getattr(args, "tag", None)
    fixed = given if given in subscriptions else None
    while True:
        if fixed:
            tag = fixed
        else:
            choice = pick("dae 订阅节点管理 — 选择订阅", tags)
            if choice is None:
                print("已返回。")
                return
            tag = tags[choice]
        if _node_rule_step(args, state, tag) or fixed:
            return


# ------------------------------------------------------- subscription management


def mask_url(url: str) -> str:
    """Show enough to tell subscriptions apart, never the token."""
    core = url.split("#", 1)[0]
    scheme = ""
    rest = core
    match = re.match(r"^([a-zA-Z0-9+.-]+://)(.*)$", core)
    if match:
        scheme, rest = match.groups()
    host, _, tail = rest.partition("/")
    tail = tail.split("?")[0]
    if len(tail) > 8:
        tail = tail[:6] + "…"
    shown = f"{scheme}{host}"
    if tail:
        shown += f"/{tail}"
    if "?" in core:
        shown += "?…"
    return shown


def code_mask(text: str) -> bytearray:
    """1 where the character is real config code, 0 inside a comment (or newline)."""
    mask = bytearray(len(text))
    quote: str | None = None
    escaped = False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            continue
        if escaped:
            escaped = False
            mask[index] = 1
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            mask[index] = 1
            continue
        if char in "'\"":
            quote = char
            mask[index] = 1
        elif char == "#":
            comment = True
        else:
            mask[index] = 1
    return mask


def find_block_spans(text: str, name: str) -> list[tuple[int, int, int]]:
    """(block_start, body_start, body_end) offsets into the ORIGINAL text.

    Unlike find_blocks() this keeps positions usable for editing, so commented-out
    braces do not shift anything.
    """
    mask = code_mask(text)
    spans: list[tuple[int, int, int]] = []
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"\s*\{")
    for match in pattern.finditer(text):
        brace = match.end() - 1
        if not mask[brace]:
            continue
        depth = 0
        for pos in range(brace, len(text)):
            if not mask[pos]:
                continue
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((match.start(), brace + 1, pos))
                    break
    return spans


def block_indent(text: str, body_start: int, body_end: int) -> str:
    for line in text[body_start:body_end].splitlines():
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return "    "


def item_tag(line: str) -> str | None:
    match = ITEM_RE.match(line)
    if not match or not match.group("tag"):
        return None
    return unquote(match.group("tag"))


def dae_key(tag: str) -> str:
    """Bare key when it is a plain identifier, quoted otherwise (matches existing configs)."""
    return tag if re.fullmatch(r"[A-Za-z_][\w.-]*", tag) else dae_quote(tag)


def add_subscription(text: str, tag: str, url: str) -> str:
    """Append one item to the first subscription block, or create the block."""
    entry = f"{dae_key(tag)}: {dae_quote(url)}\n"
    spans = find_block_spans(text, "subscription")
    if not spans:
        prefix = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{prefix}subscription {{\n    {entry}}}\n"
    _, body_start, body_end = spans[0]
    indent = block_indent(text, body_start, body_end)
    head = text[:body_end]
    if not head.endswith("\n"):
        head += "\n"
    return f"{head}{indent}{entry}{text[body_end:]}"


def remove_subscription(text: str, tag: str) -> tuple[str, int]:
    """Delete the item line(s) for tag; drop the block when nothing is left."""
    removed = 0
    for block_start, body_start, body_end in reversed(find_block_spans(text, "subscription")):
        body = text[body_start:body_end]
        lines = body.splitlines(keepends=True)
        kept = [line for line in lines if item_tag(line) != tag]
        hit = len(lines) - len(kept)
        if not hit:
            continue
        removed += hit
        if any(line.strip() for line in kept):
            text = f"{text[:body_start]}{''.join(kept)}{text[body_end:]}"
        else:
            text = f"{text[:block_start]}{text[body_end + 1:]}"
    return text, removed


def subscription_target(entry: Path) -> Path:
    """Where a new subscription goes: the file that already has a block, else config.d."""
    entry = entry.expanduser()
    for path in config_files(entry):
        if find_block_spans(path.read_text(encoding="utf-8"), "subscription"):
            return path
    config_dir = entry.parent / "config.d"
    if config_dir.is_dir():
        existing = sorted(config_dir.glob("*subscription*.dae"))
        return existing[0] if existing else config_dir / "20-subscription.dae"
    return entry


def backup_and_write(path: Path, text: str) -> Path | None:
    """Write text, keeping owner/mode, after copying the old file next to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = path.stat() if path.exists() else None
    backup: Path | None = None
    if stat:
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    atomic_write(path, text)
    if stat:
        try:
            os.chmod(path, stat.st_mode & 0o7777)
            os.chown(path, stat.st_uid, stat.st_gid)
        except (PermissionError, AttributeError):
            pass
    return backup


def apply_config_change(args: argparse.Namespace, path: Path, backup: Path | None) -> None:
    try:
        validate_and_reload(args, getattr(args, "reload", False), getattr(args, "restart", False))
    except CliError:
        if backup is not None and backup.exists():
            shutil.copy2(backup, path)
            print(f"rolled back to {backup}", file=sys.stderr)
        raise


def fetch_and_count(args: argparse.Namespace, state: dict[str, Any], tag: str) -> list[str]:
    subscriptions = read_subscriptions(Path(args.config))
    item = subscriptions.get(tag)
    if item is None:
        raise CliError(f"没有这个订阅: {tag!r}")
    nodes = parse_nodes(fetch_subscription(item, Path(args.config)))
    state["nodes"][tag] = nodes
    save_state(Path(args.state), state)
    return nodes


def print_subscription_table(entry: Path, state: dict[str, Any]) -> None:
    subscriptions = read_subscriptions(entry)
    if not subscriptions:
        print("  （还没有订阅）")
        return
    print(f"   {'#':>2}  标签            节点  链接")
    for index, (tag, item) in enumerate(subscriptions.items(), 1):
        cached = len(state.get("nodes", {}).get(tag, []))
        print(f"   {index:>2}.  {tag:<14} {cached or '-':>4}  {mask_url(item['url'])}")


def refresh_all_counts(args: argparse.Namespace, state: dict[str, Any]) -> None:
    for tag in read_subscriptions(Path(args.config)):
        try:
            nodes = fetch_and_count(args, state, tag)
            print(f"  {tag}: {len(nodes)} 个节点")
        except (CliError, OSError) as exc:
            print(f"  {tag}: 抓取失败（{exc}）")


def command_sub(args: argparse.Namespace, state: dict[str, Any]) -> None:
    action = getattr(args, "action", None) or "menu"
    entry = Path(args.config)
    if action == "menu":
        command_sub_menu(args, state)
        return
    if action == "list":
        print_subscription_table(entry, state)
        return

    if action == "add":
        tag = (getattr(args, "tag", None) or prompt("订阅标签（字母/数字/_ . -，例如 airport2）: ")).strip()
        if not re.fullmatch(r"[A-Za-z_][\w.-]*", tag):
            raise CliError(f"无效标签: {tag!r}（只能用字母、数字、_ . -）")
        if tag in read_subscriptions(entry):
            raise CliError(f"订阅 {tag!r} 已存在（要换链接请先删除再新增）")
        url = prompt(f"订阅链接（{tag}）: ")
        if not re.match(r"^[a-zA-Z0-9+.-]+://", url):
            raise CliError("链接必须以 scheme:// 开头，例如 https-file://… 或 http-file://…")
        target = subscription_target(entry)
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        backup = backup_and_write(target, add_subscription(text, tag, url))
        print(f"已写入 {target}" + (f"（备份 {backup.name}）" if backup else ""))
        apply_config_change(args, target, backup)
        print(f"订阅 {tag} 已添加：{mask_url(url)}")
        if prompt_yes_no("现在抓取一次，确认链接可用？", True):
            print(f"{tag}: {len(fetch_and_count(args, state, tag))} 个节点")
        return

    if action == "remove":
        subscriptions = read_subscriptions(entry)
        if not subscriptions:
            raise CliError("配置里没有订阅可删")
        tag = (getattr(args, "tag", None) or prompt(f"要删除的标签（{' / '.join(subscriptions)}）: ")).strip()
        item = subscriptions.get(tag)
        if item is None:
            raise CliError(f"没有这个订阅: {tag!r}")
        path = Path(item["file"])
        new_text, removed = remove_subscription(path.read_text(encoding="utf-8"), tag)
        if not removed:
            raise CliError(f"在 {path} 里没找到 {tag!r} 的条目")
        if not prompt_yes_no(f"确认从 {path.name} 删除订阅 {tag}？"):
            print("已取消。")
            return
        backup = backup_and_write(path, new_text)
        print(f"已写入 {path}" + (f"（备份 {backup.name}）" if backup else ""))
        apply_config_change(args, path, backup)
        state["nodes"].pop(tag, None)
        state["rules"].pop(tag, None)
        save_state(Path(args.state), state)
        print(f"订阅 {tag} 已删除。")
        return

    raise CliError(f"unknown sub action: {action!r}")


def command_sub_menu(args: argparse.Namespace, state: dict[str, Any]) -> None:
    entry = Path(args.config)
    while True:
        subscriptions = read_subscriptions(entry)
        tags = list(subscriptions)
        rows = []
        for tag in tags:
            cached = len(state.get("nodes", {}).get(tag, []))
            rows.append(f"{tag:<14} {cached or '-':>4} 节点   {mask_url(subscriptions[tag]['url'])}")
        rows += ["＋ 新增订阅", "⟳ 刷新节点数"]
        choice = pick(f"订阅管理  ({entry})", rows)
        if choice is None:
            return
        namespace = argparse.Namespace(**{**vars(args), "tag": None})
        try:
            if choice == len(tags):
                namespace.action = "add"
                command_sub(namespace, state)
            elif choice == len(tags) + 1:
                refresh_all_counts(args, state)
            else:
                tag = tags[choice]
                action = pick(
                    f"{tag}  {mask_url(subscriptions[tag]['url'])}",
                    ["用它写进 50-group（选节点）", "删除这条订阅", "返回"],
                )
                if action is None or action == 2:
                    continue
                if action == 0:
                    command_node_flow(argparse.Namespace(**{**vars(args), "tag": tag}), state)
                    continue
                namespace.action = "remove"
                namespace.tag = tag
                command_sub(namespace, state)
        except (CliError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)


def command_interactive(args: argparse.Namespace, state: dict[str, Any]) -> None:
    while True:
        choice = pick(
            "dae-cli",
            [
                "订阅管理（列表 / 新增 / 删除）",
                "节点规则（选择哪些节点进代理组）",
                "编辑配置文件（global / dns / routing …）",
                "退出",
            ],
        )
        if choice is None or choice == 3:
            return
        if choice == 0:
            command_sub_menu(args, state)
        elif choice == 1:
            command_node_flow(args, state)
        else:
            try:
                command_edit(argparse.Namespace(**{**vars(args), "target": None}), state)
            except (CliError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)


# --------------------------------------------------------------- config editing

ROLE_HINTS = {
    "global": "监听 / 绑定 / 日志",
    "subscription": "订阅链接（也可以直接用 dm sub）",
    "node": "手写节点",
    "dns": "DNS 规则",
    "group": "代理组（dm apply 生成，别手改）",
    "routing": "分流规则",
}
GENERATED_MARKER = "Generated by dae-cli"
EDITOR_CANDIDATES = ("nvim", "vim", "vi", "nano", "micro", "hx", "kak")


def config_roles(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [name for name in ROLE_HINTS if find_blocks(text, name)]


def is_generated(path: Path) -> bool:
    try:
        return GENERATED_MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def describe_config(path: Path) -> str:
    roles = config_roles(path)
    if not roles:
        return "（没有已知的顶层块）"
    return " + ".join(ROLE_HINTS.get(role, role) for role in roles)


def editable_files(entry: Path) -> list[Path]:
    """Files from the include chain worth editing by hand (generated ones dropped)."""
    return [path for path in config_files(entry) if not is_generated(path)]


def pick_editor() -> str:
    for name in (os.environ.get("EDITOR"), os.environ.get("VISUAL")):
        if name and shutil.which(name.split()[0]):
            return name
    for name in EDITOR_CANDIDATES:
        if shutil.which(name):
            return name
    raise CliError("找不到编辑器：装一个 nvim / vim / nano，或者 export EDITOR=… 再试")


def invoking_user() -> tuple[int, int, str] | None:
    """(uid, gid, home) of the user who ran sudo, so THEIR editor config applies."""
    if os.geteuid() != 0:
        return None
    name = os.environ.get("SUDO_USER")
    if not name:
        return None
    try:
        info = pwd.getpwnam(name)
    except KeyError:
        return None
    return info.pw_uid, info.pw_gid, info.pw_dir


def run_editor(editor: str, path: Path) -> int:
    argv = shlex.split(editor) + [str(path)]
    env = dict(os.environ)
    preexec = None
    identity = invoking_user()
    if identity is not None:
        uid, gid, home = identity
        owner = os.environ.get("SUDO_USER", "")
        env.update(HOME=home, USER=owner, LOGNAME=owner, EDITOR=editor)

        def preexec() -> None:  # drop root: the file is user-owned anyway
            try:
                os.setgroups([])
            except OSError:
                pass
            os.setgid(gid)
            os.setuid(uid)

    sys.stdout.flush()
    return subprocess.run(argv, env=env, preexec_fn=preexec, check=False).returncode


def line_delta(before: str, after: str) -> tuple[int, int]:
    plus = minus = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            plus += 1
        elif line.startswith("- "):
            minus += 1
    return plus, minus


def command_edit(args: argparse.Namespace, state: dict[str, Any]) -> None:
    entry = Path(args.config).expanduser()
    files = editable_files(entry)
    if not files:
        raise CliError(f"{entry} 下面没有可编辑的配置文件")

    wanted = (getattr(args, "target", None) or "").strip()
    if wanted:
        matches = [item for item in files if wanted in item.name]
        if not matches:
            raise CliError(f"没找到 '{wanted}'；可选：{', '.join(item.name for item in files)}")
        if len(matches) > 1:
            raise CliError(f"'{wanted}' 匹配到多个文件：{', '.join(item.name for item in matches)}")
        target = matches[0]
    else:
        rows = []
        for path in files:
            try:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            except OSError:
                stamp = "?"
            rows.append(f"{path.name:<20} {describe_config(path):<26} {stamp}")
        choice = pick(f"编辑配置文件  ({entry})", rows)
        if choice is None:
            print("已返回。")
            return
        target = files[choice]

    before = target.read_text(encoding="utf-8", errors="replace")
    backup = backup_and_write(target, before)
    editor = pick_editor()
    print(f"用 {editor} 打开 {target}（保存退出后自动跑 dae validate）…")
    code = run_editor(editor, target)

    after = target.read_text(encoding="utf-8", errors="replace")
    if after == before:
        print("文件没有改动。")
        if backup is not None:
            backup.unlink(missing_ok=True)
        return
    if code != 0:
        print(f"编辑器退出码 {code}，文件已按你保存的内容保留（备份 {backup.name if backup else '无'}）。",
              file=sys.stderr)
    plus, minus = line_delta(before, after)
    print(f"改动：+{plus} / -{minus} 行（备份 {backup.name if backup else '无'}）")

    try:
        validate_and_reload(args, getattr(args, "reload", False), getattr(args, "restart", False))
    except CliError:
        if backup is not None and prompt_yes_no("校验没通过，要回滚到编辑前的内容吗？", True):
            shutil.copy2(backup, target)
            print("已回滚。")
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dae-cli", description=__doc__)
    parser.add_argument("-c", "--config", default="/etc/dae/config.dae", help="dae entry config")
    parser.add_argument("--state", default=str(default_state_path()), help="CLI state file")
    parser.add_argument("--dae-bin", default="dae", help="dae executable")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command")

    interactive = sub.add_parser("interactive", aliases=["i"], help="interactive node selection")
    interactive.set_defaults(func=command_interactive)

    subs = sub.add_parser("subs", aliases=["subscriptions"], help="list subscriptions")
    subs.set_defaults(func=command_subs)

    nodes = sub.add_parser("nodes", help="list node names")
    nodes.add_argument("tag")
    nodes.add_argument("--cached", action="store_true", help="use the last fetched node list")
    nodes.set_defaults(func=command_nodes)

    for command, help_text in (("exclude", "add nodes to the exclusion list"), ("only", "replace the allowlist")):
        rule = sub.add_parser(command, help=help_text)
        rule.add_argument("tag")
        rule.add_argument("selectors", nargs="*", help="exact names, numbers, or ranges such as 2-5")
        rule.add_argument("-m", "--match", action="append", default=[], help="select names by regex")
        rule.add_argument("--cached", action="store_true")
        rule.set_defaults(func=command_rule, rule_command=command)

    restore = sub.add_parser("restore", help="remove nodes from the current only/exclude rule")
    restore.add_argument("tag")
    restore.add_argument("selectors", nargs="*")
    restore.add_argument("-m", "--match", action="append", default=[])
    restore.add_argument("--cached", action="store_true")
    restore.set_defaults(func=command_restore)

    reset = sub.add_parser("reset", help="clear rules for a subscription")
    reset.add_argument("tag")
    reset.set_defaults(func=command_reset)

    status = sub.add_parser("status", help="show current rules")
    status.add_argument("tag")
    status.set_defaults(func=command_status)

    apply = sub.add_parser("apply", help="write the managed dae group")
    apply.add_argument("tag")
    apply.add_argument("--group", help=f"managed group name (default: {DEFAULT_GROUP})")
    apply.add_argument("--output", help="generated config file")
    apply.add_argument("--policy", help="dae group policy")
    apply.add_argument("--reload", action="store_true", help="hot reload dae after validation (fast)")
    apply.add_argument("--restart", action="store_true", help="restart the dae service (safe when the current node is dead)")
    apply.set_defaults(func=command_apply)

    manage = sub.add_parser("sub", help="list / add / remove subscriptions in the dae config")
    manage.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="menu")
    manage.add_argument("tag", nargs="?", help="subscription tag (prompted when omitted)")
    manage.add_argument("--reload", action="store_true", help="hot reload dae after the change")
    manage.add_argument("--restart", action="store_true", help="restart the dae service instead of reloading")
    manage.set_defaults(func=command_sub)

    editor = sub.add_parser("edit", help="open one of the dae config files in your editor")
    editor.add_argument("target", nargs="?", help="file name or a part of it (global / dns / routing …)")
    editor.add_argument("--reload", action="store_true", help="hot reload dae after editing")
    editor.add_argument("--restart", action="store_true", help="restart the dae service instead of reloading")
    editor.set_defaults(func=command_edit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        state = load_state(Path(args.state))
        if args.command is None:
            command_interactive(args, state)
        else:
            args.func(args, state)
        return 0
    except (CliError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
