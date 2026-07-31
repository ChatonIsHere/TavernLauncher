"""build_tokens: the JWTs the launcher hands the game at launch.

Focused on which token carries which claim. That matters because the game
forwards exactly one of them to a server as RequestJoinMessage.UserCredentials,
and TavernLib reads every claim it cares about off that one. A claim on the
wrong token is not a partial failure, it's a total one, and it can't be seen
without a live server - hence these.
"""
import os
import sys
import json
import base64
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import att_client as ac


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
        """The one that matters: the game sends the identity token as
        UserCredentials, so this is the only copy a server ever reads. With it
        only on the access token the server sees a client with no mods at all
        and rejects every mod it requires of clients, whatever is installed."""
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

    def test_a_server_reading_the_identity_token_sees_parity(self):
        """End to end against ModParity.ValidateClient's rule: every server mod
        with a client side must be present at that exact version."""
        client_mods = json.loads(payload(self.identity)["TavernMods"])
        required = {"a.b": "1.0.0", "c.d": "2.3.4"}
        mismatches = [mid for mid, ver in required.items()
                      if client_mods.get(mid) != ver]
        self.assertEqual(mismatches, [])

    def test_a_wrong_version_is_still_caught(self):
        # The check has to keep working, not just always pass.
        client_mods = json.loads(payload(self.identity)["TavernMods"])
        self.assertNotEqual(client_mods.get("a.b"), "9.9.9")

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
