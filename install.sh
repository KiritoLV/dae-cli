#!/usr/bin/env bash
# dae-cli installer: installs the CLI plus a root-owned, whitelisted entry point
# and grants exactly ONE passwordless sudo command to one user.
#
#   sudo ./install.sh                 # interactive menu (install / uninstall / status)
#   sudo ./install.sh --install       # non-interactive install
#   sudo ./install.sh --uninstall     # non-interactive uninstall
#          ./install.sh --status      # show what is installed (no root needed)
#          ./install.sh --dry-run     # print every action, touch nothing (no root needed)
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly LIB_DIR="/usr/local/libexec/dae-cli"
readonly CLI="$LIB_DIR/dae_cli.py"
readonly ENTRY="/usr/local/sbin/dae-cli-menu"
readonly SUDOERS_DIR="/etc/sudoers.d"

ACTION=""          # install / uninstall / status
DRY_RUN=0
TARGET_USER="${SUDO_USER:-}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--install|--uninstall|--status] [--dry-run]

  (no option)     interactive menu: install / uninstall / status
  --install       install for $SUDO_USER (non-interactive)
  --uninstall     remove the entry point, the library and the sudoers rule
  --status        print what is currently installed
  --dry-run       print the actions without touching the system (no root needed)

What gets installed

  /usr/local/libexec/dae-cli/dae_cli.py   the CLI itself (root-owned, 0755)
  /usr/local/sbin/dae-cli-menu            root-owned entry point, subcommand
                                          allowlist, rejects every other option
  /etc/sudoers.d/dae-cli-<user>           NOPASSWD for that one path only

Why a wrapper: sudo must never execute a script the user can edit, otherwise
the user can edit it into "run my shell as root".  The wrapper is root-owned,
so the user cannot change what it does; the allowlist stops the CLI's own
root-only file-writing options (--output, --state, --config, --dae-bin) from
being reached through sudo.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --install)    ACTION=install ;;
        --uninstall)  ACTION=uninstall ;;
        --status)     ACTION=status ;;
        --dry-run)    DRY_RUN=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) die "unknown argument: $arg (see --help)" ;;
    esac
done

# ---------------------------------------------------------------- target user
resolve_user() {
    # SUDO_USER is the normal path (sudo ./install.sh); the rest are fallbacks so
    # that --status / --dry-run / the menu still show a sensible target name.
    if [[ -n ${TARGET_USER:-} && ${TARGET_USER} != root ]]; then
        return 0
    fi
    if [[ -n ${DAE_CLI_USER:-} && ${DAE_CLI_USER} != root ]]; then
        TARGET_USER="$DAE_CLI_USER"; return 0
    fi
    if [[ -n ${USER:-} && ${USER} != root ]]; then
        TARGET_USER="$USER"; return 0
    fi
    die "cannot determine the target user; set DAE_CLI_USER=<user> or run with sudo"
}

# -------------------------------------------------------------------- status
do_status() {
    resolve_user
    local sudoers_file="$SUDOERS_DIR/dae-cli-$TARGET_USER"
    say "target user: $TARGET_USER"

    local f found=0 unreadable_dir=0
    [[ -x $SUDOERS_DIR ]] || unreadable_dir=1     # 普通用户连目录都进不去
    for f in "$ENTRY" "$CLI" "$sudoers_file"; do
        if [[ -e $f ]]; then
            found=$((found + 1))
            printf '    [ok]      %s\n' "$f"
        elif [[ $f == "$sudoers_file" && $unreadable_dir -eq 1 ]]; then
            printf '    [?]       %s  (sudoers.d 普通用户不可读，看下面的免密测试)\n' "$f"
        else
            printf '    [missing] %s\n' "$f"
        fi
    done

    if [[ -r $CLI && -r "$SCRIPT_DIR/dae_cli.py" ]]; then
        local a b
        a="$(md5sum "$CLI" | cut -d' ' -f1)"
        b="$(md5sum "$SCRIPT_DIR/dae_cli.py" | cut -d' ' -f1)"
        if [[ $a == $b ]]; then
            note "已安装的 CLI 与仓库这份一致 ($a)"
        else
            note "警告：已安装 ($a) 与仓库 ($b) 不一致 —— 重新 install 一下"
        fi
    fi

    if command -v sudo >/dev/null 2>&1 && sudo -n "$ENTRY" help >/dev/null 2>&1; then
        note "免密 sudo 生效（sudo -n dae-cli-menu help 能跑通）"
    elif [[ $found -eq 3 ]]; then
        note "免密 sudo 未验证：试 sudo -n /usr/local/sbin/dae-cli-menu help"
    fi

    if [[ $found -eq 0 ]]; then
        note "什么都没装。运行 sudo ./install.sh 安装。"
    fi
}

