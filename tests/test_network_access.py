import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import settings  # noqa: E402


class NetworkAccessTests(unittest.TestCase):
    def test_interface_discovery_keeps_only_private_lan_and_tailnet_ipv4(self):
        raw = json.dumps([
            {"ifname": "lo", "operstate": "UNKNOWN", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
            {"ifname": "eth0", "operstate": "UP", "addr_info": [
                {"family": "inet", "local": "192.168.1.20", "prefixlen": 24},
                {"family": "inet", "local": "8.8.8.8", "prefixlen": 24},
            ]},
            {"ifname": "tailscale0", "operstate": "UNKNOWN", "addr_info": [
                {"family": "inet", "local": "100.77.4.12", "prefixlen": 32},
            ]},
            {"ifname": "down0", "operstate": "DOWN", "addr_info": [{"family": "inet", "local": "10.0.0.9"}]},
        ])
        self.assertEqual(settings.parse_host_addresses(raw), [
            {"interface": "eth0", "address": "192.168.1.20", "kind": "lan"},
            {"interface": "tailscale0", "address": "100.77.4.12", "kind": "tailnet"},
        ])

    def test_network_scopes_build_only_explicit_private_listeners(self):
        addresses = [
            {"interface": "eth0", "address": "192.168.1.20", "kind": "lan"},
            {"interface": "tailscale0", "address": "100.77.4.12", "kind": "tailnet"},
        ]
        self.assertEqual(settings.network_scope_listen("local", addresses), "local")
        self.assertEqual(settings.network_scope_listen("tailnet", addresses), "tailscale")
        self.assertEqual(settings.network_scope_listen("lan-tailnet", addresses), "local,100.77.4.12,192.168.1.20")
        with self.assertRaises(settings.SettingsError):
            settings.network_scope_listen("lan-tailnet", [addresses[0]])
        with self.assertRaises(settings.SettingsError):
            settings.network_scope_listen("all", addresses)

    def test_settings_network_scope_only_passes_selected_scope_to_service_apply(self):
        addresses = [
            {"interface": "eth0", "address": "192.168.1.20", "kind": "lan"},
            {"interface": "tailscale0", "address": "100.77.4.12", "kind": "tailnet"},
        ]
        ctx = SimpleNamespace(store=SimpleNamespace(interval=5))
        args = SimpleNamespace(demo=True, port=9092, state_dir="")
        instance = settings.Settings(ctx, args)
        instance.host_network_addresses = lambda: addresses
        instance.apply_service = lambda body: body

        self.assertEqual(instance.apply_network_scope({"scope": "lan-tailnet"}), {
            "listen": "local,100.77.4.12,192.168.1.20", "port": 9092, "interval": 5,
        })


if __name__ == "__main__":
    unittest.main()
