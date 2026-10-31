"""The version grammar, held to what TavernLib's ModVersion accepts: a string
only one side parses gives the two different version sets for one mod."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tavern_shared.mods.base import _major, _parse_version
from tavern_shared.mods.base import ModManagerError


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

    def test_a_segment_wider_than_int32_is_rejected(self):
        """int.TryParse caps at Int32, so a wider segment orders here and is
        skipped there."""
        self.assertEqual(_parse_version("2147483647.0.0"), (2147483647, 0, 0))
        for bad in ("2147483648.0.0", "1.99999999999.0", "1.0.4294967296"):
            with self.assertRaises(ModManagerError) as caught:
                _parse_version(bad)
            self.assertEqual(str(caught.exception),
                             f"Version {bad!r} has a segment larger than 2147483647.")

    def test_other_shapes_still_rejected(self):
        for bad in ("1.0", "1.0.0.0", "1.0.x", "v1.0.0", "1.0.0-rc1", "1..0",
                    "+1.0.0", "1. 0.0"):
            with self.assertRaises(ModManagerError):
                _parse_version(bad)


if __name__ == "__main__":
    unittest.main()