# ------------------------------------------------------------------- install
do_install() {
    resolve_user
    [[ $TARGET_USER =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "refusing suspicious user name: $TARGET_USER"
    command -v visudo >/dev/null 2>&1 || die "visudo not found (install sudo)"
    id "$TARGET_USER" >/dev/null 2>&1 || say "warning: user '$TARGET_USER' does not exist yet"

    local sudoers_file="$SUDOERS_DIR/dae-cli-$TARGET_USER"

    say "installing CLI to $LIB_DIR"
    run install -d -o root -g root -m 0755 "$LIB_DIR"
    run install -o root -g root -m 0755 "$SCRIPT_DIR/dae_cli.py" "$CLI"

    say "installing entry point to $ENTRY"
    run install -o root -g root -m 0755 "$SCRIPT_DIR/dae-cli-menu" "$ENTRY"

    say "writing sudoers rule for '$TARGET_USER' to $sudoers_file"
    local tmp
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' RETURN
    cat >"$tmp" <<EOF
# Managed by dae-cli install.sh -- allow ONLY the fixed, root-owned entry point.
# The user cannot edit what this path does; edit rights would mean root.
$TARGET_USER ALL=(root) NOPASSWD: $ENTRY
EOF
    visudo -cf "$tmp" >/dev/null || die "generated sudoers rule failed validation: $tmp"
    run install -o root -g root -m 0440 "$tmp" "$sudoers_file"
    if [[ $DRY_RUN -eq 0 ]]; then
        visudo -cf "$sudoers_file" >/dev/null && say "sudoers rule is valid"
    else
        say "(dry-run) skipped final validation of $sudoers_file"
    fi

    cat <<EOF

Installed. Add the short command to your shell (zsh / bash):

    dm() { sudo -n /usr/local/sbin/dae-cli-menu "\$@"; }

then:

    source ~/.zshrc      # or ~/.bashrc
    dm help
    dm subs

The state file lives in YOUR home (~/.config/dae-cli/state.json) even when run
via sudo, and it stores node display names -- treat it as private.
EOF
}

# ----------------------------------------------------------------- uninstall
do_uninstall() {
    resolve_user
    local sudoers_file="$SUDOERS_DIR/dae-cli-$TARGET_USER"
    say "removing $ENTRY"
    run rm -f "$ENTRY"
    say "removing $LIB_DIR"
    run rm -rf "$LIB_DIR"
    say "removing $sudoers_file"
    run rm -f "$sudoers_file"
    cat <<EOF

Removed. Kept on purpose:
    /etc/dae/...                      dae config and the generated group file
    ~/.config/dae-cli/state.json      cached node names   (rm -rf ~/.config/dae-cli)
    the dm() function in ~/.zshrc     harmless once the wrapper is gone

'restored to all nodes' note: nothing rewrote your dae config during removal.
EOF
}

# ---------------------------------------------------------------------- menu
menu() {
    resolve_user
    local installed="未安装" choice
    if [[ -e $ENTRY ]]; then installed="已安装"; fi

    printf '\n  dae-cli 安装器    目标用户: %s   当前状态: %s\n\n' "$TARGET_USER" "$installed"
    printf '    1) 安装      install    (root-owned wrapper + 受限 sudoers 规则)\n'
    printf '    2) 卸载      uninstall  (删入口 + 库 + sudoers，保留 dae 配置与 state)\n'
    printf '    3) 查看状态  status     (装了什么、md5 是否一致、免密是否生效)\n'
    printf '    q) 退出      quit\n\n'
    printf '  选择 [1]: '
    read -r choice || choice=""
    printf '\n'
    case "${choice:-1}" in
        1|i|install)    ACTION=install ;;
        2|u|uninstall)  ACTION=uninstall ;;
        3|s|status)     ACTION=status ;;
        q|Q|quit|exit)  printf '  bye\n'; exit 0 ;;
        *) die "unknown choice: $choice" ;;
    esac
}

# ---------------------------------------------------------------------- main
if [[ -z $ACTION ]]; then
    if [[ -t 0 || $DRY_RUN -eq 1 ]]; then
        menu
    else
        die "no arguments and no tty; use --install / --uninstall / --status"
    fi
fi

if [[ $DRY_RUN -eq 0 ]]; then
    if [[ $ACTION != status ]]; then
        [[ ${EUID} -eq 0 ]] || die "please run with sudo: sudo $0"
    fi
fi

case "$ACTION" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
esac
