import string
import unittest

from comic_dedupe import i18n


def _fields(text: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class I18nTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_every_key_has_all_languages_with_same_placeholders(self):
        for key, entry in i18n.STRINGS.items():
            self.assertEqual(set(entry), set(i18n.LANGUAGES), key)
            self.assertEqual(_fields(entry["ja"]), _fields(entry["en"]), key)

    def test_switch_and_fallback(self):
        i18n.set_language("en")
        self.assertEqual(i18n.t("btn.execute"), "Run")
        self.assertEqual(i18n.t("result.group", key="a"), "Group a")
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")
        i18n.set_language("zz")
        self.assertEqual(i18n.get_language(), i18n.DEFAULT_LANGUAGE)

    def test_role_text(self):
        from comic_dedupe import constants as C
        i18n.set_language("en")
        self.assertEqual(i18n.role_text(C.ROLE_WINNER), "Keep")
