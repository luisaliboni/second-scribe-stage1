"""Task 45: every CSS file of the app has balanced braces outside comments and strings.

An unclosed ``@media (...) {`` block slipped through twice: the browser then reads every rule after it as part of the
media block, so those rules apply only at that width and the page looks fine at the width it was checked at. This
test reads each file under ``second_scribe/app`` (static CSS, dev pages' copies, anything ending ``.css``) and fails
on a ``}`` with nothing open, or on a block still open at the end of the file, naming the file and the line."""
from __future__ import annotations

import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "second_scribe" / "app"


def brace_problems(text: str) -> list[str]:
    """Problems with the braces of one CSS text, outside ``/* */`` comments and quoted strings: ``[]`` when balanced.
    An unterminated comment or string is a problem too (it hides every brace after it)."""
    problems: list[str] = []
    stack: list[int] = []                     # line numbers of the open braces
    i, line, n = 0, 1, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                problems.append(f"line {line}: comment never closed")
                return problems
            line += text.count("\n", i, end + 2)
            i = end + 2
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n":                 # a raw newline ends a CSS string (it is then invalid)
                    break
                j += 1
            if j >= n or text[j] != c:
                problems.append(f"line {line}: string never closed")
                return problems
            i = j + 1
        elif c == "\\":                               # an escaped character outside a string, e.g. \{ in a selector
            i += 2
        elif c == "{":
            stack.append(line)
            i += 1
        elif c == "}":
            if not stack:
                problems.append(f"line {line}: '}}' closes nothing")
            else:
                stack.pop()
            i += 1
        else:
            i += 1
    problems.extend(f"line {at}: '{{' never closed" for at in stack)
    return problems


def css_files() -> list[Path]:
    return sorted(APP.rglob("*.css"))


class TestChecker(unittest.TestCase):
    """The checker itself bites, and ignores braces in comments and strings."""

    def test_balanced(self):
        self.assertEqual(brace_problems("a { color: red; }\n@media (max-width: 9px) {\n  b { c: d; }\n}\n"), [])

    def test_unclosed_media_block(self):
        css = "@media (max-width: 760px) {\n  .x { a: b; }\n\n.y { c: d; }\n"
        self.assertEqual(brace_problems(css), ["line 1: '{' never closed"])

    def test_stray_close(self):
        self.assertEqual(brace_problems(".x { a: b; }\n}\n"), ["line 2: '}' closes nothing"])

    def test_braces_in_comments_and_strings_do_not_count(self):
        css = '/* { not a block */\n.x::before { content: "{"; }\n.y::after { content: \'}\\\'\'; }\n'
        self.assertEqual(brace_problems(css), [])

    def test_unterminated_comment_and_string(self):
        self.assertEqual(brace_problems(".x { a: b; }\n/* open"), ["line 2: comment never closed"])
        self.assertEqual(brace_problems('.x { content: "a; }\n'), ["line 1: string never closed"])


class TestAppCss(unittest.TestCase):
    def test_every_app_css_file_has_balanced_braces(self):
        files = css_files()
        self.assertGreaterEqual(len(files), 10, "the app's CSS files were not found")
        for f in files:
            with self.subTest(css=str(f.relative_to(APP))):
                self.assertEqual(brace_problems(f.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
