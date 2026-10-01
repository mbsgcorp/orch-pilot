import configparser
import unittest
from pathlib import Path

EDITORCONFIG = Path(__file__).resolve().parent.parent / ".editorconfig"


def split_preamble(text):
    """Return (preamble line, remaining text) with the first real line removed."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ";")):
            return stripped, "\n".join(lines[:i] + lines[i + 1:])
    return "", text


class EditorConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = EDITORCONFIG.read_text(encoding="utf-8")
        cls.preamble, rest = split_preamble(text)
        cls.config = configparser.ConfigParser(interpolation=None)
        cls.config.read_string(rest)

    def assertSettings(self, section, expected):
        self.assertIn(section, self.config.sections())
        for key, value in expected.items():
            with self.subTest(section=section, key=key):
                self.assertIn(key, self.config[section])
                self.assertEqual(self.config[section][key].strip().lower(), value)

    def test_root_preamble(self):
        self.assertEqual(self.preamble.lower(), "root = true")

    def test_sections_present(self):
        self.assertEqual(self.config.sections(), ["*", "*.md", "*.{yml,yaml}"])

    def test_all_files_section(self):
        self.assertSettings("*", {
            "charset": "utf-8",
            "end_of_line": "lf",
            "insert_final_newline": "true",
            "trim_trailing_whitespace": "true",
            "indent_style": "space",
            "indent_size": "4",
        })

    def test_markdown_section(self):
        self.assertSettings("*.md", {
            "trim_trailing_whitespace": "false",
            "indent_size": "2",
        })

    def test_yaml_section(self):
        self.assertSettings("*.{yml,yaml}", {"indent_size": "2"})


if __name__ == "__main__":
    unittest.main()
