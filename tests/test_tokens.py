"""Task 22: colour/type tokens, base styles, the dev viewer page, and the SS core's JS tests."""
import colorsys
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "second_scribe" / "app" / "static"
TOKENS = STATIC / "css" / "tokens.css"
BASE = STATIC / "css" / "base.css"
VIEWER = STATIC / "dev" / "viewer.html"
EN = ROOT / "second_scribe" / "app" / "strings" / "en.json"

# "Three Lights" design system (design handoff, 23 Oct 2026; replaces spec section 14 "Lab notebook").
# name -> (light, dark). Dark is the default (:root); light is data-theme="light".
COLOURS = {
    "ground": ("#f6f5f1", "#0f1216"), "panel": ("#ffffff", "#151920"), "panel-2": ("#efede8", "#1d222a"),
    "band": ("#eeece6", "#12161b"), "lane": ("#f3f1ec", "#1a1e25"), "input": ("#fbfaf7", "#0b0d10"),
    "rule": ("#e3e1da", "#222831"), "border": ("#cfccc4", "#39404a"), "field-border": ("#80858c", "#646c77"), "ink": ("#14171c", "#ece8df"),
    "ink-2": ("#4f555e", "#b7bcc4"), "navy": ("#14171c", "#ece8df"), "muted": ("#61676f", "#8b9099"),
    "ai": ("#1a7386", "#6cc6d9"), "ai-fill": ("#e8f1f3", "#21313a"), "missing": ("#d4553a", "#f2765a"),
    "missing-text": ("#b83f26", "#f2765a"), "script": ("#e7e4dc", "#262b33"),
    "script-text": ("#4f555e", "#b7bcc4"), "never": ("#80868f", "#6f7680"),
    "synthetic-bg": ("#f0d78a", "#f0d78a"), "synthetic-text": ("#3d3000", "#3d3000"),
    "inverse": ("#14171c", "#ece8df"), "on-inverse": ("#f6f5f1", "#0f1216"),
    "muted-on-inverse": ("#b7bcc4", "#4f555e"), "ai-on-inverse": ("#6cc6d9", "#176d80"),
    "missing-on-inverse": ("#f2765a", "#a8391f"),
}
FONTS = {"display": '"Schibsted Grotesk"', "sans": '"Schibsted Grotesk"', "mono": '"JetBrains Mono"'}


