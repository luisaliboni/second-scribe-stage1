# Bundled fonts: provenance

All files are data (woff2 and licence text). Nothing is loaded from a CDN: the site and the app read these files from
this folder. The woff2 files are the variable-weight Latin subsets that the Google Fonts CSS API serves for each family
(Google makes the subset; we did not convert or subset anything). They were retrieved on 2026-10-22 with a desktop
Chrome user agent from `https://fonts.googleapis.com/css2?family=<family>:wght@<range>&display=swap`, block `/* latin */`.
The licence texts are the OFL files of the google/fonts repository.

## Files

| File | Family, axis | Download URL | sha256 |
|---|---|---|---|
| `SchibstedGrotesk-Variable-latin.woff2` | Schibsted Grotesk, wght 400 to 900 (Google Fonts v7) | https://fonts.gstatic.com/s/schibstedgrotesk/v7/Jqz55SSPQuCQF3t8uOwiUL-taUTtap9GayojdSFO.woff2 | `e3b56e90510a84ac0ed465b822e112983eaf58e37436bf769681c31f77b1f3a7` |
| `JetBrainsMono-Variable-latin.woff2` | JetBrains Mono, wght 400 to 800 (Google Fonts v24) | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2 | `2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4` |

Latin subset: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329,
U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD. Other characters (arrows, the triangle
mark, math signs) come from the system fallback in the font stack.

## Licence files (SIL Open Font License 1.1)

| File | Download URL | sha256 |
|---|---|---|
| `OFL-SchibstedGrotesk.txt` | https://raw.githubusercontent.com/google/fonts/main/ofl/schibstedgrotesk/OFL.txt | `3b4f3063b6ac7c1e403e2c4a5e8ef3a58190ff83ed7b15af66511858699139ce` |
| `OFL-JetBrainsMono.txt` | https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/OFL.txt | `b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d` |

Glyph check: `fact ff fi fl g a y` rendered at 150px at weights 400, 600 and 900 (the f rises straight and ends on the
baseline; the weights differ, so the axis is present in the file).
