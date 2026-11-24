#!/usr/bin/env python3
"""Convert Clash/Mihomo AnyTLS and TUIC proxies to daed share links."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import quote, urlencode

import yaml


def encode(value: Any) -> str:
    return quote(str(value), safe="")


def host_port(server: Any, port: Any) -> str:
    host = str(server).strip()
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    return f"{host}:{int(port)}"


def query_string(items: list[tuple[str, Any]]) -> str:
    values = [(key, str(value)) for key, value in items if value not in (None, "")]
    return f"?{urlencode(values)}" if values else ""


def convert_anytls(proxy: dict[str, Any]) -> str:
    password = proxy["password"]
    address = host_port(proxy["server"], proxy["port"])
    params: list[tuple[str, Any]] = []
    if proxy.get("sni"):
        params.append(("sni", proxy["sni"]))
    if proxy.get("skip-cert-verify") is True:
        params.append(("insecure", "1"))
    return (
        f"anytls://{encode(password)}@{address}"
        f"{query_string(params)}#{encode(proxy.get('name', 'AnyTLS'))}"
    )


def convert_tuic(proxy: dict[str, Any]) -> str:
    user = proxy["uuid"]
    password = proxy["password"]
    address = host_port(proxy["server"], proxy["port"])
    params: list[tuple[str, Any]] = []

    if proxy.get("sni"):
        params.append(("sni", proxy["sni"]))
    if proxy.get("skip-cert-verify") is True:
        params.append(("allow_insecure", "1"))
    if proxy.get("disable-sni") is True:
        params.append(("disable_sni", "1"))
    if proxy.get("congestion-controller"):
        params.append(("congestion_control", proxy["congestion-controller"]))
    if proxy.get("alpn"):
        alpn = proxy["alpn"]
        params.append(("alpn", ",".join(map(str, alpn)) if isinstance(alpn, list) else alpn))
    if proxy.get("udp-relay-mode"):
        params.append(("udp_relay_mode", proxy["udp-relay-mode"]))

    return (
        f"tuic://{encode(user)}:{encode(password)}@{address}"
        f"{query_string(params)}#{encode(proxy.get('name', 'TUIC'))}"
    )


CONVERTERS = {
    "anytls": convert_anytls,
    "tuic": convert_tuic,
}


def secure_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Clash/Mihomo AnyTLS and TUIC nodes to daed links."
    )
    parser.add_argument("input", nargs="?", default="Config.yaml", help="input YAML file")
    parser.add_argument(
        "--output",
        "-o",
        default="daed-links.txt",
        help="plain one-link-per-line output",
    )
    parser.add_argument(
        "--base64-output",
        "-b",
        default="daed-subscription.txt",
        help="Base64 subscription output",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    try:
        data = yaml.safe_load(input_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"读取 YAML 失败：{exc}", file=sys.stderr)
        return 1

    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(proxies, list):
        print("配置中没有找到 proxies 列表。", file=sys.stderr)
        return 1

    links: list[str] = []
    counts = {name: 0 for name in CONVERTERS}
    skipped: dict[str, int] = {}
    errors: list[str] = []

    for index, proxy in enumerate(proxies, start=1):
        if not isinstance(proxy, dict):
            errors.append(f"第 {index} 个节点不是对象")
            continue
        protocol = str(proxy.get("type", "")).lower()
        converter = CONVERTERS.get(protocol)
        if converter is None:
            skipped[protocol or "unknown"] = skipped.get(protocol or "unknown", 0) + 1
            continue
        try:
            links.append(converter(proxy))
            counts[protocol] += 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{proxy.get('name', f'节点 {index}')}：缺少或无效字段 {exc}")

    if not links:
        print("没有生成任何受支持的节点。", file=sys.stderr)
        return 1

    plain = "\n".join(links) + "\n"
    encoded = base64.b64encode(plain.encode("utf-8")).decode("ascii") + "\n"
    output_path = Path(args.output).expanduser()
    base64_path = Path(args.base64_output).expanduser()
    secure_write(output_path, plain)
    secure_write(base64_path, encoded)

    summary = ", ".join(f"{name}={count}" for name, count in counts.items() if count)
    print(f"转换完成：{len(links)} 个节点（{summary}）")
    print(f"逐行链接：{output_path.resolve()}")
    print(f"Base64 订阅：{base64_path.resolve()}")
    if skipped:
        print("跳过的协议：" + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    if errors:
        print(f"有 {len(errors)} 个节点转换失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
