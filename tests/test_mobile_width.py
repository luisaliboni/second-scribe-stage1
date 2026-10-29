"""At 390 px nothing on the offline site is wider than the screen (audit fix C follow-up).

Headless Chrome lays a page out at 500 px at least, so a 390 px window shows a 500 px layout cropped to 390 and
looks like overflow. The check therefore puts each page in a 390 px iframe (a true 390 px layout) on a loopback
server and reads ``document.documentElement.scrollWidth`` and the right edge of every element that is not inside
a scroll container of its own. Skipped when Chrome is not installed. No model, no outside address.
"""
import functools
import http.server
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_build_site import build

WIDTH = 390
CHROME = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chromium-browser") or "",
]
PROBE = """<!doctype html><meta charset="utf-8"><body style="margin:0">
<iframe id="f" src="%(page)s" style="width:%(w)dpx;height:800px;border:0"></iframe><pre id="out">pending</pre>
<script>
setTimeout(function () {
  var o = document.getElementById('out');
  try {
    var d = document.getElementById('f').contentDocument, de = d.documentElement, cw = de.clientWidth, wide = [];
    function scrolls(n) { for (; n && n !== d.body; n = n.parentNode) {
      var s = d.defaultView.getComputedStyle(n); if (/(auto|scroll|hidden)/.test(s.overflowX)) return true; } return false; }
    d.querySelectorAll('body *').forEach(function (n) {
      var r = n.getBoundingClientRect();
      if (r.right > cw + 1 && !scrolls(n) && wide.length < 20) wide.push(n.tagName + '.' + (typeof n.className === 'string' ? n.className : '') + ' ' + Math.round(r.right));
    });
    o.textContent = 'scrollWidth=' + de.scrollWidth + ' clientWidth=' + cw + ' cells=' + d.querySelectorAll('.pf-cell').length + '\\n' + wide.join('\\n');
  } catch (e) { o.textContent = 'ERR ' + e; }
}, 2500);
</script>
"""


def chrome() -> str | None:
    return next((c for c in CHROME if c and Path(c).exists()), None)


class TestMobileWidth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chrome = chrome()
        if cls.chrome is None:
            raise unittest.SkipTest("Chrome is not installed")
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        cls.site = Path(cls._tmp.name) / "site"
        from tests.test_build_site import STUDY_PROFILE
        build(cls.site, study=STUDY_PROFILE)

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

        handler = functools.partial(Quiet, directory=str(cls.site))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def measure(self, page: str) -> str:
        (self.site / "probe.html").write_text(PROBE % {"page": page, "w": WIDTH}, encoding="utf-8")
        out = subprocess.run(
            [self.chrome, "--headless=new", "--disable-gpu", "--virtual-time-budget=10000", "--dump-dom",
             f"http://127.0.0.1:{self.port}/probe.html"], capture_output=True, text=True, timeout=90).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', out, re.DOTALL)
        self.assertIsNotNone(m, "no result from the probe page")
        return m.group(1)

    def check(self, page: str):
        res = self.measure(page)
        self.assertNotIn("ERR", res, res)
        m = re.search(r"scrollWidth=(\d+) clientWidth=(\d+) cells=(\d+)", res)
        self.assertIsNotNone(m, res)
        scroll, client = int(m.group(1)), int(m.group(2))
        self.assertLessEqual(scroll, WIDTH, f"{page} scrolls sideways at {WIDTH}px: {res}")
        self.assertEqual(scroll, client, res)
        over = [ln for ln in res.splitlines()[1:] if ln.strip()]
        self.assertEqual(over, [], f"{page}: elements past the right edge at {WIDTH}px")
        return int(m.group(3))

    def test_study_page_fits_390(self):
        self.assertGreater(self.check("study.html"), 100)      # the profile really rendered

    def test_try_it_page_fits_390(self):
        self.assertGreater(self.check("try.html"), 20)

    def test_install_and_home_fit_390(self):
        self.check("install.html")
        self.check("index.html")


if __name__ == "__main__":
    unittest.main()
