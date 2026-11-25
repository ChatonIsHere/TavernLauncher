"""Joining by name: SRV records find a server's auth port, and the auth
replies carry its game and web ports (client/core/srv.py, auth.py), served
by the server launcher from auth_port, web_port and server_port."""
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client.core import auth, srv
from server.core import auth_service as svc
from server.core import data_store


class Pick(unittest.TestCase):

    def test_the_lowest_priority_wins(self):
        self.assertEqual(srv.pick([(10, 5, 1, "b.example."), (0, 5, 2, "a.example.")]), ("a.example", 2))

    def test_weight_shares_out_one_priority(self):
        records = [(0, 1, 1, "light."), (0, 3, 2, "heavy.")]
        with mock.patch.object(srv.random, "uniform", lambda a, b: 0.5):
            self.assertEqual(srv.pick(records), ("light", 1))
        with mock.patch.object(srv.random, "uniform", lambda a, b: 3.9):
            self.assertEqual(srv.pick(records), ("heavy", 2))

    def test_all_zero_weights_take_the_first(self):
        self.assertEqual(srv.pick([(0, 0, 1, "a."), (0, 0, 2, "b.")]), ("a", 1))

    def test_a_dot_target_means_no_service(self):
        self.assertIsNone(srv.pick([(0, 0, 1762, ".")]))
        self.assertIsNone(srv.pick([]))


class Lookup(unittest.TestCase):

    def _no_query(self, name):
        raise AssertionError("queried " + name)

    def test_ip_addresses_are_never_looked_up(self):
        with mock.patch.object(srv, "_query", self._no_query, create=True):
            for host in ("1.2.3.4", "::1", "", "  "):
                self.assertIsNone(srv.lookup(host))

    def test_the_service_name_goes_in_front(self):
        asked = []
        answer = [(0, 0, 27016, "box.example.com.")]
        with mock.patch.object(srv, "_query", lambda n: asked.append(n) or answer, create=True), \
             mock.patch.object(srv.sys, "platform", "win32"):
            self.assertEqual(srv.lookup("play.example.com."), ("box.example.com", 27016))
        self.assertEqual(asked, ["_townshiptale._tcp.play.example.com"])

    def test_a_failed_lookup_is_no_record(self):
        def boom(name):
            raise OSError("no DNS")
        with mock.patch.object(srv, "_query", boom, create=True), \
             mock.patch.object(srv.sys, "platform", "win32"):
            self.assertIsNone(srv.lookup("play.example.com"))

    def test_off_windows_there_is_no_record(self):
        with mock.patch.object(srv, "_query", self._no_query, create=True), \
             mock.patch.object(srv.sys, "platform", "linux"):
            self.assertIsNone(srv.lookup("play.example.com"))

    @unittest.skipUnless(sys.platform == "win32" and os.environ.get("TAVERN_NET_TESTS"),
                         "set TAVERN_NET_TESTS=1 on Windows to ask real DNS")
    def test_a_real_record(self):
        with mock.patch.object(srv, "SERVICE", "_xmpp-client._tcp."):
            self.assertEqual(srv.lookup("jabber.org")[1], 5222)
        self.assertIsNone(srv.lookup("example.com"))


