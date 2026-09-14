"""Redaction tests. The stability property is the one that matters."""

from support import FixtureCase, fixture
from ps3diag import redaction


class Replacement(FixtureCase):
    def setUp(self):
        self.sample = fixture("text", "crash_report.txt")

    def test_identifiers_are_gone(self):
        text, _found = redaction.redact(self.sample)
        self.assertNotIn("00000001006A0200A1B2C3D4E5F60718", text)
        self.assertNotIn("3F2A19C4D5E6B7A8091A2B3C4D5E6F70", text)
        self.assertNotIn("00:1F:A7:3C:9B:2E", text)

    def test_everything_else_survives(self):
        text, _found = redaction.redact(self.sample)
        self.assertIn("4.93 CEX", text)
        self.assertIn("BLES01428", text)
        self.assertIn("DABR match", text)

    def test_each_kind_is_reported(self):
        _text, found = redaction.redact(self.sample)
        counts = redaction.summarise(found)
        self.assertEqual(counts.get("IDPS"), 1)
        self.assertEqual(counts.get("PSID"), 1)
        self.assertEqual(counts.get("MAC"), 1)


class Stability(FixtureCase):
    def test_the_same_value_always_gives_the_same_placeholder(self):
        first, _ = redaction.redact("IDPS: 00000001006A0200A1B2C3D4E5F60718")
        second, _ = redaction.redact("IDPS: 00000001006A0200A1B2C3D4E5F60718")
        self.assertEqual(first, second)

    def test_labelled_and_bare_forms_agree(self):
        labelled, _ = redaction.redact("IDPS 00000001006A0200A1B2C3D4E5F60718")
        bare, _ = redaction.redact("00000001006A0200A1B2C3D4E5F60718")
        self.assertEqual(labelled.split()[-1], bare.strip())

    def test_case_and_separators_do_not_change_the_placeholder(self):
        upper = redaction.placeholder("MAC", "00:1F:A7:3C:9B:2E")
        lower = redaction.placeholder("MAC", "00-1f-a7-3c-9b-2e")
        self.assertEqual(upper, lower)

    def test_different_consoles_get_different_placeholders(self):
        one = redaction.placeholder("IDPS", "00000001006A0200A1B2C3D4E5F60718")
        two = redaction.placeholder("IDPS", "00000001006A0200A1B2C3D4E5F60719")
        self.assertNotEqual(one, two)

    def test_the_placeholder_does_not_contain_the_value(self):
        value = "00000001006A0200A1B2C3D4E5F60718"
        self.assertNotIn(value.lower(),
                         redaction.placeholder("IDPS", value).lower())


class IncludeMode(FixtureCase):
    def test_values_are_kept_but_still_counted(self):
        text, found = redaction.redact(fixture("text", "crash_report.txt"),
                                       include_identifiers=True)
        self.assertIn("00:1F:A7:3C:9B:2E", text)
        self.assertTrue(found)


class Discrimination(FixtureCase):
    def test_placeholder_words_are_left_alone(self):
        text, _found = redaction.redact("Nickname: none\nonline_id: unknown")
        self.assertIn("none", text)
        self.assertIn("unknown", text)

    def test_a_title_id_is_not_mistaken_for_an_identifier(self):
        text, _found = redaction.redact("Process: BLES01428")
        self.assertIn("BLES01428", text)

    def test_an_ip_address_is_kept_because_it_is_diagnostic(self):
        text, _found = redaction.redact("IP: 192.168.1.42")
        self.assertIn("192.168.1.42", text)

    def test_empty_input(self):
        self.assertEqual(redaction.redact(""), ("", {}))
        self.assertEqual(redaction.redact(None), (None, {}))
