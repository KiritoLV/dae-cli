#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_NAME="daed-subscription"
readonly PORT="${DAED_SUB_PORT:-18080}"
readonly DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/daed-sub-manager"
readonly WEB_DIR="$DATA_DIR/www"
readonly CONFIG_FILE="$DATA_DIR/Config.yaml"
readonly URL_FILE="$DATA_DIR/subscription.url"
readonly LINKS_FILE="$WEB_DIR/links.txt"
readonly BASE64_FILE="$WEB_DIR/subscription.txt"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly UNIT_FILE="$UNIT_DIR/$APP_NAME.service"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly CONVERTER="$SCRIPT_DIR/clash_yaml_to_daed.py"
readonly SUB_URL="http://127.0.0.1:$PORT/links.txt"

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

prepare_dirs() {
    mkdir -p -- "$DATA_DIR" "$WEB_DIR" "$UNIT_DIR"
    chmod 700 -- "$DATA_DIR" "$WEB_DIR"
}

install_service() {
    prepare_dirs
    local desired
    desired="$(cat <<EOF
[Unit]
Description=Local HTTP server for daed subscription
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m http.server $PORT --bind 127.0.0.1 --directory $WEB_DIR
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
EOF
)"
    if [[ ! -f "$UNIT_FILE" ]] || [[ "$(<"$UNIT_FILE")" != "$desired" ]]; then
        printf '%s\n' "$desired" >"$UNIT_FILE"
        chmod 600 -- "$UNIT_FILE"
        systemctl --user daemon-reload
    fi
}

