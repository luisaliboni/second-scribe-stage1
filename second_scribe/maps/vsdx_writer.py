"""Writes minimal .vsdx files for tests and demos (synthetic maps only)."""
from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape, quoteattr

NS = "http://schemas.microsoft.com/office/visio/2012/main"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>')


def _cell(name: str, value) -> str:
    return f"<Cell N={quoteattr(name)} V={quoteattr(str(value))}/>"


def _shape_xml(s: dict) -> str:
    sid = str(s["id"])
    attrs = f"ID={quoteattr(sid)} NameU={quoteattr('Shape' + sid)}"
    if s.get("master"):
        attrs += f" Master={quoteattr(str(s['master']))}"
    pin = s.get("pin", (0, 0))         # pin=None writes a shape with no position at all
    cells = [] if pin is None else [_cell("PinX", pin[0]), _cell("PinY", pin[1])]
    cells += [_cell("Width", s.get("w", 1)), _cell("Height", s.get("h", 0.5))]
    if s.get("line"):
        cells.append(_cell("LineColor", s["line"]))
    if s.get("fill"):
        cells.append(_cell("FillForegnd", s["fill"]))
    if s.get("connector") or s.get("begin") or s.get("end"):
        (bx, by), (ex, ey) = s["begin"], s["end"]
        cells += [_cell("BeginX", bx), _cell("BeginY", by), _cell("EndX", ex), _cell("EndY", ey)]
    char = s.get("char")               # text colour(s): one Character row per colour
    char = [char] if isinstance(char, str) else (char or [])
    section = ""
    if char:
        rows = "".join(f'<Row IX="{i}">{_cell("Color", c)}</Row>' for i, c in enumerate(char))
        section = f'<Section N="Character">{rows}</Section>'
    text = f"<Text>{escape(s['text'])}</Text>" if s.get("text") else ""
    return f"<Shape {attrs}>{''.join(cells)}{section}{text}</Shape>"


def write_vsdx(path, shapes: list[dict], connects: list[dict], masters: dict[str, str] | None = None) -> None:
    """shapes: [{"id", "text", "pin": (x, y), "w", "h", "line", "fill", "master",
                 "connector": bool, "begin": (x, y), "end": (x, y), "char": "#rrggbb" | [..]}]
       (pin=None omits PinX/PinY)
       connects: [{"from_sheet", "from_cell": "BeginX"|"EndX", "to_sheet"}]
       masters: {"1": "Process"} -> visio/masters/masters.xml (NameU)"""
    shapes_xml = "".join(_shape_xml(s) for s in shapes)
    connects_xml = "".join(
        f"<Connect FromSheet={quoteattr(str(c['from_sheet']))} FromCell={quoteattr(c['from_cell'])} "
        f"ToSheet={quoteattr(str(c['to_sheet']))} ToCell=\"PinX\"/>" for c in connects)
    page = (f'<?xml version="1.0" encoding="UTF-8"?><PageContents xmlns="{NS}">'
            f"<Shapes>{shapes_xml}</Shapes><Connects>{connects_xml}</Connects></PageContents>")
    files = {"[Content_Types].xml": _CONTENT_TYPES, "visio/pages/page1.xml": page}
    if masters:
        ms = "".join(f"<Master ID={quoteattr(str(i))} NameU={quoteattr(n)}/>" for i, n in masters.items())
        files["visio/masters/masters.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?><Masters xmlns="{NS}">{ms}</Masters>')
    # Byte-deterministic output: fixed timestamp, compression, attributes and member order.
    with zipfile.ZipFile(path, "w") as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, files[name])
