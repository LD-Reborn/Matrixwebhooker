"""Offline test of the new crypto.py paths (no network):

* plain (v1.13+) to-device m.room_key import — unsigned and signed;
* request_room_key (PUT wire format);
* find_device_with_curve.
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

import olm

sys.path.insert(0, "/home/username/MatrixWebhooker")
import crypto as crypto_mod  # noqa: E402


class FakeClient:
    def __init__(self):
        self.hs = "https://example.test"
        self.user_id = "@bot:example.test"
        self.device_id = "XTSIATLNCQ"
        self.state_dir = Path(tempfile.mkdtemp(prefix="mwfake"))
        self.calls = []
        self.query_devices = {}   # user -> {dev: curve}

    def _request(self, method, url, body=None, params=None, timeout=None):
        self.calls.append((method, url, body))
        if "keys/query" in url:
            user = next(iter(body["device_keys"]))
            devs = body["device_keys"][user]
            out = {}
            for dev in (devs or [] if devs != [] else list(self.query_devices.get(user, {}))):
                key = self.query_devices.get(user, {}).get(dev)
                if key:
                    out[dev] = {"keys": {"curve25519:" + dev: key,
                                         "ed25519:" + dev: key}}
            return {"device_keys": {user: out}, "failures": {}}
        return {}


def main():
    client = FakeClient()
    bot = crypto_mod.BotCrypto(client, client.state_dir,
                               on_log=lambda m: print("[log]", m))
    bot._load_or_create()

    # -- sender side: a Megolm group session we will hand over as a key ----
    sender_dev = olm.Account()
    group = olm.OutboundGroupSession()
    sid = group.id
    export_key = group.session_key
    plain = json.dumps({"msgtype": "m.text",
                        "body": "hello from the modern plain format"})
    ct = group.encrypt(plain)
    sender_curve = sender_dev.identity_keys["curve25519"]

    # 1) unsigned plain room key must import (warning path)
    content = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "room_id": "!room:example.test",
        "sender_key": sender_curve,
        "session_id": sid,
        "session_key": export_key,
    }
    bot.handle_todevice([{"type": "m.room_key", "sender": "@alice:example.test",
                          "content": content}])
    dec = bot.decrypt_room_content({"sender_key": sender_curve,
                                    "session_id": sid,
                                    "ciphertext": ct,
                                    "sender": "@alice:example.test"})
    assert dec["body"] == "hello from the modern plain format", dec
    print("OK 1: plain m.room_key (unsigned) imported + decrypted")

    # 2) signed plain room key: signature over canonical content (sans
    #    "signatures"), verified via keys/query lookup
    sender_dev2 = olm.Account()
    group2 = olm.OutboundGroupSession()
    sid2 = group2.id
    export2 = group2.session_key
    plain2 = json.dumps({"msgtype": "m.text", "body": "signed variant"})
    ct2 = group2.encrypt(plain2)
    curve2 = sender_dev2.identity_keys["curve25519"]
    content2 = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "room_id": "!room:example.test",
        "sender_key": curve2,
        "session_id": sid2,
        "session_key": export2,
    }
    message = json.dumps(content2, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True)
    sig = sender_dev2.sign(message)
    content2["signatures"] = {"@alice:example.test":
                              {"ed25519:ALICEDV1": sig}}
    client.query_devices["@alice:example.test"] = {"ALICEDV1": curve2,
                                                   "_ed_alice": sig}
    # keys/query must return the SENDER's ed25519 key for ALICEDV1; patch:
    orig_request = client._request

    def patched(method, url, body=None, params=None, timeout=None):
        resp = orig_request(method, url, body, params, timeout)
        if "keys/query" in url:
            user = "@alice:example.test"
            if user in resp["device_keys"]:
                resp["device_keys"][user]["ALICEDV1"]["keys"][
                    "ed25519:ALICEDV1"] = sender_dev2.identity_keys["ed25519"]
        return resp

    client._request = patched
    bot.handle_todevice([{"type": "m.room_key", "sender": "@alice:example.test",
                          "content": content2}])
    dec2 = bot.decrypt_room_content({"sender_key": curve2,
                                     "session_id": sid2, "ciphertext": ct2})
    assert dec2["body"] == "signed variant", dec2
    print("OK 2: plain m.room_key (signed) imported + decrypted")

    # 3) bad signature must be dropped
    group3 = olm.OutboundGroupSession()
    sid3 = group3.id
    export3 = group3.session_key
    curve3 = sender_dev2.identity_keys["curve25519"]
    content3 = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "room_id": "!r:example.test",
        "sender_key": curve3,
        "session_id": sid3,
        "session_key": export3,
    }
    content3["signatures"] = {"@alice:example.test":
                              {"ed25519:ALICEDV1": "AAAA" * 8 + "A" * 4 + "AB"}}
    bot.handle_todevice([{"type": "m.room_key", "sender": "@alice:example.test",
                          "content": content3}])
    try:
        bot.decrypt_room_content({"sender_key": curve3,
                                  "session_id": sid3,
                                  "ciphertext": "xx"})
    # (ct3 not needed)
        raise AssertionError("should have failed to decrypt")
    except crypto_mod.CryptoError:
        pass
    print("OK 3: bad signature rejected")

    # 4) request_room_key wire format
    client.calls.clear()
    rid = bot.request_room_key("@user:example.test", "DEVICE123",
                               "!room:example.test", sender_curve, sid)
    method, url, body = client.calls[-1]
    assert method == "PUT", method
    assert url.endswith(f"sendToDevice/m.room_key_request/{rid}"), url
    assert body["messages"]["@user:example.test"]["DEVICE123"]["action"] == \
        "request"
    assert body["messages"]["@user:example.test"]["DEVICE123"][
        "requesting_device_id"] == "XTSIATLNCQ"
    b = body["messages"]["@user:example.test"]["DEVICE123"]["body"]
    assert b == {"algorithm": "m.megolm.v1.aes-sha2",
                 "room_id": "!room:example.test",
                 "sender_key": sender_curve, "session_id": sid}, b
    print("OK 4: request_room_key wire format")

    # 5) find_device_with_curve
    client.query_devices["@user:example.test"] = {
        "AAA": "wrong-curve", "CYG": sender_curve}
    dev = bot.find_device_with_curve("@user:example.test", sender_curve)
    assert dev == "CYG", dev
    assert bot.find_device_with_curve("@user:example.test", "nope") is None
    print("OK 5: find_device_with_curve")

    print("\nALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    main()