def parse_blocks(css):
    """Return [(selector_chain_tuple, {prop: value})] for every rule with declarations, nested at-rules included."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []

    def walk(text, chain):
        i = 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                break
            head = " ".join(text[i:j].split())
            depth, k = 1, j + 1
            while depth and k < len(text):
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[j + 1:k - 1]
            if "{" in body:
                walk(body, chain + (head,))
            else:
                decls = {}
                for d in body.split(";"):
                    if ":" in d:
                        p, v = d.split(":", 1)
                        decls[p.strip()] = " ".join(v.split())
                out.append((chain + (head,), decls))
            i = k

    walk(css, ())
    return out


def token_sets():
    """(light, dark_default, dark_attr): the data-theme="light" block, :root (dark is the default) and the explicit
    data-theme="dark" block. Colour tokens only for the dark sets; :root also carries the font and radius tokens."""
    blocks = parse_blocks(TOKENS.read_text(encoding="utf-8"))
    light = dark_root = dark_attr = None
    for chain, decls in blocks:
        if chain == (":root",):
            dark_root = decls
        elif chain == (':root[data-theme="light"]',):
            light = decls
        elif chain == (':root[data-theme="dark"]',):
            dark_attr = decls
    if dark_root is not None:
        dark_root = {k: v for k, v in dark_root.items() if k not in FONT_PROPS | RADIUS_PROPS | SPACE_PROPS}
    return light, dark_root, dark_attr


def root_decls():
    return next(d for c, d in parse_blocks(TOKENS.read_text(encoding="utf-8")) if c == (":root",))


def lum(hex_):
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hsl(hex_):
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, l


def colours(mode):
    light, dark_media, _ = token_sets()
    src = light if mode == "light" else dark_media
    return {k: src["--" + k].lower() for k in COLOURS}


class TestTokens(unittest.TestCase):
    def test_token_values_match_spec(self):
        light, dark_media, dark_attr = token_sets()
        self.assertIsNotNone(light, '[data-theme="light"] block')
        self.assertIsNotNone(dark_media, ":root block (dark is the default)")
        self.assertIsNotNone(dark_attr, '[data-theme="dark"] block')
        for name, (lt, dk) in COLOURS.items():
            with self.subTest(token=name):
                self.assertEqual(light["--" + name].lower(), lt)
                self.assertEqual(dark_media["--" + name].lower(), dk)
                self.assertEqual(dark_attr["--" + name].lower(), dk)

    def test_dark_blocks_identical_and_complete(self):
        light, dark_media, dark_attr = token_sets()
        self.assertEqual(dark_media, dark_attr)
        self.assertEqual(set(dark_media), {"--" + k for k in COLOURS})
        # every colour token has a light value; fonts, radii and spacing (Part 6) are mode-independent and live in
        # :root only
        self.assertEqual(set(light), {"--" + k for k in COLOURS})
        self.assertTrue(FONT_PROPS | RADIUS_PROPS | SPACE_PROPS <= set(root_decls()))

    def test_font_tokens(self):
        root = root_decls()
        for name, fam in FONTS.items():
            with self.subTest(font=name):
                self.assertIn(fam, root["--" + name])
        self.assertIn("sans-serif", root["--display"].split(",")[-1])
        self.assertEqual(root["--serif"], "var(--display)")   # the old heading token now reads the display face
        self.assertIn("monospace", root["--mono"])

    def test_text_contrast_both_modes(self):
        for mode in ("light", "dark"):
            c = colours(mode)
            text_pairs = [(t, g) for t in ("ink", "muted", "navy") for g in ("ground", "panel", "lane", "input")]
            text_pairs += [("missing-text", g) for g in ("ground", "panel", "lane")]
            text_pairs += [("script-text", "script"), ("synthetic-text", "synthetic-bg"), ("ink", "ai-fill")]
            text_pairs += [(t, g) for t in ("ink", "ink-2", "muted") for g in ("panel-2", "band")]
            text_pairs += [("ai", g) for g in ("ground", "panel")] + [("ground", "ai")]   # cyan text; text on the primary button
            text_pairs += [(t, "inverse") for t in ("on-inverse", "muted-on-inverse", "ai-on-inverse", "missing-on-inverse")]
            for fg, bg in text_pairs:
                with self.subTest(mode=mode, fg=fg, bg=bg):
                    self.assertGreaterEqual(contrast(c[fg], c[bg]), 4.5, f"{fg} on {bg} = {contrast(c[fg], c[bg]):.2f}")
            for fg in ("ai", "missing", "never"):
                with self.subTest(mode=mode, border=fg):
                    self.assertGreaterEqual(contrast(c[fg], c["lane"]), 3.0, f"{fg} on lane = {contrast(c[fg], c['lane']):.2f}")
            for bg in ("input", "ground", "panel", "panel-2"):   # text fields and selects: WCAG 1.4.11, 3:1
                with self.subTest(mode=mode, field_on=bg):
                    self.assertGreaterEqual(contrast(c["field-border"], c[bg]), 3.0, f"field-border on {bg}")

    def test_no_red_but_missing(self):
        for mode in ("light", "dark"):
            c = colours(mode)
            red_hue = hsl(c["missing"])[0]
            allowed = {"missing", "missing-text", "missing-on-inverse"}
            for name, value in c.items():
                if name in allowed:
                    continue
                h, s, _ = hsl(value)
                dist = min(abs(h - red_hue), 360 - abs(h - red_hue))
                with self.subTest(mode=mode, token=name):
                    self.assertFalse(dist <= 20 and s > 0.40, f"{name} {value} looks red (hue {h:.0f}, sat {s:.2f})")

    def test_missing_text_is_red_family(self):
        # the exception in the test above must itself be a red, otherwise the allowance hides a gap
        for mode in ("light", "dark"):
            c = colours(mode)
            for name in ("missing", "missing-text", "missing-on-inverse"):
                h, s, _ = hsl(c[name])
                self.assertTrue(min(h, 360 - h) <= 20 and s > 0.40, (mode, name, h, s))


FONT_PROPS = {"--display", "--serif", "--sans", "--mono"}
RADIUS_PROPS = {"--radius-chip", "--radius-input", "--radius-card", "--radius-pill"}
# Part 6: the spacing scale, the named gaps, the card padding and gap, the body leading and the text measure
SPACE_PROPS = {"--space-%d" % i for i in range(1, 9)} | {
    "--gap-heading-hint", "--gap-hint-controls", "--gap-block", "--card-pad", "--card-gap", "--choice-gap",
    "--leading-body", "--measure"}


class TestBase(unittest.TestCase):
    def setUp(self):
        self.css = BASE.read_text(encoding="utf-8")
        self.blocks = parse_blocks(self.css)

    def decls(self, selector):
        merged = {}
        for chain, d in self.blocks:
            if chain[-1] == selector and len(chain) == 1:
                merged.update(d)
        return merged

    def test_body(self):
        body = self.decls("body")
        self.assertEqual(body["font-size"], "15px")
        self.assertIn("var(--sans)", body["font-family"])
        self.assertEqual(body["background"], "var(--ground)")
        self.assertEqual(body["color"], "var(--ink)")

    def test_primary_button_min_height(self):
        self.assertEqual(self.decls("button.primary")["min-height"], "44px")

    def test_focus_ring(self):
        ring = self.decls(":focus-visible")
        self.assertEqual(ring["outline"], "2px solid var(--ai)")

    def test_reduced_motion(self):
        inner = [(c, d) for c, d in self.blocks if c[0] == "@media (prefers-reduced-motion: reduce)"]
        self.assertTrue(inner, "reduced-motion media block")
        text = " ".join(f"{p}:{v}" for _, d in inner for p, v in d.items())
        self.assertIn("transition", text)
        self.assertRegex(text, r"transition[-a-z]*:\s*none|transition[-a-z]*:\s*0")

    def test_min_text_13px(self):
        for m in re.finditer(r"font-size:\s*([0-9.]+)(px|rem|em|%)", self.css):
            val, unit = float(m.group(1)), m.group(2)
            self.assertEqual(unit, "px", "base.css sizes text in px so the 13px floor is checkable")
            self.assertGreaterEqual(val, 13, m.group(0))

    def test_no_remote_and_no_hardcoded_colours(self):
        for name, css in (("base.css", self.css), ("tokens.css", TOKENS.read_text(encoding="utf-8"))):
            self.assertNotRegex(css, r"https?://|@import|//cdn", name)
        # base.css uses tokens only, so dark mode cannot be bypassed by a stray hex colour
        self.assertNotRegex(self.css, r"#[0-9a-fA-F]{3,8}\b")


class TestViewerDevPage(unittest.TestCase):
    def setUp(self):
        self.html = VIEWER.read_text(encoding="utf-8")

    def test_loads_local_assets_in_order(self):
        order = [self.html.index(p) for p in ("css/fonts.css", "css/tokens.css", "css/base.css", "js/core.js")]
        self.assertEqual(order, sorted(order))
        for ref in re.findall(r'(?:href|src)="([^"]+)"', self.html):
            if ref.startswith("#"):
                continue
            self.assertNotRegex(ref, r"^(https?:)?//", ref)
            self.assertTrue((VIEWER.parent / ref).resolve().is_file(), ref)

    def test_inline_strings_match_en_json(self):
        m = re.search(r"window\.SS_STRINGS\s*=\s*(\{.*?\});", self.html, re.S)
        self.assertIsNotNone(m, "dev page inlines the strings it uses")
        inline = json.loads(m.group(1))
        en = json.loads(EN.read_text(encoding="utf-8"))
        for k, v in inline.items():
            self.assertEqual(en.get(k), v, k)

    def test_no_dom_injection(self):
        for bad in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function"):
            self.assertNotIn(bad, self.html)

    def test_dark_theme_hook(self):
        self.assertIn("theme=dark", self.html)
        self.assertIn("data-theme", self.html)


@unittest.skipUnless(shutil.which("node"), "node is needed for the JS tests")
class TestJsCore(unittest.TestCase):
    def test_node_tests_pass(self):
        # Node 22 reads a directory argument as a file, so the test files are listed explicitly.
        files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "js").glob("*.test.js"))
        self.assertTrue(files)
        proc = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
