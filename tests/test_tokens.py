"""build_tokens: which JWT carries which claim. The game forwards only the
identity token to a server, so a claim on the wrong token is a total
failure that's invisible without a live server."""
import os
import sys
import json
import base64
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client.core import auth as ac


def payload(token):
    """The decoded middle segment of a JWT, no signature check."""
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


class BuildTokens(unittest.TestCase):

    def setUp(self):
        self.access, self.refresh, self.identity = ac.build_tokens(
            2000000001, "Tester", "secret-token")

    def test_the_identity_token_carries_the_tavern_token(self):
        """The identity token is the only copy a server reads; FilterInvalidTokens
        reads this off UserCredentials."""
        self.assertEqual(payload(self.identity)["role"], "Identity")
        self.assertEqual(payload(self.identity).get("TavernToken"), "secret-token")

    def test_the_access_token_carries_it_as_well(self):
        self.assertEqual(payload(self.access)["role"], "Access")
        self.assertEqual(payload(self.access).get("TavernToken"), "secret-token")

    def test_identity_and_access_agree(self):
        a, i = payload(self.access), payload(self.identity)
        for name in ("TavernToken", "UserId", "Username"):
            self.assertEqual(a.get(name), i.get(name), name)

    def test_no_mod_list_rides_the_tokens(self):
        """The mod list goes over set_mods: in the token it outgrew the game's join message."""
        for token in (self.access, self.identity):
            self.assertNotIn("TavernMods", payload(token))

    def test_the_identity_token_fits_the_join_message(self):
        """The game sends it in a 1100-byte join message with a few other fields."""
        _, _, identity = ac.build_tokens(9999999999, "N" * 32, "t" * 128)
        self.assertLess(len(identity), 900)

    def test_no_token_leaves_an_empty_string_not_a_missing_claim(self):
        _, _, identity = ac.build_tokens(1, "Tester")
        self.assertEqual(payload(identity).get("TavernToken"), "")

    def test_refresh_token_stays_minimal(self):
        # It's never sent to a server, so it carries no Tavern claims.
        self.assertNotIn("TavernToken", payload(self.refresh))
        self.assertEqual(payload(self.refresh)["role"], "Refresh")


if __name__ == "__main__":
    unittest.main()