convert_from_url() {
    prepare_dirs
    [[ -x "$CONVERTER" ]] || die "找不到转换器：$CONVERTER"

    local url="${1:-}"
    if [[ -z "$url" ]]; then
        printf '请输入 Clash/Mihomo 订阅链接（输入内容不会回显）：\n'
        IFS= read -r -s url
        printf '\n'
    fi
    [[ "$url" == http://* || "$url" == https://* ]] || die "订阅链接必须以 http:// 或 https:// 开头"

    local temp_dir
    temp_dir="$(mktemp -d "$DATA_DIR/update.XXXXXX")"
    trap 'rm -rf -- "$temp_dir"' RETURN

    printf '正在下载订阅……\n'
    curl --fail --location --silent --show-error \
        --retry 2 --connect-timeout 15 --max-time 90 \
        --user-agent 'clash-verge-rev' \
        --output "$temp_dir/Config.yaml" -- "$url"
    chmod 600 -- "$temp_dir/Config.yaml"

    printf '正在转换节点……\n'
    "$CONVERTER" "$temp_dir/Config.yaml" \
        --output "$temp_dir/links.txt" \
        --base64-output "$temp_dir/subscription.txt"

    mv -f -- "$temp_dir/Config.yaml" "$CONFIG_FILE"
    mv -f -- "$temp_dir/links.txt" "$LINKS_FILE"
    mv -f -- "$temp_dir/subscription.txt" "$BASE64_FILE"
    printf '%s\n' "$url" >"$URL_FILE"
    chmod 600 -- "$CONFIG_FILE" "$URL_FILE" "$LINKS_FILE" "$BASE64_FILE"
    trap - RETURN
    rm -rf -- "$temp_dir"

    printf '\n转换成功。daed 订阅地址：\n%s\n' "$SUB_URL"
    if service_running; then
        printf 'HTTP 服务正在运行，daed 现在可以更新订阅。\n'
    else
        printf 'HTTP 服务尚未启动，请选择“启动服务”。\n'
    fi
}

masked_bound_url() {
    if [[ ! -s "$URL_FILE" ]]; then
        printf '尚未绑定'
        return
    fi
    local url origin
    IFS= read -r url <"$URL_FILE"
    if [[ "$url" =~ ^(https?://[^/]+) ]]; then
        origin="${BASH_REMATCH[1]}"
        printf '%s/…（路径和参数已隐藏）' "$origin"
    else
        printf '已绑定（内容已隐藏）'
    fi
}

show_bound_url() {
    printf '当前绑定：%s\n' "$(masked_bound_url)"
}

update_bound() {
    [[ -s "$URL_FILE" ]] || die "尚未绑定订阅链接，请先选择“绑定或更换订阅链接”"
    local url
    IFS= read -r url <"$URL_FILE"
    convert_from_url "$url"
}

service_running() {
    systemctl --user is-active --quiet "$APP_NAME.service" 2>/dev/null
}

start_service() {
    install_service
    [[ -s "$LINKS_FILE" ]] || die "尚未生成 links.txt，请先转换订阅"
    systemctl --user enable --now "$APP_NAME.service"
    printf 'HTTP 服务已启动：%s\n' "$SUB_URL"
}

stop_service() {
    install_service
    systemctl --user disable --now "$APP_NAME.service" >/dev/null 2>&1 || true
    printf 'HTTP 服务已停止，端口 %s 已关闭。\n' "$PORT"
}

restart_service() {
    install_service
    [[ -s "$LINKS_FILE" ]] || die "尚未生成 links.txt，请先转换订阅"
    systemctl --user enable "$APP_NAME.service" >/dev/null
    systemctl --user restart "$APP_NAME.service"
    printf 'HTTP 服务已重启：%s\n' "$SUB_URL"
}

show_status() {
    show_bound_url
    printf '订阅地址：%s\n' "$SUB_URL"
    if [[ -s "$LINKS_FILE" ]]; then
        printf '节点数量：%s\n' "$(wc -l <"$LINKS_FILE")"
        printf '订阅文件：%s\n' "$LINKS_FILE"
    else
        printf '订阅文件：尚未生成\n'
    fi
    if service_running; then
        printf 'HTTP 服务：运行中\n'
        if curl --fail --silent --max-time 3 --output /dev/null -- "$SUB_URL"; then
            printf '访问测试：正常\n'
        else
            printf '访问测试：失败，请尝试重启服务\n'
        fi
    else
        printf 'HTTP 服务：已停止\n'
    fi
}

update_and_start() {
    if [[ -n "${1:-}" ]]; then
        convert_from_url "$1"
    elif [[ -s "$URL_FILE" ]]; then
        update_bound
    else
        convert_from_url
    fi
    start_service
}

show_menu() {
    while true; do
        printf '\n=== daed 本地订阅管理 ===\n'
        printf '当前绑定：%s\n' "$(masked_bound_url)"
        printf '1) 绑定或更换订阅链接并更新\n'
        printf '2) 更新当前绑定的订阅\n'
        printf '3) 更新当前订阅并启动 HTTP 服务\n'
        printf '4) 启动 HTTP 服务\n'
        printf '5) 停止 HTTP 服务\n'
        printf '6) 重启 HTTP 服务\n'
        printf '7) 查看详细状态\n'
        printf '0) 退出\n'
        printf '请选择：'
        IFS= read -r choice
        case "$choice" in
            1) convert_from_url ;;
            2) update_bound ;;
            3) update_and_start ;;
            4) start_service ;;
            5) stop_service ;;
            6) restart_service ;;
            7) show_status ;;
            0) return 0 ;;
            *) printf '无效选项，请重新输入。\n' ;;
        esac
    done
}

usage() {
    cat <<EOF
用法：
  $(basename "$0")                  打开交互菜单
  $(basename "$0") bind [URL]       绑定或更换链接并更新
  $(basename "$0") update           更新当前绑定的订阅
  $(basename "$0") up [URL]         更新订阅并启动服务
  $(basename "$0") start            启动 HTTP 服务
  $(basename "$0") stop             停止 HTTP 服务
  $(basename "$0") restart          重启 HTTP 服务
  $(basename "$0") status           查看状态

绑定的链接以 600 权限保存在本地。省略 URL 时会安全地交互输入，
避免链接进入 shell 历史；状态页面只会脱敏显示链接。
EOF
}

main() {
    need_command curl
    need_command python3
    need_command systemctl
    prepare_dirs

    case "${1:-menu}" in
        menu) show_menu ;;
        bind) convert_from_url "${2:-}" ;;
        update) update_bound ;;
        up) update_and_start "${2:-}" ;;
        start) start_service ;;
        stop) stop_service ;;
        restart) restart_service ;;
        status) show_status ;;
        help|-h|--help) usage ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
