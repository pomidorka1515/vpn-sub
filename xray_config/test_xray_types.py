#!/usr/bin/env python3
"""Test round-trip serialization of XrayConfig with HTTPUserObject."""

import json
from xray_types import (
    XrayConfig, InboundObject, HTTPUserObject, HTTPInboundConfigurationObject,
    InboundStreamSettingsObject, TLSObject, SniffingObject,
    from_dict, to_dict,
)

# Minimal inbound config - port as string to avoid Port() constructor issues
SAMPLE_CONFIG = {
    "log": {"loglevel": "warning"},
    "inbounds": [
        {
            "tag": "http-in",
            "listen": "0.0.0.0",
            "port": "8080",
            "protocol": "http",
            "settings": {
                "users": [
                    {"user": "alice", "pass": "secret123"},
                    {"user": "bob", "pass": "hunter2"},
                ],
                "allowTransparent": False,
            },
            "streamSettings": {
                "network": "tcp",
                "security": "tls",
                "tlsSettings": {
                    "serverName": "example.com",
                    "alpn": ["h2", "http/1.1"],
                },
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["https", "tls"],
            },
        }
    ],
}


def main():
    # Round-trip: JSON -> dataclass -> JSON
    cfg = from_dict(XrayConfig, SAMPLE_CONFIG)
    out = to_dict(cfg)

    # Verify the 'pass' key survived round-trip
    users = cfg.inbounds[0].settings.users
    print(f"Loaded {len(users)} users:")
    for u in users:
        print(f"  {u.user}: {u._pass}")

    # Verify JSON output has 'pass' not '_pass'
    assert "pass" in json.dumps(out), "Expected 'pass' in JSON output"
    assert "_pass" not in json.dumps(out), "Unexpected '_pass' in JSON output"

    # Verify the values survived
    assert users[0].user == "alice"
    assert users[0]._pass == "secret123"

    print("\nJSON output (pretty):")
    print(json.dumps(out, indent=2))
    print("\nRound-trip OK!")


if __name__ == "__main__":
    main()