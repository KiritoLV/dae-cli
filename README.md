# dae-cli

Pick exactly which nodes [dae](https://github.com/daeuniverse/dae) is allowed to use,
without hand-editing dae config files — and without giving the CLI blanket root.

```console
$ dm nodes airport          # numbered list of nodes
$ dm exclude airport 1 3-5  # drop the info rows / dead nodes
$ dm only airport 2 7 8     # or keep only these
$ dm apply airport --reload # write the group file and hot-reload dae
$ dm                        # ...or just use the interactive menu
```

dae takes a hand-written config. Routing groups are `group { filter: ... }` blocks, so
"use all nodes except the traffic-info rows" turns into editing a file by hand and
re-typing node names. This CLI fetches the subscription you already have, shows the
nodes with stable numbering, previews what will be used, and writes one group file.

## Two things it is careful about

**1. sudo never runs a script you can edit.**

The obvious design is `sudo install -o $USER .../dae_cli.py` + a NOPASSWD rule for it —
that is a privilege-escalation hole: whoever can write the script can make root run
anything. So the NOPASSWD rule points at a **root-owned wrapper** instead:

| Path | Owner | Role |
|---|---|---|
| `/usr/local/sbin/dae-cli-menu` | root, 0755 | subcommand allowlist; rejects any argument starting with `-` except `--reload` / `--cached` |
| `/usr/local/libexec/dae-cli/dae_cli.py` | root, 0755 | the CLI; you cannot edit it |
| `/usr/local/libexec/dae-cli/daed-sub/` | root, 0755 | the `ds` converter (unprivileged — never run it via sudo) |
| `/etc/sudoers.d/dae-cli-<user>` | root, 0440 | `NOPASSWD: /usr/local/sbin/dae-cli-menu` and nothing else |

The allowlist matters because the CLI itself has root-capable options
(`--output`, `--state`, `--config`, `--dae-bin`). Those can rewrite arbitrary files
when run as root, so they must not be reachable through sudo — they are blocked at the
wrapper, and the wrapper is not user-writable.

**2. Node URIs and the subscription URL are never stored.**

`~/.config/dae-cli/state.json` caches **display names only** (plus the group name and
output path), so it can renumber nodes without re-fetching. Note that display names
often contain your provider's name, node locations and expiry dates — treat the file
as private. It lives in the *invoking user's* home even when the CLI runs under sudo
(`SUDO_USER` is honoured).

## Install

```console
$ sudo ./install.sh            # interactive menu: install / uninstall / status
$ sudo ./install.sh --install  # same, non-interactive (scripts, Ansible, ...)
$ ./install.sh --status        # what is installed right now (no root needed)
$ sudo ./install.sh --uninstall
$ ./install.sh --install --dry-run   # print every action, touch nothing
```

Running it as root with no arguments gives:

```
    1) 安装      install    (root-owned wrapper + 受限 sudoers 规则)
    2) 卸载      uninstall  (删入口 + 库 + sudoers，保留 dae 配置与 state)
    3) 查看状态  status     (装了什么、md5 是否一致、免密是否生效)
    q) 退出      quit
```

`--status` also compares the installed `dae_cli.py` with the one in the repo, so
"did I actually reinstall after pulling?" is one command. Uninstalling never touches
`/etc/dae/` or your cached node names.

Then add the short command to your shell:

```sh
# ~/.zshrc or ~/.bashrc
dm() { sudo -n /usr/local/sbin/dae-cli-menu "$@"; }
```

`sudo -n` = non-interactive, exactly what the NOPASSWD rule is for. Pass arguments
through verbatim (`"$@"`) or the wrapper's allowlist will reject them.

Without the wrapper the CLI still works directly, it just needs real root:

```console
$ sudo python3 dae_cli.py subs
```

## Commands

| Command | What it does |
|---|---|
| *(no arguments)* | interactive menu: pick a subscription, then all / exclude / only / keep |
| `subs` | list subscription labels found in the dae config |
| `nodes <sub> [--cached]` | numbered node list |
| `only <sub> <sel>...` | replace the rule with an explicit allowlist |
| `exclude <sub> <sel>...` | use everything except these |
| `restore <sub> <sel>...` | remove these names from the current rule |
| `reset <sub>` | back to all nodes |
| `status <sub>` | show the current rule |
| `apply <sub> [--reload\|--restart]` | write the group file; `--reload` = hot reload, `--restart` = restart the service |
| `sub [list\|add\|remove] [tag]` | manage the subscriptions themselves (see below) |
| `edit [name] [--reload]` | open global / dns / routing … in `$EDITOR`, validate on exit |

`<sel>` accepts numbers (`3`), ranges (`3-8`) and a name regex (`--match '流量|到期|官网'`).
`--cached` uses the cached node list instead of fetching the subscription again.

Global options (direct CLI only, blocked at the wrapper): `-c/--config <dae entry config>`,
`--output <group file>`, `--state <state file>`, `--dae-bin <binary>`.

Defaults: entry config `/etc/dae/config.dae`, generated group file
`/etc/dae/config.d/50-group.dae`, group name `proxy`. The entry config must include it:

```dae
include {
    config.d/*.dae
}
```

Because the group is named `proxy` by default, an existing `fallback: proxy` routing rule
keeps working unchanged.

## ds: convert a Clash subscription for daed

`daed-sub/daed-sub-manager.sh` (installed to `/usr/local/libexec/dae-cli/daed-sub/`) is the
companion tool for providers that only hand out **Clash YAML**:

```console
$ ds status          # what is bound, node count, is the local server up
$ ds bind            # paste the subscription URL (prompted, so it stays out of shell history)
$ ds update          # fetch + convert again
$ ds up              # update, then start the local HTTP server
$ ds start|stop|restart
$ ds                 # interactive menu
```

It converts the YAML to a plain URI list, serves it on `http://127.0.0.1:18080/links.txt`
as a **systemd --user** unit, and you point daed's subscription at that URL — so daed never
needs to understand Clash YAML itself.

Privacy / safety notes:

- the bound URL is stored `0600` in `~/.local/share/daed-sub-manager/subscription.url`,
  and `ds status` only prints a masked form;
- it runs entirely inside your home directory: **never run `ds` via sudo**, it would write
  root-owned files into `/root` and the user unit would stop matching;
- the local server binds `127.0.0.1` only.

## How the dae config is laid out (what this tool expects)

This CLI was written against a **split config**: one entry file that only pulls in a
`config.d/` directory. Everything below is the layout it is tested against.

```
/etc/dae/
├── config.dae                 entry: only an include block
└── config.d/
    ├── 10-global.dae          global { }          listen / bind / log
    ├── 20-subscription.dae    subscription { }    ← dm sub reads and edits this
    ├── 30-node.dae            node { }            hand-written nodes (optional)
    ├── 40-dns.dae             dns { }             (not touched by this tool)
    ├── 50-group.dae           group { }           ← generated by `dm apply`, do not edit
    └── 60-routing.dae         routing { }         (not touched by this tool)
```

Entry file — nothing but the include, so the pieces stay independent:

```dae
include {
  config.d/*.dae
}
```

Subscriptions — one `tag: 'url'` per line, no indentation required (the tool reuses
whatever indentation the block already has), tags may be bare or quoted:

```dae
subscription {
myairport: 'https-file://sub.example.com/link/abcdef'
local: 'http-file://127.0.0.1:18080/links.txt'
}
```

Rules this layout implies:

- `dm sub add` appends into the **existing** `subscription { }` block, so a split config
  stays split; `dm sub remove` deletes just that one line and drops the block once empty.
- `50-group.dae` is rewritten whole on every `dm apply` (it starts with a
  `# Generated by dae-cli` header) — edit the routing file, not that one.
- Anything the entry config does not `include` is invisible to dae; if you point the tool
  at a file outside the include set, the change will not take effect.

中文一句话：本工具是按「`config.dae` 只放 `include`，订阅单独写在
`config.d/20-subscription.dae` 的 `subscription { }` 块里」这套结构写的；所有文件名都是
我自己的命名习惯（10/20/30/40/50/60 前缀），工具只认入口的 `include`，不认前缀。

Single-file setups (everything in one `config.dae`) are **not** a tested path — it will
most likely still work because the reader follows the entry file, but keep it in mind if
something looks off.

## Full-screen menu

`dm` with no arguments opens a full-screen, arrow-key menu (stdlib only, no curses, no deps):

```
dae-cli
──────────────────────────────────────────────
  订阅管理（列表 / 新增 / 删除）
  节点规则（选择哪些节点进代理组）
  编辑配置文件（global / dns / routing …）
  退出
↑↓ / j k 选择    Enter 确认    q 退出
```

- `↑ ↓` / `j k` move, `Home` / `End` / `g` / `G` jump, `Enter` selects, `q` / `Esc` / `Ctrl-C` leaves;
- **every list has a `← 返回` row at the bottom** and `q` / `Esc` is the same as picking it — you can
  always climb back one level, and finishing an action (write / delete / apply) drops you back at
  the list you came from instead of quitting the program;
- the screen is redrawn in place (cursor home + per-line clear), so nothing scrolls away and the
  previous menu does not pile up in your scrollback;
- subscription management is one flat list: pick a subscription, then
  `用它写进 50-group（选节点）` / `删除这条订阅`; `＋ 新增订阅` and `⟳ 刷新节点数` sit at the bottom;
- **no TTY (pipe, script, `dm | tee`) falls back to the numbered prompts automatically** —
  force it anywhere with `DAE_CLI_PLAIN=1`;
- the node-selection wizard (node numbers, ranges, regex) stays line-based on purpose: it needs
  free-text input, not a list.

## Managing subscriptions

`dm` with no arguments now opens a two-level menu; subscription management is the first entry:

```
=== dae-cli ===
  1) 订阅管理（列表 / 新增 / 删除）
  2) 节点规则（选择哪些节点进代理组）

=== 订阅管理 ===
    #  标签            节点  链接
    1.  kitty             44  https-file://m1.tohno-1.top/oosaka…
    2.  yy                44  http-file://127.0.0.1:18080/links.…
  1) 新增订阅   2) 删除订阅   3) 刷新节点数   0) 返回
```

The same thing without the menu:

```console
$ sudo dm sub list                     # tag / cached node count / masked URL
$ sudo dm sub add                      # prompts for tag + URL, then validates and reloads
$ sudo dm sub add myairport            # tag given, URL still prompted
$ sudo dm sub remove myairport
```

Design notes:

- **the URL is read from a prompt, never from argv** — otherwise it would sit in `ps`
  output and shell history;
- it is only ever printed **masked** (`scheme://host/first6…`);
- before every edit the file is copied next to itself (`20-subscription.dae.bak-<ts>`)
  and owner/mode are preserved; then `dae validate` runs and **rolls back** on failure;
- `add` appends to the `subscription { }` block that already exists (or creates
  `config.d/20-subscription.dae` when there is none);
- `remove` drops just that one line, and the block itself once it is empty; the cached
  node list and node rule for that tag are cleared too.

## Reload vs restart (why `dae reload` hangs)

`dae reload` re-reads the config and re-dials connections **inside the running process**. That
is fast and seamless as long as the node you are currently on still works. When you are
switching *because the current node is dead*, reload tries to tear down a connection that will
never come back and can block indefinitely — the switch looks like a hang and never lands.

So there are two ways to make a change take effect:

| | command | cost | when |
|---|---|---|---|
| hot reload | `sudo dm apply <tag> --reload` (menu default) | ~instant, no restart, no dropped connections | current node is healthy |
| restart service | `sudo dm apply <tag> --restart` | a few seconds offline, connections drop | current node is already dead, or reload hung last time |

Details:

- `dae reload` runs under a **20 s timeout** (`DEFAULT_RELOAD_TIMEOUT`). On timeout you get
  `当前节点多半已经不可用，热重载卡住了` plus the `--restart` hint instead of a frozen terminal;
- when a reload you asked for interactively times out, you are offered the restart right away;
  non-interactive runs fail with that message and a non-zero exit;
- `--restart` = `systemctl restart dae.service`, then `systemctl is-active` is checked and a
  non-`active` result is reported together with `journalctl -u dae.service -n 50`;
- the interactive menu asks every time it writes: `1) 热重载  2) 重启服务  0) 先不动`;
- `dm sub add|remove` and `dm edit` accept `--restart` as well.

## Editing the config files from the CLI

No file manager round trip:

```console
$ sudo dm edit                    # pick from a list
=== 编辑配置文件 ===
   1) config.dae            （没有已知的顶层块）        07-30 12:19
   2) 10-global.dae         监听 / 绑定 / 日志          09-22 00:08
   3) 20-subscription.dae   订阅链接（也可以直接用 dm sub） 11-24 15:14
   5) 40-dns.dae            DNS 规则 + 分流规则         09-15 11:04
   6) 60-routing.dae        分流规则                     10-23 21:51

选择 1-6（0 取消）[0]:

$ sudo dm edit routing            # or jump straight to one by (part of the) name
```

Behaviour worth knowing:

- editor resolution: `$EDITOR` → `$VISUAL` → the first of nvim / vim / vi / nano / micro / hx / kak;
- `sudo` strips `$EDITOR`, so the editor is started **as the user who ran sudo**
  (`SUDO_USER`, with their `HOME`) — your own `~/.config/nvim` applies, and because the
  config files are user-owned, an editor that saves by renaming cannot leave them
  `root:root`;
- the file is copied to `<name>.bak-<timestamp>` **before** the editor opens, and the
  backup is deleted again if you save nothing;
- on exit you get a `+N / -M 行` summary, then `dae validate` runs; if validation fails you
  are asked whether to roll back to that backup;
- the list only contains files the entry config actually includes, and generated files are
  filtered out (`50-group.dae` starts with `# Generated by dae-cli` — regenerate it with
  `dm apply` instead of editing it).

## Supported subscription formats

- Base64-encoded URI lists, plain URI lists
- SIP008 JSON
- VMess, VLESS, Trojan, Shadowsocks, SSR, TUIC, Hysteria, AnyTLS links

Clash YAML is not parsed on purpose: dae wants URI/Base64 or SIP008, so point the tool at
the provider's general-purpose subscription endpoint instead.

## Requirements

- Linux, `dae` or `daed` already configured
- Python 3, standard library only (no pip install)
- `sudo` + `visudo` (rejects the generated rule if it is malformed)

## Development

```console
$ make test     # python3 -m unittest -v test_dae_cli  (7 tests, stdlib only)
$ make check    # py_compile + sh -n + bash -n
```

## License

MIT
