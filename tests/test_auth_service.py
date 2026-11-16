"""The server launcher's 1762 handler: set_mods saves the player's mod list in
users.json for TavernLib's join check, and a join handshake clears it."""
import json
import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client.core import auth as client_auth
from server.core import auth_service as svc


class FakeConn:
    def __init__(self, data, chunk=1000):
        self.chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)]
        self.sent = b""

    def settimeout(self, t):
        pass

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


class SetMods(unittest.TestCase):

    def setUp(self):
        self.users = {"tester": {"user_id": 1000000001, "token": "tok", "registered_from": "1.2.3.4", "roles": []}}
        self.fails = []
        for name, value in {
            "_load_users": lambda: json.loads(json.dumps(self.users)),
            "_save_users": lambda users: setattr(self, "users", users),
            "_throttle_ok": lambda ip: True,
            "_record_fail": self.fails.append,
            "load_server_settings": lambda: {},
            "_is_blacklisted": lambda *a: False,
            "_is_whitelisted": lambda *a: True,
        }.items():
            patcher = mock.patch.object(svc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _send(self, req, chunk=1000):
        conn = FakeConn(json.dumps(req).encode(), chunk)
        svc._handle_auth(conn, ("1.2.3.4", 5000), lambda *a: None, lambda: None)
        return json.loads(conn.sent.decode())

    def _set(self, mods, token="tok", username="Tester"):
        return self._send({"set_mods": True, "username": username, "token": token, "mods": mods})

    def test_the_list_is_saved_sorted(self):
        self.assertEqual(self._set({"b.mod": "2.0", "a.mod": "1.0"}), {"status": "ok"})
        self.assertEqual(list(self.users["tester"]["mods"].items()), [("a.mod", "1.0"), ("b.mod", "2.0")])

    def test_a_list_over_many_recvs_is_read_whole(self):
        """Every mod is a few dozen bytes, so a big list spans several recvs."""
        mods = {f"Author.Mod{i:03}": "1.0.0" for i in range(300)}
        req = {"set_mods": True, "username": "Tester", "token": "tok", "mods": mods}
        self.assertEqual(self._send(req, chunk=1400), {"status": "ok"})
        self.assertEqual(self.users["tester"]["mods"], mods)

    def test_an_empty_list_is_kept_as_no_mods(self):
        """{} says "no mods", which TavernLib tells apart from not sending a list."""
        self._set({})
        self.assertEqual(self.users["tester"]["mods"], {})

    def test_the_wrong_token_saves_nothing(self):
        resp = self._set({"a.mod": "1.0"}, token="other")
        self.assertEqual(resp["status"], "error")
        self.assertNotIn("mods", self.users["tester"])
        self.assertEqual(self.fails, ["1.2.3.4"])

    def test_an_unknown_account_saves_nothing(self):
        self.assertEqual(self._set({"a.mod": "1.0"}, username="Nobody")["status"], "error")
        self.assertNotIn("nobody", self.users)

    def test_malformed_lists_are_refused(self):
        for mods in (["a.mod"], {"a.mod": 1}, {"": "1.0"}, {"a" * 129: "1.0"}, {"a.mod": "v" * 129},
                     {f"m{i}": "1" for i in range(1001)}):
            with self.subTest(mods=str(mods)[:40]):
                self.assertEqual(self._set(mods), {"status": "error", "message": "Malformed mod list"})
        self.assertNotIn("mods", self.users["tester"])

    def test_a_join_handshake_clears_the_last_list(self):
        """The launcher sends this join's list once its mods are installed."""
        self._set({"a.mod": "1.0"})
        resp = self._send({"username": "Tester", "token": "tok"})
        self.assertEqual(resp["status"], "ok")
        self.assertNotIn("mods", self.users["tester"])

    def test_the_launcher_s_send_mods_over_a_socket(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            conn, addr = listener.accept()
            svc._handle_auth(conn, addr, lambda *a: None, lambda: None)
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        mods = {f"Author.Mod{i:03}": "1.0.0" for i in range(500)}
        with mock.patch.object(client_auth, "AUTH_PORT", listener.getsockname()[1]):
            self.assertIsNone(client_auth.send_mods("127.0.0.1", "Tester", "tok", mods))
        server.join(5)
        self.assertEqual(self.users["tester"]["mods"], mods)


class ReadRequest(unittest.TestCase):

    def test_nothing_sent(self):
        self.assertIsNone(svc._read_request(FakeConn(b"")))

    def test_cut_off(self):
        with self.assertRaises(ValueError):
            svc._read_request(FakeConn(b'{"ping": tr'))

    def test_over_the_limit(self):
        with self.assertRaises(ValueError):
            svc._read_request(FakeConn(b'{"pad": "' + b"x" * svc._MAX_REQUEST + b'"}', chunk=8192))


if __name__ == "__main__":
    unittest.main()
