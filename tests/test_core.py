"""Testy jednostkowe rdzeni: ramki, krypto, konfiguracja, mapowanie klawiszy.

Uruchom:  python3 -m pytest tests/test_core.py   albo   python3 tests/test_core.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rd import crypto as dcrypto
from rd import keys
from rd.protocol import (
    FLAG_COMPRESSED,
    FLAG_PLAIN,
    FT,
    HEADER_LEN,
    ProtocolError,
    pack_frame,
    unpack_frame,
)
from rd.settings import Config, format_id, looks_like_address, parse_id


class FramingTests(unittest.TestCase):
    def test_roundtrip(self):
        raw = pack_frame(FT.CAPS, b'{"a":1}')
        self.assertEqual(len(raw), HEADER_LEN + len(b'{"a":1}'))
        frame, rest = unpack_frame(raw)
        self.assertEqual(frame.type, FT.CAPS)
        self.assertEqual(frame.payload, b'{"a":1}')
        self.assertEqual(rest, b"")

    def test_two_frames_in_one_buffer(self):
        a = pack_frame(FT.PING, b"1")
        b = pack_frame(FT.PONG, b"2")
        f1, rest = unpack_frame(a + b)
        f2, rest = unpack_frame(rest)
        self.assertEqual((f1.payload, f2.payload), (b"1", b"2"))
        self.assertEqual(rest, b"")

    def test_bad_magic(self):
        with self.assertRaises(ProtocolError):
            unpack_frame(b"XX" + b"\x00" * 20)

    def test_truncated(self):
        raw = pack_frame(FT.FRAME, b"A" * 100)
        with self.assertRaises(ProtocolError):
            unpack_frame(raw[: HEADER_LEN + 50])

    def test_compression_flag(self):
        payload = b"ala" * 500
        raw = pack_frame(FT.SYSINFO, payload, flags=FLAG_COMPRESSED)
        self.assertLess(len(raw), len(payload))
        frame, _ = unpack_frame(raw)
        self.assertEqual(frame.payload, payload)


class CryptoTests(unittest.TestCase):
    def test_available(self):
        self.assertTrue(dcrypto.HAS_CRYPTO, "brak 'cryptography' - sesie beda bez Szyfrowania")

    def test_kex_and_keys(self):
        a = dcrypto.EphemeralKey.generate()
        b = dcrypto.EphemeralKey.generate()
        s1 = a.shared(b.public_b64)
        s2 = b.shared(a.public_b64)
        self.assertEqual(s1, s2)
        keys_a = dcrypto.derive_session_keys(s1, b"transcript")
        keys_b = dcrypto.derive_session_keys(s2, b"transcript")
        self.assertEqual(keys_a.data_c2s, keys_b.data_c2s)
        self.assertNotEqual(keys_a.data_c2s, keys_a.data_s2c)

    def test_aead_roundtrip_and_order(self):
        tx = os.urandom(32)
        rx = os.urandom(32)
        alice = dcrypto.SessionCrypto(tx, rx)
        bob = dcrypto.SessionCrypto(rx, tx)
        for i in range(200):
            blob = alice.encrypt(f"ramka {i}".encode(), i, 0)
            self.assertEqual(bob.decrypt(blob, i, 0), f"ramka {i}".encode())
        # zla kolejnosc = odrzucone (replay / reordering)
        with self.assertRaises(ValueError):
            bob.decrypt(alice.encrypt(b"x", 201, 0), 201, 0)

    def test_tamper_detected(self):
        k = os.urandom(32)
        a = dcrypto.SessionCrypto(k, k)
        b = dcrypto.SessionCrypto(k, k)
        blob = bytearray(a.encrypt(b"sekret", 0, 0))
        blob[3] ^= 0xFF
        from cryptography.exceptions import InvalidTag

        with self.assertRaises(InvalidTag):
            b.decrypt(bytes(blob), 0, 0)

    def test_password_proof_uses_verifier_only(self):
        """Host dowodzi bez hasla jawnego - tylko z zapisanego weryfikatora."""
        salt = dcrypto.new_salt()
        pw = "super-tajne-123"
        verifier = dcrypto.hash_password(pw, salt)
        key = os.urandom(32)
        challenge = os.urandom(16)
        proof = dcrypto.auth_proof(key, challenge, "AA:BB", verifier)
        expect = dcrypto.auth_proof(key, challenge, "AA:BB", bytes.fromhex(dcrypto.hash_password_hex(pw, salt)))
        self.assertEqual(proof, expect)
        wrong = dcrypto.auth_proof(key, challenge, "AA:BB", dcrypto.hash_password("inna", salt))
        self.assertNotEqual(proof, wrong)

    def test_fingerprint_stable(self):
        pub = os.urandom(32)
        self.assertEqual(dcrypto.fingerprint(pub), dcrypto.fingerprint(pub))
        self.assertEqual(len(dcrypto.fingerprint(pub).split(":")), 16)


class SettingsTests(unittest.TestCase):
    def test_id_format_and_parse(self):
        self.assertEqual(format_id(123456789), "123 456 789")
        self.assertEqual(parse_id("123 456 789"), 123456789)
        self.assertEqual(parse_id("ID: 12-34-56-78-9"), 123456789)
        self.assertIsNone(parse_id("za krotkie"))
        self.assertTrue(looks_like_address("192.168.1.20:7000"))
        self.assertTrue(looks_like_address("host.local"))
        self.assertFalse(looks_like_address("123 456 789"))

    def test_save_load_password(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            import pathlib

            cfg = Config.load(pathlib.Path(path))
            cfg.set_password("haslo-agenta")
            cfg.fps = 33
            cfg.identity.desk_id = 424242424
            cfg.save(pathlib.Path(path))
            back = Config.load(pathlib.Path(path))
            self.assertEqual(back.fps, 33)
            self.assertEqual(back.identity.desk_id, 424242424)
            self.assertTrue(back.verify_password("haslo-agenta"))
            self.assertFalse(back.verify_password("zle"))
            self.assertTrue(back.unattended)
            # jawne haslo nie trafia na dysk
            raw = pathlib.Path(path).read_text()
            self.assertNotIn("haslo-agenta", raw)

    def test_tofu(self):
        cfg = Config()
        self.assertTrue(cfg.is_trusted(111, "AA:BB"))      # pierwszy kontakt = ufnosc
        cfg.trust(111, "AA:BB")
        self.assertTrue(cfg.is_trusted(111, "AA:BB"))
        self.assertFalse(cfg.is_trusted(111, "DE:AD"))      # 'key changed'

    def test_recent_dedup(self):
        cfg = Config()
        cfg.remember_peer(1, "a", "FP1")
        cfg.remember_peer(2, "b", "FP2")
        cfg.remember_peer(1, "a-nowa", "FP1")
        self.assertEqual(cfg.recent[0]["desk_id"], 1)
        self.assertEqual(len(cfg.recent), 2)


class KeyMapTests(unittest.TestCase):
    def test_pynput_names(self):
        class K:
            def __init__(self, name):
                self.name = name

        self.assertEqual(keys.normalize_pynput_key(K("shift_l"))[0], "shift")
        self.assertEqual(keys.normalize_pynput_key(K("cmd"))[0], "super")
        self.assertEqual(keys.normalize_pynput_key(K("page_up"))[0], "pageup")
        self.assertEqual(keys.normalize_pynput_key(K("f5"))[0], "f5")

        class C:
            char = "z"

        self.assertEqual(keys.normalize_pynput_key(C()), (None, "z"))

    def test_tk_keysyms(self):
        # realne keysymy z Tk/X11 i Windows/Wine musza dac te same CANON-y
        self.assertEqual(keys.key_from_tk("Control_L"), ("ctrl", None))
        self.assertEqual(keys.key_from_tk("ctrl_L"), ("ctrl", None))
        self.assertEqual(keys.key_from_tk("CONTROL_L"), ("ctrl", None))
        self.assertEqual(keys.key_from_tk("Shift_R"), ("shift", None))
        self.assertEqual(keys.key_from_tk("Super_L"), ("super", None))
        self.assertEqual(keys.key_from_tk("Win_R"), ("super", None))
        self.assertEqual(keys.key_from_tk("Alt_R"), ("altgr", None))
        self.assertEqual(keys.key_from_tk("Return"), ("enter", None))
        self.assertEqual(keys.key_from_tk("Prior"), ("pageup", None))
        self.assertEqual(keys.key_from_tk("F12"), ("f12", None))
        self.assertEqual(keys.key_from_tk("KP_3"), ("num3", None))
        # znaki drukowalne ida jako char, nie jako canon
        self.assertEqual(keys.key_from_tk("c"), (None, "c"))
        self.assertEqual(keys.key_from_tk("A"), (None, "A"))
        self.assertEqual(keys.key_from_tk("U0142"), (None, "\u0142"))
        self.assertEqual(keys.key_from_tk("plus"), (None, "+"))
        self.assertEqual(keys.key_from_tk(""), (None, None))

    def test_host_names_diverge(self):
        self.assertEqual(keys.host_key_name("esc", windows=True), "escape")
        self.assertEqual(keys.host_key_name("esc", windows=False), "esc")
        self.assertEqual(keys.host_key_name("super", windows=True), "win")
        self.assertEqual(keys.host_key_name("super", windows=False), "super")
        self.assertIsNone(keys.host_key_name("blad", windows=True))


class RelayTargetTests(unittest.TestCase):
    """Zmiana adresu relaya w Ustawieniach musi restartowac watek, nie czekac na restart apki."""

    @staticmethod
    def _link(host, port, tls=False, key=""):
        import types

        return types.SimpleNamespace(host=host, port=port, tls=tls, key=key)

    def _agent(self, host="", port=7001, tls=False, key=""):
        import pathlib
        import types

        from rd.agent import Agent

        cfg = Config()
        cfg.relay_host, cfg.relay_port, cfg.relay_tls = host, port, tls
        cfg.relay_key = key
        store = types.SimpleNamespace(config=cfg, path=pathlib.Path("cfg.json"),
                                      flush=lambda force=False: None)
        return Agent(store)

    def test_restart_when_no_link(self):
        a = self._agent("relay.example", 7010)
        self.assertTrue(a.needs_relay_restart("relay.example", 7010, False, ""))
        a2 = self._agent("", 7010)
        self.assertFalse(a2.needs_relay_restart("", 7010, False, ""))   # brak hosta = nic nie startuje

    def test_same_target_is_left_alone(self):
        a = self._agent("127.0.0.1", 7010)
        a.link = self._link("127.0.0.1", 7010)
        self.assertFalse(a.needs_relay_restart("127.0.0.1", 7010, False, ""))

    def test_stale_link_to_typo_host_is_replaced(self):
        """Klasyka: najpierw '7002' w polu hosta, potem poprawka na 127.0.0.1."""
        a = self._agent("127.0.0.1", 7010)
        a.link = self._link("7002", 7002)
        self.assertTrue(a.needs_relay_restart("127.0.0.1", 7010, False, ""))
        # port, tls i klucz tez licza sie jako roznica
        self.assertTrue(a.needs_relay_restart("127.0.0.1", 7011, False, ""))
        self.assertTrue(a.needs_relay_restart("127.0.0.1", 7010, True, ""))
        self.assertTrue(a.needs_relay_restart("127.0.0.1", 7010, False, "sekret"))

    def test_tls_int_str_port_and_missing_attrs(self):
        a = self._agent("127.0.0.1", 7010)
        a.link = self._link("127.0.0.1", "7010")            # port jako str -> int() zgadza
        self.assertFalse(a.needs_relay_restart("127.0.0.1", 7010, False, ""))
        a.link = self._link("127.0.0.1", "nie-liczba")            # port, ktorego nie da sie int()
        self.assertTrue(a.needs_relay_restart("127.0.0.1", 7010, False, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
