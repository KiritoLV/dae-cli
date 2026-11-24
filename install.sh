#!/usr/bin/env bash
# dae-cli installer: installs the CLI plus a root-owned, whitelisted entry point
# and grants exactly ONE passwordless sudo command to one user.
#
#   sudo ./install.sh                 # install for $SUDO_USER
#   sudo ./install.sh --uninstall     # remove everything this script installed
#          ./install.sh --dry-run     # print what would happen (no root needed)
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly LIB_DIR="/usr/local/libexec/dae-cli"
readonly CLI="$LIB_DIR/dae_cli.py"
readonly ENTRY="/usr/local/sbin/dae-cli-menu"
readonly SUDOERS_DIR="/etc/sudoers.d"

ACTION=install
DRY_RUN=0
TARGET_USER="${SUDO_USER:-}"

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--uninstall] [--dry-run]

  (no option)   install dae-cli for $SUDO_USER
  --uninstall   remove the entry point, the library and the sudoers rule
  --dry-run     print the actions without touching the system (no root needed)

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

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION=uninstall ;;
        --dry-run)   DRY_RUN=1 ;;
        -h|--help)   usage; exit 0 ;;
        *) die "unknown argument: $arg (see --help)" ;;
    esac
done

command -v visudo >/dev/null 2>&1 || die "visudo not found (install sudo)"

if [[ $DRY_RUN -eq 0 ]]; then
    [[ ${EUID} -eq 0 ]] || die "please run with sudo: sudo $0"
fi

if [[ -z $TARGET_USER || $TARGET_USER == root ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
        TARGET_USER="${DAE_CLI_USER:-${USER:-$(id -un)}}"
        say "no SUDO_USER (dry-run): assuming target user '$TARGET_USER'"
    else
        die "SUDO_USER is not set; run it as: sudo $0   (not from a root shell)"
    fi
fi

[[ $TARGET_USER =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "refusing suspicious user name: $TARGET_USER"
id "$TARGET_USER" >/dev/null 2>&1 || say "warning: user '$TARGET_USER' does not exist yet"

readonly SUDOERS_FILE="$SUDOERS_DIR/dae-cli-$TARGET_USER"

if [[ $ACTION == uninstall ]]; then
    say "removing $ENTRY"
    run rm -f "$ENTRY"
    say "removing $LIB_DIR"
    run rm -rf "$LIB_DIR"
    say "removing $SUDOERS_FILE"
    run rm -f "$SUDOERS_FILE"
    say "done. 'dm' will stop working in new shells (restart the shell to drop the alias/function)."
    exit 0
fi

say "installing CLI to $LIB_DIR"
run install -d -o root -g root -m 0755 "$LIB_DIR"
run install -o root -g root -m 0755 "$SCRIPT_DIR/dae_cli.py" "$CLI"

say "installing entry point to $ENTRY"
run install -o root -g root -m 0755 "$SCRIPT_DIR/dae-cli-menu" "$ENTRY"

say "writing sudoers rule for '$TARGET_USER' to $SUDOERS_FILE"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat >"$tmp" <<EOF
# Managed by dae-cli install.sh -- allow ONLY the fixed, root-owned entry point.
# The user cannot edit what this path does; edit rights would mean root.
$TARGET_USER ALL=(root) NOPASSWD: $ENTRY
EOF
visudo -cf "$tmp" >/dev/null || die "generated sudoers rule failed validation: $tmp"
run install -o root -g root -m 0440 "$tmp" "$SUDOERS_FILE"
if [[ $DRY_RUN -eq 0 ]]; then
    visudo -cf "$SUDOERS_FILE" >/dev/null && say "sudoers rule is valid"
else
    say "(dry-run) skipped final validation of $SUDOERS_FILE"
fi

cat <<EOF

Installed. Add the short command to your shell (zsh / bash):

    dm() { sudo -n /usr/local/sbin/dae-cli-menu "\$@"; }

then:

    source ~/.zshrc      # or ~/.bashrc
    dm help
    dm subs

The CLI turns root writes into /etc/dae/config.d/50-group.dae only.  The state
file lives in YOUR home (~/.config/dae-cli/state.json) even when run via sudo,
and it stores node display names -- treat it as private.
EOF
