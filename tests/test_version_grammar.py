"""Which version strings the launcher accepts, as opposed to which ones it can
technically parse.

TavernLib parses the same index and the same manifests with int.TryParse, and
both sides skip an unparseable version with a warning rather than throwing. So a
string only Python accepts doesn't blow up anywhere - it quietly gives the two
implementations different version sets for the same mod, and with that a
different highest() and a different major lock. These cases pin the grammar to
the intersection.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared.mods.version import _major, _parse_version
from tavern_shared.mods.errors import ModManagerError


class VersionGrammar(unittest.TestCase):

    def test_plain_versions_still_parse(self):
        self.assertEqual(_parse_version("1.10.0"), (1, 10, 0))
        self.assertEqual(_parse_version(" 1.2.3 "), (1, 2, 3))
        self.assertEqual(_major("2.0.0"), 2)

    def test_underscore_digit_separators_rejected(self):
        """int("0_5") is 5 in Python and a parse failure in C#: accepting it
        here would have the launcher resolve 1.5.0 where the host resolves
        nothing at all."""
        for bad in ("1_0.0.0", "1.0_5.0", "1.0.1_0"):
            with self.assertRaises(ModManagerError):
                _parse_version(bad)

    def test_non_ascii_digits_rejected(self):
        """int() accepts every Unicode decimal digit (Arabic-Indic here, but
        Devanagari and the rest behave the same); int.TryParse accepts none."""
        for bad in ("١.0.0", "1.٢٣.0", "1.0.۵"):
            with self.assertRaises(ModManagerError):
                _parse_version(bad)

    def test_other_shapes_still_rejected(self):
        for bad in ("1.0", "1.0.0.0", "1.0.x", "v1.0.0", "1.0.0-rc1", "1..0",
                    "+1.0.0", "1. 0.0"):
            with self.assertRaises(ModManagerError):
                _parse_version(bad)


if __name__ == "__main__":
    unittest.main()
