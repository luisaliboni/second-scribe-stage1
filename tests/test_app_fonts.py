"""Task 2: bundled OFL fonts, fonts.css, and the no-remote-fonts rule."""
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
STATIC = APP / "static"
FONTS = STATIC / "fonts"
CSS = STATIC / "css" / "fonts.css"
SOURCES = FONTS / "SOURCES.md"

WOFF2 = ["SchibstedGrotesk-Variable-latin.woff2", "JetBrainsMono-Variable-latin.woff2"]


def _sources_hashes():
    """Map file name -> sha256 from the SOURCES.md table rows (| `file` | ... | sha256 |)."""
    out = {}
    for line in SOURCES.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`([^`]+\.woff2)`\s*\|.*\|\s*`?([0-9a-f]{64})`?\s*\|\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


class TestFonts(unittest.TestCase):
    def test_every_font_face_url_exists(self):
        css = CSS.read_text(encoding="utf-8")
        urls = re.findall(r"url\(\s*['\"]?([^)'\"]+?)['\"]?\s*\)", css)
        self.assertEqual(len(urls), len(WOFF2))
        for u in urls:
            self.assertTrue(u.startswith("../fonts/"), u)
            self.assertTrue((CSS.parent / u).resolve().is_file(), u)
        self.assertEqual(sorted(Path(u).name for u in urls), sorted(WOFF2))

    def test_font_family_weights_declared(self):
        css = CSS.read_text(encoding="utf-8")
        # one variable file per family; the weight range covers every weight the CSS uses
        for fam, weights in (("Schibsted Grotesk", "400 900"), ("JetBrains Mono", "400 800")):
            got = []
            for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
                if re.search(r"font-family:\s*['\"]%s['\"]" % re.escape(fam), block):
                    got.append(re.search(r"font-weight:\s*([0-9 ]+);", block).group(1).strip())
                    self.assertIn("font-display: swap", block)
            self.assertEqual(got, [weights], fam)

    def test_font_sha256_matches_sources(self):
        listed = _sources_hashes()
        self.assertEqual(sorted(listed), sorted(WOFF2))
        for name in WOFF2:
            data = (FONTS / name).read_bytes()
            self.assertEqual(data[:4], b"wOF2", name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), listed[name], name)

    def test_licences_present(self):
        for name in ("OFL-SchibstedGrotesk.txt", "OFL-JetBrainsMono.txt"):
            text = (FONTS / name).read_text(encoding="utf-8")
            self.assertIn("SIL Open Font License", text, name)

    def test_no_remote_fonts_anywhere(self):
        bad = (b"fonts.googleapis", b"fonts.gstatic", b"@import url(http")
        for p in APP.rglob("*"):
            if p == SOURCES:
                continue   # the provenance table names the download URLs; nothing loads it (test_js_lint has the same exception)
            if p.is_file() and "__pycache__" not in p.parts:
                data = p.read_bytes()
                for b in bad:
                    self.assertNotIn(b, data, "%s in %s" % (b.decode(), p))


if __name__ == "__main__":
    unittest.main()