class Addresses(unittest.TestCase):

    def setUp(self):
        records = {"play.example.com": ("box.example.com", 27016)}
        patcher = mock.patch.object(srv, "lookup", records.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auth_address(self):
        self.assertEqual(auth.auth_address("play.example.com"), ("box.example.com", 27016))
        self.assertEqual(auth.auth_address("other.example.com"), ("other.example.com", 1762))
        self.assertEqual(auth.auth_address("203.0.113.5"), ("203.0.113.5", 1762))
        # A key's port is the auth port, and skips the lookup.
        self.assertEqual(auth.auth_address("203.0.113.5:27016"), ("203.0.113.5", 27016))
        self.assertEqual(auth.auth_address("[2001:db8::5]:27016"), ("2001:db8::5", 27016))

    def test_the_key_keeps_a_port_only_when_it_isnt_1762(self):
        ips = {"box.example.com": "203.0.113.5", "other.example.com": "203.0.113.6"}
        with mock.patch.object(auth.socket, "gethostbyname", lambda h: ips.get(h, h)):
            self.assertEqual(auth._resolve_ip_for_game("play.example.com"), "203.0.113.5:27016")
            self.assertEqual(auth._resolve_ip_for_game("other.example.com"), "203.0.113.6")
            self.assertEqual(auth._resolve_ip_for_game("203.0.113.5:27016"), "203.0.113.5:27016")
        with mock.patch.object(auth.socket, "gethostbyname", lambda h: "2001:db8::5"):
            self.assertEqual(auth._resolve_ip_for_game("play.example.com"), "[2001:db8::5]:27016")

    def test_game_ip(self):
        self.assertEqual(auth.game_ip("203.0.113.5:27016"), "203.0.113.5")
        self.assertEqual(auth.game_ip("203.0.113.5"), "203.0.113.5")
        self.assertEqual(auth.game_ip("[2001:db8::5]:27016"), "2001:db8::5")

    def test_ports_of(self):
        self.assertEqual(auth.ports_of({"game_port": 27015, "web_port": 27017}), (27015, 27017))
        self.assertEqual(auth.ports_of({"game_port": 1757}), (1757, None))
        self.assertEqual(auth.ports_of({"game_port": "1757", "web_port": 70000}), (None, None))

    def test_the_console_is_where_the_ping_says(self):
        pinged = []
        with mock.patch.object(auth, "ping_server",
                               lambda h, timeout=3: (pinged.append(h), ({"console_port": 27018}, 1))[1]):
            # Found by SRV: the console is on the record's target, on the port the ping gives.
            self.assertEqual(auth.console_address("play.example.com"), ("box.example.com", 27018))
            # A typed game port is dropped before the ping.
            self.assertEqual(auth.console_address("203.0.113.5:1757"), ("203.0.113.5", 27018))
        self.assertEqual(pinged, ["play.example.com", "203.0.113.5"])

    def test_the_console_falls_back_to_1760(self):
        with mock.patch.object(auth, "ping_server", lambda h, timeout=3: ({"status": "pong"}, 1)):
            self.assertEqual(auth.console_address("203.0.113.5"), ("203.0.113.5", 1760))

        def unreachable(h, timeout=3):
            raise OSError("refused")
        with mock.patch.object(auth, "ping_server", unreachable):
            self.assertEqual(auth.console_address("play.example.com"), ("play.example.com", 1760))


class CfgPort(unittest.TestCase):

    def test_missing_and_bad_values_are_the_default(self):
        self.assertEqual(data_store.cfg_port("auth_port", 1762, {}), 1762)
        self.assertEqual(data_store.cfg_port("auth_port", 1762, {"auth_port": "27016"}), 27016)
        for bad in ("", "x", 0, 70000, None):
            self.assertEqual(data_store.cfg_port("auth_port", 1762, {"auth_port": bad}), 1762)


class OverTheWire(unittest.TestCase):
    """The client's ping and join, found by SRV record, against the server
    launcher's handler on a port that isn't 1762."""

    def setUp(self):
        self.cfg = {"server_port": "27015", "web_port": 27017}
        users = {"tester": {"user_id": 1000000001, "token": "tok", "registered_from": "127.0.0.1", "roles": []}}
        for target, name, value in [
            (svc, "_load_users", lambda: json.loads(json.dumps(users))),
            (svc, "_save_users", lambda u: None),
            (svc, "_throttle_ok", lambda ip: True),
            (svc, "load_server_settings", lambda: {}),
            (svc, "_is_blacklisted", lambda *a: False),
            (svc, "_is_whitelisted", lambda *a: True),
            (svc, "_read_live_player_status", lambda: (None, None)),
            (svc, "load_cfg", lambda: dict(self.cfg)),
            (data_store, "load_cfg", lambda: dict(self.cfg)),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _found_at(self, port):
        patcher = mock.patch.object(srv, "lookup", {"play.example.com": ("127.0.0.1", port)}.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ping_and_join_by_name(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)
        self._found_at(listener.getsockname()[1])

        def serve():
            for _ in range(3):
                conn, addr = listener.accept()
                svc._handle_auth(conn, addr, lambda *a: None, lambda: None)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        resp, _ms = auth.ping_server("play.example.com")
        self.assertEqual(auth.ports_of(resp), (27015, 27017))
        self.assertEqual(resp["console_port"], 1760)   # not in this config: the default
        user_id, error, _scene, ports = auth.login("play.example.com", "Tester", "tok")
        self.assertIsNone(error)
        self.assertEqual(user_id, 1000000001)
        self.assertEqual(ports, (27015, 27017))
        # authenticate() keeps the three values Rover unpacks.
        self.assertEqual(len(auth.authenticate("play.example.com", "Tester", "tok")), 3)
        thread.join(5)

    def test_the_service_listens_on_auth_port(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        self.cfg["auth_port"] = port
        logged = []
        svc.start_auth_service(lambda msg, tag="": logged.append(msg), lambda: None)
        deadline = time.time() + 5
        while f"Auth service listening on :{port}" not in logged and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn(f"Auth service listening on :{port}", logged)
        self._found_at(port)
        resp, _ms = auth.ping_server("play.example.com")
        self.assertEqual(resp["status"], "pong")


if __name__ == "__main__":
    unittest.main()
