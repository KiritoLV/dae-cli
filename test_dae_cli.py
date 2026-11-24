import argparse
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dae_cli


class DaeCliTests(unittest.TestCase):
    def test_parse_base64_nodes(self):
        vmess = base64.urlsafe_b64encode(json.dumps({"ps": "Hong Kong 01"}).encode()).decode().rstrip("=")
        links = f"vmess://{vmess}\nvless://id@example.com:443#Japan%2002\n"
        subscription = base64.b64encode(links.encode())
        self.assertEqual(dae_cli.parse_nodes(subscription), ["Hong Kong 01", "Japan 02"])

    def test_parse_sip008(self):
        data = json.dumps({"servers": [{"remarks": "HK"}, {"server": "example.com"}]}).encode()
        self.assertEqual(dae_cli.parse_nodes(data), ["HK", "example.com"])

    def test_parse_plain_anytls_and_tuic_nodes(self):
        data = (
            "anytls://password@hk.example:443?sni=hk.example#%F0%9F%87%AD%F0%9F%87%B0%20%E9%A6%99%E6%B8%AF%2001\n"
            "tuic://id:password@sg.example:443?sni=sg.example#%F0%9F%87%B8%F0%9F%87%AC%20%E6%96%B0%E5%8A%A0%E5%9D%A1%2001\n"
            "tuic://id:password@unnamed.example:443?alpn=h3\n"
        ).encode()
        self.assertEqual(
            dae_cli.parse_nodes(data),
            ["🇭🇰 香港 01", "🇸🇬 新加坡 01", "unnamed.example"],
        )

    def test_read_subscription_from_include(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.d").mkdir()
            (root / "config.dae").write_text("include { config.d/*.dae }\n", encoding="utf-8")
            (root / "config.d" / "sub.dae").write_text(
                "subscription {\n  airport: 'https-file://example.com/sub#token' # comment\n}\n",
                encoding="utf-8",
            )
            result = dae_cli.read_subscriptions(root / "config.dae")
            self.assertEqual(result["airport"]["url"], "https-file://example.com/sub#token")

    def test_filter_rendering(self):
        rule = {"mode": "exclude", "names": ["HK 01", 'quote " node']}
        rendered = dae_cli.render_filter("airport", rule)
        self.assertEqual(
            rendered,
            'subtag("airport") && !name("HK 01", "quote \\" node")',
        )

    def test_select_numbers_ranges_and_regex(self):
        nodes = ["HK 01", "HK 02", "JP 01", "US 01"]
        self.assertEqual(
            dae_cli.select_names(nodes, ["3-4"], ["^HK"]),
            ["HK 01", "HK 02", "JP 01", "US 01"],
        )

    def test_interactive_defaults_to_proxy_and_50_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subs").mkdir()
            links = base64.b64encode(b"vless://id@example.com:443#HK%2001\n")
            (root / "subs" / "nodes.sub").write_bytes(links)
            (root / "config.dae").write_text(
                "subscription { airport: 'file://subs/nodes.sub' }\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config=str(root / "config.dae"),
                state=str(root / "state.json"),
                dae_bin="dae",
            )
            answers = iter(["1", "2", "1", "", "y", "n"])
            with mock.patch("builtins.input", side_effect=lambda _: next(answers)):
                with mock.patch("dae_cli.command_apply") as apply:
                    state = dae_cli.empty_state()
                    dae_cli.command_interactive(args, state)
            self.assertEqual(state["rules"]["airport"], {"mode": "exclude", "names": ["HK 01"]})
            self.assertEqual(args.group, "proxy")
            self.assertEqual(args.output, "/etc/dae/config.d/50-group.dae")
            self.assertFalse(args.reload)
            apply.assert_called_once_with(args, state)


if __name__ == "__main__":
    unittest.main()
