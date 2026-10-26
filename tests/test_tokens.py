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


CLAIM = json.dumps({"a.b": "1.0.0", "c.d": "2.3.4"})


class BuildTokens(unittest.TestCase):

    def setUp(self):
        self.access, self.refresh, self.identity = ac.build_tokens(
            2000000001, "Tester", "secret-token", CLAIM)

    def test_the_identity_token_carries_the_mods_claim(self):
        """The identity token is the only copy a server reads; without the claim
        it sees a client with no mods at all."""
        self.assertEqual(payload(self.identity)["role"], "Identity")
        self.assertEqual(payload(self.identity).get("TavernMods"), CLAIM)

    def test_the_identity_token_carries_the_tavern_token(self):
        # Same reason: FilterInvalidTokens reads this off UserCredentials too.
        self.assertEqual(payload(self.identity).get("TavernToken"), "secret-token")

    def test_the_access_token_carries_them_as_well(self):
        self.assertEqual(payload(self.access)["role"], "Access")
        self.assertEqual(payload(self.access).get("TavernMods"), CLAIM)
        self.assertEqual(payload(self.access).get("TavernToken"), "secret-token")

    def test_identity_and_access_agree_on_both_claims(self):
        a, i = payload(self.access), payload(self.identity)
        for name in ("TavernMods", "TavernToken", "UserId", "Username"):
            self.assertEqual(a.get(name), i.get(name), name)

    def test_no_claim_computed_leaves_an_empty_string_not_a_missing_claim(self):
        _, _, identity = ac.build_tokens(1, "Tester")
        self.assertEqual(payload(identity).get("TavernMods"), "")
        self.assertEqual(payload(identity).get("TavernToken"), "")

    def test_refresh_token_stays_minimal(self):
        # It's never sent to a server, so it carries no Tavern claims.
        p = payload(self.refresh)
        self.assertEqual(p["role"], "Refresh")
        self.assertNotIn("TavernMods", p)
        self.assertNotIn("TavernToken", p)


if __name__ == "__main__":
    unittest.main()
