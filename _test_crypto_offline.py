"""Offline round-trip test of crypto.py (no network).

Simulates another Matrix client ("alice"): alice has an olm Account, creates
outbound group sessions, shares their raw keys with us via the to-device Olm
envelope flow, and sends encrypted room messages. Our side is BotCrypto,
driven by the exact same event dicts the watcher would feed it.

Protocol note: per the Matrix spec the sender shares the *raw* group
session key (44-char base64) when the group is created, before sending
messages with it — the constructor import path in libolm handles exactly
that. (The index-dependent "session key" export blob returned by the
binding after encrypting is the SAS/backup export format and is not what
room key sharing uses.)
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

import olm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crypto import BotCrypto, CryptoError  # noqa: E402

ALICE_USER = "@alice:matrix.example"


class FakeClient:
    hs = "https://matrix.example"
    user_id = "@bot:matrix.example"
    device_id = "TESTDEV"
    uploaded = {}

    def _request(self, method, url, body=None, **kw):
        if url.endswith("/keys/upload"):
            self.uploaded = body
        return {}


tmp = Path(tempfile.mkdtemp(prefix="cryptotest"))
client = FakeClient()
log = []
bot = BotCrypto(client, tmp, on_log=log.append)

print("== 1) device registration (keys/upload) ==")
bot._load_or_create()
assert client.uploaded, "keys/upload was not called"
dk = client.uploaded["device_keys"]
assert dk["user_id"] == "@bot:matrix.example"
assert dk["signatures"]["@bot:matrix.example"][f"ed25519:{client.device_id}"]
otks = client.uploaded["one_time_keys"]
assert otks, "no one-time keys uploaded"
# signed_curve25519 form (MSC3827): each with its own signature
assert all(k.startswith("signed_curve25519:") for k in otks), list(otks)[:3]
sample = next(iter(otks.values()))
assert "key" in sample and set(sample) == {"key", "signatures"}
print(f"uploaded device keys + {len(otks)} signed OTKs; OK")

our_identity = dk["keys"][f"curve25519:{client.device_id}"]
# Alice will use ONE of OUR one-time keys to send us the first (prekey)
# message — exactly what a real client does via /keys/query + /keys/claim.
alice = olm.Account()
otk_val = next(iter(otks.values()))["key"]

g_out = olm.OutboundGroupSession()
g2_out = olm.OutboundGroupSession()
# Keys must be captured BEFORE any encryption with the group: the shared
# form (what Element/js-sdk also shares via olm_outbound_group_session_key)
# is consumed by InboundGroupSession(...), exactly the protocol round-trip.
g1_key = g_out.session_key
g2_key = g2_out.session_key
assert len(g1_key) >= 43, f"implausible key len {len(g1_key)}"
group1 = {
    "algorithm": "m.megolm.v1.aes-sha2",
    "room_id": "!testroom:example",
    "sender_key": alice.identity_keys["curve25519"],
    "session_id": g_out.id,
    "session_key": g1_key,
}
group2 = {
    "algorithm": "m.megolm.v1.aes-sha2",
    "room_id": "!room2:example",
    "sender_key": alice.identity_keys["curve25519"],
    "session_id": g2_out.id,
    "session_key": g2_key,
}

print("== 2) alice -> us: prekey olm to-device carrying m.room_key ==")
out_s = olm.OutboundSession(alice, our_identity, otk_val)
wrapped1 = out_s.encrypt(json.dumps({"m.room_key": group1})).ciphertext
bot.handle_todevice([{
    "type": "m.olm",
    "sender": ALICE_USER,
    "content": {"sender_key": alice.identity_keys["curve25519"],
                "content": {"type": 0, "ciphertext": wrapped1}},
}])
assert (alice.identity_keys["curve25519"], g_out.id) in bot._groups, \
    f"groups: {list(bot._groups)}"
print("olm session established; room key #1 imported; OK")

print("== 3) alice -> us: plain olm to-device (megolm type event) ==")
wrapped2 = out_s.encrypt(json.dumps({"m.room_key": group2})).ciphertext
bot.handle_todevice([{
    "type": "m.megolm.v1.aes-sha2",
    "sender": ALICE_USER,
    "content": {"sender_key": alice.identity_keys["curve25519"],
                "ciphertext": wrapped2},
}])
assert (alice.identity_keys["curve25519"], g2_out.id) in bot._groups
print("room key #2 imported via existing session; OK")

print("== 4) decrypt room messages (both groups) ==")
ct1 = g_out.encrypt(json.dumps({"msgtype": "m.text", "body": "hi bot, can you decrypt me?"}))
p1 = bot.decrypt_room_content({
    "sender_key": alice.identity_keys["curve25519"],
    "session_id": g_out.id, "ciphertext": ct1})
assert p1["body"] == "hi bot, can you decrypt me?", p1
ct2 = g2_out.encrypt(json.dumps({"msgtype": "m.text", "body": "second room, still me?"}))
p2 = bot.decrypt_room_content({
    "sender_key": alice.identity_keys["curve25519"],
    "session_id": g2_out.id, "ciphertext": ct2})
assert p2["body"] == "second room, still me?", p2
print(f"decrypted both; indices {p1['_olm_index']}, {p2['_olm_index']}; OK")

print("== 5) persistence round-trip (simulated restart) ==")
class FakeClient2(FakeClient):
    def _request(self, method, url, body=None, **kw):
        raise AssertionError("restored state: must NOT reupload keys")

bot2 = BotCrypto(FakeClient2(), tmp, on_log=log.append)
bot2.handle_todevice([])  # triggers load-or-create from disk
assert (alice.identity_keys["curve25519"], g_out.id) in bot2._groups
assert (alice.identity_keys["curve25519"], g2_out.id) in bot2._groups
ct3 = g_out.encrypt(json.dumps({"msgtype": "m.text", "body": "after-restart?"}))
assert bot2.decrypt_room_content({
    "sender_key": alice.identity_keys["curve25519"],
    "session_id": g_out.id, "ciphertext": ct3})["body"] == "after-restart?"
print("state survived restart; OK")

print("== 6) failure path: unknown session raises CryptoError ==")
try:
    bot.decrypt_room_content({"sender_key": "AAAA" * 5,
                              "session_id": "AAAA" * 5, "ciphertext": "AAAA"})
except CryptoError as e:
    print("good:", e)
else:
    raise SystemExit("expected CryptoError")

print("\nlog lines: " + f"({len(log)})")
for line in log:
    print("  |", line)
print("\nALL OFFLINE TESTS PASSED")
