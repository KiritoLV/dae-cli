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
| `apply <sub> [--reload]` | write the group file; `--reload` restarts dae |

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
