#!/bin/sh
# Dev-only: headless Chrome screenshot. Usage: tools/shot.sh URL OUT.png WIDTH HEIGHT
# Dark mode and reduced motion are requested through the URL hash (#theme=dark, #motion=reduce).
#
# Headless Chrome lays a page out at 500 px at least: a plain 390 px window shows a 500 px layout cropped to 390,
# which looks like overflow. So for WIDTH under 500 this puts the page in a WIDTH px iframe (a true WIDTH px layout,
# the method of tests/test_mobile_width.py), shoots a 500 px window and crops the PNG back to WIDTH with sips
# (macOS). The page must allow being framed: the offline site does; the app does not (it is desktop only).
set -eu
if [ "$#" -ne 4 ]; then
  echo "usage: $0 URL OUT.png WIDTH HEIGHT" >&2
  exit 2
fi
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
url=$1 out=$2 w=$3 h=$4
if [ "$w" -ge 500 ]; then
  exec "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="$w,$h" --screenshot="$out" "$url"
fi
dir=$(mktemp -d "${TMPDIR:-/tmp}/ss-tests-shot.XXXXXX")
src=$(printf '%s' "$url" | sed -e 's/&/\&amp;/g' -e 's/"/\&quot;/g' -e 's/</\&lt;/g')
# The frame sits at (1, 1): sips reads a crop offset of 0 0 as "centre", so the crop starts at 1 1 instead.
printf '<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff">\n<iframe src="%s" style="position:absolute;left:1px;top:1px;width:%spx;height:%spx;border:0"></iframe>\n' \
  "$src" "$w" "$h" > "$dir/frame.html"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="501,$((h + 1))" --screenshot="$dir/wide.png" \
  "file://$dir/frame.html" 2>/dev/null
sips --cropOffset 1 1 -c "$h" "$w" "$dir/wide.png" --out "$out" >/dev/null
rm -f "$dir/frame.html" "$dir/wide.png"
rmdir "$dir"
