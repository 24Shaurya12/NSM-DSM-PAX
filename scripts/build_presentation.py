#!/usr/bin/env python3
"""Builds the presentation (results/presentation/NSM_DSM_PAX_layout_advisor.pptx) from
results/presentation/deck_data.json (scripts/deck_data.py). Every chart is a native PowerPoint
chart and every diagram is native shapes, so all graphics are vector and editable.

  python3 scripts/deck_data.py && PYTHONPATH=<dir with python-pptx> python3 scripts/build_presentation.py
"""
import copy
import json
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_MARKER_STYLE, XL_LABEL_POSITION
from pptx.enum.dml import MSO_LINE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

REPO = Path(__file__).resolve().parent.parent
D = json.loads((REPO / "results/presentation/deck_data.json").read_text())
OUT = REPO / "results/presentation/NSM_DSM_PAX_layout_advisor.pptx"

# ---------------------------------------------------------------- palette and type
INK = RGBColor(0x14, 0x1B, 0x2D)        # dark background / headings
BODY = RGBColor(0x37, 0x41, 0x51)       # body text
MUTED = RGBColor(0x64, 0x6B, 0x78)      # captions
RULE = RGBColor(0xD9, 0xDC, 0xE1)       # hairlines, gridlines
CARD = RGBColor(0xF3, 0xF5, 0xF8)       # card tint on white
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DARK_TEXT = RGBColor(0xE8, 0xEC, 0xF2)  # body text on INK
DARK_MUTED = RGBColor(0xA9, 0xB3, 0xC4)
NSM = RGBColor(0x2A, 0x78, 0xD6)
DSM = RGBColor(0xEB, 0x68, 0x34)
PAX = RGBColor(0x1B, 0xAF, 0x7A)
ADV = RGBColor(0x62, 0x50, 0xD6)
GOLD = RGBColor(0xED, 0xA1, 0x00)
GREEN = RGBColor(0x00, 0x83, 0x00)
TINT = {"nsm": RGBColor(0xC6, 0xDC, 0xF6), "dsm": RGBColor(0xF9, 0xD0, 0xBF),
        "pax": RGBColor(0xB4, 0xE8, 0xD3)}
LAYOUT = {"nsm": NSM, "dsm": DSM, "pax": PAX}
HEAD, TEXT, MONO = "Cambria", "Calibri", "Courier New"

W, H = Inches(13.333), Inches(7.5)
ML = Inches(0.6)                         # side margin
CW = W - 2 * ML                          # content width

prs = Presentation()
prs.slide_width, prs.slide_height = W, H
BLANK = prs.slide_layouts[6]
page_no = [0]


# ---------------------------------------------------------------- helpers
def bg(slide, color):
    f = slide.background.fill
    f.solid()
    f.fore_color.rgb = color


def add_runs(p, t, set_font):
    """Adds text t to paragraph p; '\n' becomes a line break (not a literal character)."""
    for k, piece in enumerate(t.split("\n")):
        if k:
            p.add_line_break()
        r = p.add_run()
        r.text = piece
        set_font(r.font)


def text(slide, x, y, w, h, runs, size=16, color=BODY, font=TEXT, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, italic=False, spacing=None, name=None):
    """runs: str, or list of paragraphs; a paragraph is str or list of (text, {opts})."""
    tb = slide.shapes.add_textbox(x, y, w, h)
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    paras = runs if isinstance(runs, list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if spacing:
            p.space_after = Pt(spacing)
        parts = para if isinstance(para, list) else [(para, {})]
        for t, o in parts:
            def setf(f, o=o):
                f.name = o.get("font", font)
                f.size = Pt(o.get("size", size))
                f.bold = o.get("bold", bold)
                f.italic = o.get("italic", italic)
                f.color.rgb = o.get("color", color)
            add_runs(p, t, setf)
    return tb


def bullets(slide, x, y, w, h, items, size=16, color=BODY, gap=8):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, it in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        level = 1 if it.startswith("  ") else 0
        it = it.strip()
        pPr = p._p.get_or_add_pPr()
        indent = 228600 if level == 0 else 457200
        pPr.set("marL", str(indent))
        pPr.set("indent", str(-228600))
        bu = etree.SubElement(pPr, qn("a:buChar"))
        bu.set("char", "•" if level == 0 else "–")
        p.space_after = Pt(gap)
        parts = it.split("**")
        for j, part in enumerate(parts):
            if not part:
                continue
            r = p.add_run()
            r.text = part
            r.font.name = TEXT
            r.font.size = Pt(size if level == 0 else size - 2)
            r.font.bold = j % 2 == 1
            r.font.color.rgb = color if level == 0 else MUTED
    return tb


def box(slide, x, y, w, h, fill=CARD, line=None, radius=True, shape=None):
    shp = slide.shapes.add_shape(shape or (MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE),
                                 x, y, w, h)
    if radius and shape is None:
        shp.adjustments[0] = 0.08
    if fill is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(1)
    shp.shadow.inherit = False
    return shp


def boxed_text(slide, x, y, w, h, runs, fill=CARD, line=None, size=16, color=BODY, bold=False,
               align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, font=TEXT, pad=0.12):
    shp = box(slide, x, y, w, h, fill, line)
    tf = shp.text_frame
    tf.word_wrap = True
    for m in ("margin_left", "margin_right"):
        setattr(tf, m, Inches(pad))
    tf.margin_top = tf.margin_bottom = Inches(0.05)
    tf.vertical_anchor = anchor
    paras = runs if isinstance(runs, list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        parts = para if isinstance(para, list) else [(para, {})]
        for t, o in parts:
            def setf(f, o=o):
                f.name = o.get("font", font)
                f.size = Pt(o.get("size", size))
                f.bold = o.get("bold", bold)
                f.color.rgb = o.get("color", color)
            add_runs(p, t, setf)
    return shp


def arrow(slide, x1, y1, x2, y2, color=MUTED, width=1.5, dash=False):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    c.line.color.rgb = color
    c.line.width = Pt(width)
    if dash:
        c.line.dash_style = MSO_LINE.DASH
    ln = c.line._get_or_add_ln()
    tail = etree.SubElement(ln, qn("a:tailEnd"))
    tail.set("type", "triangle")
    tail.set("w", "med")
    tail.set("len", "med")
    return c


def header(slide, eyebrow, title, dark=False):
    text(slide, ML, Inches(0.42), CW, Inches(0.3), eyebrow.upper(), size=12, bold=True,
         color=GOLD if dark else DSM, name="eyebrow")
    text(slide, ML, Inches(0.72), CW, Inches(0.6), title, size=28, bold=True, font=HEAD,
         color=WHITE if dark else INK, name="title")


def footer(slide, note=None, dark=False):
    page_no[0] += 1
    c = DARK_MUTED if dark else MUTED
    text(slide, W - ML - Inches(1.0), Inches(7.0), Inches(1.0), Inches(0.25), str(page_no[0]),
         size=10, color=c, align=PP_ALIGN.RIGHT)
    if note:
        text(slide, ML, Inches(7.0), Inches(10.9), Inches(0.25), note, size=10, color=c)


def notes(slide, s):
    slide.notes_slide.notes_text_frame.text = s


def new_slide(dark=False):
    s = prs.slides.add_slide(BLANK)
    bg(s, INK if dark else WHITE)
    return s


def style_chart(chart, legend=True, size=12, legend_pos=XL_LEGEND_POSITION.TOP):
    chart.font.name = TEXT
    chart.font.size = Pt(size)
    chart.font.color.rgb = BODY
    chart.has_legend = legend
    if legend:
        chart.legend.position = legend_pos
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(size)


def quiet_axes(chart, val_title=None, cat_title=None, gridlines=True, size=12):
    va, ca = chart.value_axis, chart.category_axis
    for ax in (va, ca):
        ax.format.line.color.rgb = RULE
        ax.tick_labels.font.size = Pt(size)
        ax.tick_labels.font.color.rgb = MUTED
    va.has_major_gridlines = gridlines
    if gridlines:
        va.major_gridlines.format.line.color.rgb = RULE
        va.major_gridlines.format.line.width = Pt(0.75)
    for ax, t in ((va, val_title), (ca, cat_title)):
        if t:
            ax.has_title = True
            ax.axis_title.text_frame.text = t
            r = ax.axis_title.text_frame.paragraphs[0].runs[0]
            r.font.size = Pt(size)
            r.font.bold = False
            r.font.color.rgb = MUTED


def log_axis(axis, base=10, minimum=None, maximum=None):
    sc = axis._element.find(qn("c:scaling"))
    lb = sc.find(qn("c:logBase"))
    if lb is None:
        lb = etree.Element(qn("c:logBase"))
        sc.insert(0, lb)
    lb.set("val", str(base))
    if minimum is not None:
        axis.minimum_scale = minimum
    if maximum is not None:
        axis.maximum_scale = maximum


def color_series(chart, colors, line=False, width=2.25, dashes=None, markers=None):
    for i, (s, c) in enumerate(zip(chart.plots[0].series, colors)):
        if line:
            s.format.line.color.rgb = c
            s.format.line.width = Pt(width)
            s.smooth = False
            if dashes and dashes[i]:
                s.format.line.dash_style = MSO_LINE.DASH
            mk = markers[i] if markers else XL_MARKER_STYLE.CIRCLE
            s.marker.style = mk
            if mk != XL_MARKER_STYLE.NONE:
                s.marker.size = 7
                s.marker.format.fill.solid()
                s.marker.format.fill.fore_color.rgb = c
                s.marker.format.line.color.rgb = WHITE
        else:
            s.format.fill.solid()
            s.format.fill.fore_color.rgb = c
            s.format.line.fill.background()


def bar_gap(chart, gap=60, overlap=0):
    plot = chart.plots[0]
    plot.gap_width = gap
    plot.overlap = overlap


def labels(chart, fmt="0.0", size=11, pos=XL_LABEL_POSITION.OUTSIDE_END):
    p = chart.plots[0]
    p.has_data_labels = True
    dl = p.data_labels
    dl.number_format = fmt
    dl.number_format_is_linked = False
    dl.font.size = Pt(size)
    dl.font.color.rgb = BODY
    dl.position = pos


def stat(slide, x, y, w, big, caption, color=INK, cap_color=MUTED, big_size=44):
    bh = Inches(big_size * 1.25 / 72)
    text(slide, x, y, w, bh, big, size=big_size, bold=True, font=HEAD, color=color)
    text(slide, x, y + bh + Inches(0.05), w, Inches(0.7), caption, size=14, color=cap_color)


def table(slide, x, y, w, rows, col_w, size=13, header_fill=INK, zebra=True, bold_first_col=False,
          row_h=0.36, align=None):
    nr, nc = len(rows), len(rows[0])
    shp = slide.shapes.add_table(nr, nc, x, y, w, Inches(row_h * nr))
    tbl = shp.table
    tbl.first_row = True
    tblPr = tbl._tbl.tblPr
    style = tblPr.find(qn("a:tableStyleId"))
    if style is None:
        style = etree.SubElement(tblPr, qn("a:tableStyleId"))
    style.text = "{5940675A-B579-460E-94D1-54222C63F5DA}"  # "No Style, Table Grid": we paint cells
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = Inches(cw)
    for i, row in enumerate(rows):
        tbl.rows[i].height = Inches(row_h)
        for j, val in enumerate(row):
            cell = tbl.cell(i, j)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            bold = False
            if isinstance(val, tuple):
                val, bold = val
            tf = cell.text_frame
            tf.text = str(val)
            p = tf.paragraphs[0]
            p.alignment = (align[j] if align else (PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT))
            r = p.runs[0]
            r.font.size = Pt(size)
            r.font.name = TEXT
            cell.fill.solid()
            if i == 0:
                cell.fill.fore_color.rgb = header_fill
                r.font.color.rgb = WHITE
                r.font.bold = True
            else:
                cell.fill.fore_color.rgb = CARD if (zebra and i % 2 == 0) else WHITE
                r.font.color.rgb = BODY
                r.font.bold = bold or (bold_first_col and j == 0)
            _cell_borders(cell)
    return shp


def _cell_borders(cell):
    tcPr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        ln = etree.SubElement(tcPr, qn(tag))
        ln.set("w", "6350")
        sf = etree.SubElement(ln, qn("a:solidFill"))
        c = etree.SubElement(sf, qn("a:srgbClr"))
        c.set("val", "D9DCE1")


def short(storage):
    if storage.startswith("pax:page="):
        return storage.split("=")[1]
    return storage.upper()


# ================================================================= slides
def s_title():
    s = new_slide(dark=True)
    text(s, ML, Inches(1.2), Inches(9), Inches(0.35), "COL868 · COURSE PROJECT", size=14, bold=True,
         color=GOLD)
    text(s, ML, Inches(1.65), Inches(11.5), Inches(1.0), "Row, Column or Both?", size=48, bold=True,
         font=HEAD, color=WHITE)
    text(s, ML, Inches(2.75), CW, Inches(0.6),
         "Measuring NSM, DSM and PAX, and predicting which to use", size=26, font=HEAD,
         color=DARK_TEXT)
    # the three layouts as a motif: rows, columns, pages
    y = Inches(4.35)
    for i, (name, c) in enumerate((("NSM", NSM), ("DSM", DSM), ("PAX", PAX))):
        x = ML + Inches(i * 1.55)
        dot = box(s, x, y, Inches(0.32), Inches(0.32), c, shape=MSO_SHAPE.OVAL)
        text(s, x + Inches(0.42), y - Inches(0.02), Inches(1.0), Inches(0.36), name, size=18,
             bold=True, color=WHITE)
    text(s, ML, Inches(5.3), Inches(11), Inches(0.4), "[Team member names]  ·  [Date]", size=16,
         color=DARK_MUTED)
    text(s, ML, Inches(5.75), Inches(11), Inches(0.4),
         "In-memory scan engine · hardware counters · calibrated cost model · layout advisor",
         size=16, color=DARK_MUTED)
    notes(s, "Two parts: first we measure where each storage layout wins and explain why with "
             "hardware counters; then we turn those mechanisms into a model that recommends a "
             "layout for a given machine, workload and data, and we check its picks against "
             "10,368 measurements.")


def s_question():
    s = new_slide()
    header(s, "The question", "Which layout and parameters, on which machine?")
    # inputs -> advisor -> outputs
    y0 = Inches(2.1)
    ins = [("Machine", "caches, bandwidth, prefetchers, TLB"),
           ("Workload", "columns, selectivity, lookups, mix"),
           ("Data", "rows, width, value distribution")]
    for i, (t, d) in enumerate(ins):
        y = y0 + Inches(i * 1.25)
        boxed_text(s, ML, y, Inches(3.6), Inches(1.0),
                   [[(t, {"bold": True, "size": 18, "color": INK})], [(d, {"size": 14, "color": MUTED})]],
                   fill=CARD, align=PP_ALIGN.LEFT, pad=0.2)
        arrow(s, ML + Inches(3.65), y + Inches(0.5), Inches(5.15), Inches(3.6))
    boxed_text(s, Inches(5.2), Inches(2.85), Inches(2.9), Inches(1.5),
               [[("Layout advisor", {"bold": True, "size": 20, "color": WHITE})],
                [("calibrated cost model + search", {"size": 14, "color": DARK_TEXT})]],
               fill=INK)
    arrow(s, Inches(8.15), Inches(3.6), Inches(8.85), Inches(3.6))
    outs = [("Layout + parameters", "NSM · DSM · PAX with page size"),
            ("Execution options", "batch size, branch-free, predicate order"),
            ("Predicted time + margin", "and evidence that the pick is right")]
    for i, (t, d) in enumerate(outs):
        boxed_text(s, Inches(8.9), y0 + Inches(i * 1.25), Inches(3.83), Inches(1.0),
                   [[(t, {"bold": True, "size": 18, "color": INK})], [(d, {"size": 14, "color": MUTED})]],
                   fill=CARD, align=PP_ALIGN.LEFT, pad=0.2)
    text(s, ML, Inches(6.1), CW, Inches(0.5),
         [[("Goal 1: ", {"bold": True, "color": INK}),
           ("measure and explain the crossovers with hardware counters.   ", {}),
           ("Goal 2: ", {"bold": True, "color": INK}),
           ("predict the right choice, and prove it with measurements.", {})]], size=16)
    footer(s)
    notes(s, "The same table can be stored row-wise, column-wise, or column-wise inside pages. "
             "The right choice depends on the query, the data, and the machine.")


def s_layouts():
    s = new_slide()
    header(s, "Background", "Three ways to lay out the same 4 × 4 table")
    cols = [NSM, DSM, PAX, GOLD]
    tints = [RGBColor(0xC6, 0xDC, 0xF6), RGBColor(0xF9, 0xD0, 0xBF), RGBColor(0xB4, 0xE8, 0xD3),
             RGBColor(0xFA, 0xE3, 0xA8)]
    names = "abcd"
    cell_w, cell_h = Inches(0.6), Inches(0.42)

    def cell(x, y, col, row):
        boxed_text(s, x, y, cell_w, cell_h, f"{names[col]}{row + 1}", fill=tints[col], size=13,
                   color=INK, bold=True, pad=0.02)

    # NSM: rows back to back
    x0, y0 = ML, Inches(1.6)
    text(s, x0, y0, Inches(4), Inches(0.4), [[("NSM", {"bold": True, "color": NSM}),
                                              ("  rows back to back", {"color": MUTED})]], size=18)
    for row in range(4):
        for col in range(4):
            cell(x0 + (row * 4 + col) * (cell_w + Inches(0.04)) if row < 2 else
                 x0 + ((row - 2) * 4 + col) * (cell_w + Inches(0.04)),
                 y0 + Inches(0.45) + (Inches(0.5) if row >= 2 else 0), col, row)
    text(s, Inches(6.05), y0 + Inches(0.45), Inches(6.6), Inches(1.2),
         ["One row = 4 × 8 B, contiguous.", "Reading one column still drags every other column "
          "through the cache: a 64 B line holds whole rows."], size=15, spacing=4)
    # DSM: one array per column
    y1 = Inches(3.3)
    text(s, x0, y1, Inches(4), Inches(0.4), [[("DSM", {"bold": True, "color": DSM}),
                                              ("  one array per column", {"color": MUTED})]], size=18)
    for col in range(4):
        for row in range(4):
            cell(x0 + (col % 2) * Inches(2.7) + row * (cell_w + Inches(0.04)),
                 y1 + Inches(0.45) + (col // 2) * Inches(0.5), col, row)
    text(s, Inches(6.05), y1 + Inches(0.45), Inches(6.6), Inches(1.2),
         ["A scan of one column reads only that column.",
          "Rebuilding a tuple touches one place in every column array."], size=15, spacing=4)
    # PAX: pages with minipages
    y2 = Inches(5.0)
    text(s, x0, y2, Inches(4), Inches(0.4), [[("PAX", {"bold": True, "color": PAX}),
                                              ("  pages of minipages", {"color": MUTED})]], size=18)
    for page in range(2):
        px = x0 + page * Inches(2.7)
        frame = box(s, px - Inches(0.06), y2 + Inches(0.39), Inches(2.6), Inches(1.06), None, line=MUTED)
        frame.line.dash_style = MSO_LINE.DASH
        for col in range(4):
            for k in range(2):
                row = page * 2 + k
                cell(px + (col % 2) * Inches(1.28) + k * (cell_w + Inches(0.04)),
                     y2 + Inches(0.45) + (col // 2) * Inches(0.5), col, row)
    text(s, Inches(6.05), y2 + Inches(0.45), Inches(6.6), Inches(1.2),
         ["Each page holds whole rows, stored column by column inside it.",
          "Parameter: page size. A minipage holds page ÷ (columns × 8 B) values."], size=15, spacing=4)
    footer(s, "Cell label = column letter + row number; colour = column.")
    notes(s, "NSM keeps rows together, DSM keeps columns together, PAX keeps rows together at "
             "page granularity but columns together inside the page. Cache lines are 64 bytes and "
             "OS pages 4 KB, which is why page size matters for PAX.")


def s_engine():
    s = new_slide()
    header(s, "The engine", "One scan engine, one interface for every layout")
    # pipeline
    steps = [("QuerySpec", "op × columns ×\npredicates × lookups"),
             ("Layout", "NSM · DSM · PAX\n(registry)"),
             ("Operator", "scan · filter · sum ·\nmaterialize · lookup"),
             ("TupleSink", "reconstructed rows,\nchecksum")]
    bw, gap, y = Inches(2.6), Inches(0.53), Inches(1.95)
    for i, (t, d) in enumerate(steps):
        x = ML + i * (bw + gap)
        boxed_text(s, x, y, bw, Inches(1.2),
                   [[(t, {"bold": True, "size": 18, "color": WHITE})],
                    [(d, {"size": 13, "color": DARK_TEXT})]], fill=INK)
        if i < 3:
            arrow(s, x + bw + Inches(0.05), y + Inches(0.6), x + bw + gap - Inches(0.05), y + Inches(0.6))
    # three cards
    cards = [("Execution options, separate from storage",
              ["DSM batch size (256 … 128K rows)", "branchy or branch-free filters",
               "predicate order"]),
             ("Correct by construction",
              ["every result checked against a reference executor",
               "53,000 automated checks; injected faults caught"]),
             ("Measured with counters",
              ["perf_event_open: cycles, branch misses, LLC and dTLB misses",
               "DRAM bytes read from the memory controller"])]
    cw = (CW - 2 * Inches(0.35)) / 3
    for i, (t, items) in enumerate(cards):
        x = ML + i * (cw + Inches(0.35))
        box(s, x, Inches(3.6), cw, Inches(2.85), CARD)
        text(s, x + Inches(0.25), Inches(3.8), cw - Inches(0.5), Inches(0.7), t, size=17, bold=True,
             color=INK)
        bullets(s, x + Inches(0.25), Inches(4.55), cw - Inches(0.5), Inches(1.8), items, size=14, gap=6)
    footer(s, "C++17, int64 columns, -O2 (scalar code as written). Extensible: layouts, operators and "
              "distributions register by name.")
    notes(s, "Storage parameters are fixed per table; execution options can change per query. "
             "That split matters later for the advisor.")


def s_method():
    s = new_slide()
    header(s, "Method", "How we measured")
    items = [("Pinned", "Intel i7-1360P, one performance core, AC power, machine idle"),
             ("Cold", "table flushed from every cache before each run; 5 samples, median"),
             ("Counted", "cycles, instructions, branch / LLC / dTLB misses, DRAM bytes"),
             ("Plain code", "-O2 without extra flags: no auto-vectorisation, scalar loops")]
    cw = (CW - 3 * Inches(0.3)) / 4
    for i, (t, d) in enumerate(items):
        x = ML + i * (cw + Inches(0.3))
        box(s, x, Inches(1.95), cw, Inches(2.4), CARD)
        text(s, x + Inches(0.25), Inches(2.15), cw - Inches(0.5), Inches(0.5), t, size=22, bold=True,
             font=HEAD, color=INK)
        text(s, x + Inches(0.25), Inches(2.8), cw - Inches(0.5), Inches(1.5), d, size=15)
    text(s, ML, Inches(4.75), CW, Inches(0.4), "Why cold?", size=18, bold=True, color=INK)
    text(s, ML, Inches(5.2), Inches(11.5), Inches(1.2),
         "A repeated query on a 256 MB table is partly served from the 18 MB L3, which flatters "
         "narrow scans. Flushing first makes every query start from DRAM, which is also what the "
         "cost model assumes.", size=16)
    footer(s)
    notes(s, "Warm mode exists too; the ground truth uses cold mode so the measurement and the "
             "model start from the same cache state.")


def s_ground_truth():
    s = new_slide()
    header(s, "Ground truth", "10,368 measured points as the answer key")
    stats = [("10,368", "measured points\n(51,840 samples, 100 min)"),
             ("12", "storage configurations\nNSM, DSM, PAX 1 KB … 16 MB"),
             ("134", "query templates\nper table width"),
             ("4.3 %", "median run-to-run\ndeviation (10% re-run)")]
    cw = (CW - 3 * Inches(0.3)) / 4
    for i, (b, c) in enumerate(stats):
        x = ML + i * (cw + Inches(0.3))
        stat(s, x, Inches(1.85), cw, b, c, color=INK)
    rows = [["Dimension", "Values"],
            ["Data sets (256 MB each)", "16 columns uniform · 64 columns uniform · 16 columns with sorted c0"],
            ["Scans", "SUM and SELECT of 1 column, 25 %, 25 % spread, 100 % of columns"],
            ["Filters", "one predicate at 0.1 % … 100 %; two predicates in both orders"],
            ["Lookups", "10³, 10⁴, 10⁵ row ids, uniform or sorted"],
            ["Execution options", "DSM batch 256 / 1K / 16K / 128K × branchy / branch-free"]]
    table(s, ML, Inches(3.75), CW, rows, [3.3, 8.833], size=14, row_h=0.42, align=[PP_ALIGN.LEFT, PP_ALIGN.LEFT])
    footer(s, "Repeatability: 54 % of points within ±5 %, 90th percentile 20 % (mostly ~1 ms runs): "
              "the noise floor of a laptop.")
    notes(s, "One engine process per data set and storage configuration; every query and execution "
             "option inside it. Because times are stored per query and option, any workload mix "
             "can be evaluated afterwards without re-running anything.")


def s_bytes():
    s = new_slide()
    header(s, "Finding 1", "Bytes moved explain scans: NSM reads the whole row")
    sc = D["scan"]
    cats = ["1 column", "4 columns", "16 columns"]
    keys = ["p1", "p25", "p100"]
    for i, (idx, title, fmt) in enumerate(((0, "Time per row (ns)", "0.0"),
                                           (1, "DRAM bytes read per row", "0"))):
        cd = CategoryChartData()
        cd.categories = cats
        for name, key in (("DSM", "dsm"), ("NSM", "nsm"), ("PAX 16 KB", "pax:page=16K")):
            cd.add_series(name, [sc[key][k][idx] for k in keys])
        x = ML + i * Inches(4.25)
        gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, Inches(1.75), Inches(4.0),
                                Inches(4.5), cd)
        ch = gf.chart
        style_chart(ch, legend=(i == 0))
        ch.has_title = True
        ch.chart_title.text_frame.text = title
        ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        ch.chart_title.text_frame.paragraphs[0].runs[0].font.bold = True
        quiet_axes(ch)
        color_series(ch, [DSM, NSM, PAX])
        bar_gap(ch, 70, -5)
        labels(ch, fmt, size=10)
    x = Inches(9.3)
    stat(s, x, Inches(1.9), Inches(3.4), "13×",
         "slower: NSM vs DSM for SUM of one column (6.4 vs 0.49 ns/row)", color=NSM)
    stat(s, x, Inches(3.6), Inches(3.4), "~17 GB/s",
         "one core’s DRAM bandwidth: time ≈ bytes ÷ bandwidth", color=INK)
    text(s, x, Inches(5.3), Inches(3.4), Inches(1.2),
         "NSM reads ~140 B per 128 B row for one 8 B column: each line arrives with its "
         "neighbour (an effective 128 B transfer unit).", size=14)
    footer(s, "16-column table, 256 MB, unfiltered SUM, cold. All columns: the layouts converge "
              "(7.6 / 7.8 / 8.2 ns/row).")
    notes(s, "When data comes from DRAM, the time is the bytes moved divided by about 17 GB/s. "
             "NSM cannot avoid reading whole rows; DSM reads exactly the columns it needs.")


def s_branches():
    s = new_slide()
    header(s, "Finding 2", "Branch misses make 50 % selectivity the slowest")
    b = D["branch"]
    cats = [f"{r * 100:g}%" for r in b["rates"]]
    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series("Random data (measured)", b["uniform"])
    cd.add_series("Model 2·f·(1−f)", b["model"])
    cd.add_series("Sorted data (measured)", b["sorted"])
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, ML, Inches(1.75), Inches(6.0), Inches(4.6), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Branch misses per row, by selectivity"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, cat_title="pass rate of the predicate")
    color_series(ch, [INK, GOLD, PAX], line=True, dashes=[False, True, False],
                 markers=[XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.NONE, XL_MARKER_STYLE.CIRCLE])
    ch.value_axis.tick_labels.number_format = "0.0"
    ch.value_axis.tick_labels.number_format_is_linked = False
    bf = D["branchfree"]
    cd = CategoryChartData()
    cd.categories = cats
    for name, key in (("DSM", "dsm"), ("PAX", "pax"), ("NSM", "nsm")):
        cd.add_series(name, bf[key])
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(6.85), Inches(1.75), Inches(5.9),
                            Inches(4.6), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Branch-free speed-up (branchy ÷ branch-free time)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, cat_title="pass rate of the predicate")
    color_series(ch, [DSM, PAX, NSM], line=True)
    ch.value_axis.tick_labels.number_format = "0.0×"
    ch.value_axis.tick_labels.number_format_is_linked = False
    footer(s, f"Branch-free: up to {max(bf['dsm']):.2f}× faster for DSM at 50 %, but "
              f"{1 / min(bf['dsm']):.2f}× slower at 0.1 % — an execution choice, not a default. "
              "Calibrated miss cost: 26 cycles.")
    notes(s, "On random data each predicate evaluation mispredicts with probability 2f(1-f), which "
             "peaks at 50%. On sorted data the branch is predictable and misses vanish. "
             "Branch-free filtering removes the misses but always pays for writing the candidate, "
             "so it loses when almost nothing qualifies.")


def s_pagesize():
    s = new_slide()
    header(s, "Finding 3", "Small PAX pages invite the prefetcher")
    p = D["pagesize"]
    cd = CategoryChartData()
    cd.categories = p["pages"]
    cd.add_series("PAX", p["dram"])
    cd.add_series("DSM", [p["dsm_dram"]] * len(p["pages"]))
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, ML, Inches(1.75), Inches(6.0), Inches(4.6), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "DRAM bytes per row, SUM(c0)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, cat_title="PAX page size")
    color_series(ch, [PAX, DSM], line=True, dashes=[False, True],
                 markers=[XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.NONE])
    cd = CategoryChartData()
    cd.categories = p["pages"]
    cd.add_series("PAX", p["ns"])
    cd.add_series("DSM", [p["dsm_ns"]] * len(p["pages"]))
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(6.85), Inches(1.75), Inches(5.9),
                            Inches(4.6), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Time per row (ns), SUM(c0)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, cat_title="PAX page size")
    color_series(ch, [PAX, DSM], line=True, dashes=[False, True],
                 markers=[XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.NONE])
    ch.value_axis.tick_labels.number_format = "0.0"
    ch.value_axis.tick_labels.number_format_is_linked = False
    footer(s, f"16 columns, 256 MB. 1 KB pages read {p['dram'][0]:.0f} B/row for 8 useful bytes; "
              f"≥ 256 KB ≈ DSM. The common 16 KB default is {p['ns'][4] / p['dsm_ns']:.1f}× slower "
              "than DSM here.")
    notes(s, "At 1 KB pages a minipage is one 64-byte line, so a scan of one column is a sequence "
             "of tiny runs; the hardware prefetchers fetch the following lines, which belong to "
             "other columns. Bigger pages mean longer runs and less over-fetch.")


def s_lookups():
    s = new_slide()
    header(s, "Finding 4", "Point lookups are NSM’s home ground")
    lk = D["lookups"]
    for i, (ds, title) in enumerate((("w16_uniform", "16-column table"),
                                     ("w64_uniform", "64-column table"))):
        ncol = 16 if i == 0 else 64
        cd = CategoryChartData()
        cd.categories = ["1 column", f"{ncol // 4} columns", f"all {ncol}"]
        for name, key in (("NSM", "nsm"), ("PAX", "pax"), ("DSM", "dsm")):
            cd.add_series(name, lk[ds][key])
        x = ML + i * Inches(4.25)
        gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, Inches(1.75), Inches(4.0),
                                Inches(4.5), cd)
        ch = gf.chart
        style_chart(ch, legend=(i == 0))
        ch.has_title = True
        ch.chart_title.text_frame.text = f"ns per lookup, {title}"
        ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        quiet_axes(ch)
        color_series(ch, [NSM, PAX, DSM])
        bar_gap(ch, 70, -5)
        labels(ch, "0", size=10)
    x = Inches(9.3)
    w16 = lk["w16_uniform"]
    stat(s, x, Inches(1.9), Inches(3.4), f"{w16['dsm'][2] / w16['nsm'][2]:.1f}×",
         f"faster: NSM vs DSM for a full 16-column tuple ({w16['nsm'][2]:.0f} vs {w16['dsm'][2]:.0f} ns)",
         color=NSM)
    text(s, x, Inches(3.55), Inches(3.4), Inches(2.8),
         ["DSM pays one cache miss and one TLB miss per column; NSM pays about two lines.",
          "For one column, DSM and PAX match or beat NSM: fewer bytes per probe."], size=14,
         spacing=8)
    footer(s, "10,000 independent lookups, uniform row ids, cold. Sorted ids make every layout "
              "cheaper (e.g. DSM, one column of 64: 9.1 → 3.4 ns).")
    notes(s, "Lookups are independent, so the CPU overlaps their misses: about 15 in flight, "
             "calibrated. That is why 64 columns of DSM cost under 1 microsecond, not 64 × 109 ns.")


def s_winners():
    s = new_slide()
    header(s, "Finding 5", "DSM wins most queries, but many are close calls")
    wn = D["winners"]
    order = [("w16_uniform", "16 cols uniform"), ("w64_uniform", "64 cols uniform"),
             ("w16_c0sorted", "16 cols, c0 sorted")]
    cd = CategoryChartData()
    cd.categories = [n for _, n in order]
    for name, key in (("DSM", "dsm"), ("PAX", "pax"), ("NSM", "nsm")):
        cd.add_series(name, [wn[ds][key] for ds, _ in order])
    gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_STACKED, ML, Inches(1.75), Inches(7.6), Inches(4.4), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Winning layout per query (each layout with its best options)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, val_title="number of queries")
    color_series(ch, [DSM, PAX, NSM])
    bar_gap(ch, 55, 100)
    labels(ch, "0", size=11, pos=XL_LABEL_POSITION.CENTER)
    ch.plots[0].data_labels.font.color.rgb = WHITE
    ch.plots[0].data_labels.font.bold = True
    x = Inches(8.7)
    stat(s, x, Inches(1.9), Inches(4.0), f"{D['close_5'] * 100:.0f} %",
         "of queries have a runner-up within 5 %", color=DSM, big_size=54)
    stat(s, x, Inches(3.6), Inches(4.0), f"{D['close_10'] * 100:.0f} %",
         "within 10 %", color=INK, big_size=54)
    text(s, x, Inches(5.25), Inches(4.0), Inches(1.0),
         "NSM wins full-tuple lookups; PAX (large pages) wins some sums on sorted data.", size=14)
    footer(s)
    notes(s, "With each layout given its best execution options, DSM wins most filters and "
             "reconstructions on this machine. The margins are often small, which is why an "
             "advisor must report its margin, not only its pick.")


def s_exec():
    s = new_slide()
    header(s, "Finding 6", "Execution options matter as much as the layout")
    er, orr = D["exec_ratio"], D["order_ratio"]
    cd = CategoryChartData()
    cd.categories = ["DSM", "PAX", "NSM"]
    cd.add_series("median", [er[k]["median"] for k in ("dsm", "pax", "nsm")])
    cd.add_series("90th percentile", [er[k]["p90"] for k in ("dsm", "pax", "nsm")])
    cd.add_series("worst", [er[k]["max"] for k in ("dsm", "pax", "nsm")])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ML, Inches(1.75), Inches(7.2), Inches(4.5), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Worst ÷ best time over execution options, same query and layout"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch)
    color_series(ch, [RGBColor(0x9A, 0xA3, 0xB2), RGBColor(0x5A, 0x65, 0x78), INK])
    bar_gap(ch, 60, -5)
    labels(ch, '0.0"×"', size=11)
    x = Inches(8.3)
    stat(s, x, Inches(1.9), Inches(4.4), f"{orr['pax']['max']:.1f}×",
         f"worst case of evaluating the selective predicate last (PAX; median "
         f"{orr['pax']['median']:.1f}×)", color=PAX)
    text(s, x, Inches(3.65), Inches(4.4), Inches(2.6),
         ["Options: DSM batch size, branchy vs branch-free, predicate order.",
          "Every DSM batch size (256 … 128K rows) is the best for some query.",
          "So the advisor picks options per query, on top of the layout per table."], size=15,
         spacing=8)
    footer(s, "Queries with at least one predicate; all three data sets.")
    notes(s, "For the same query on the same layout, a bad choice of options costs a median of "
             "1.4x for DSM and up to 7.9x.")


def s_advisor():
    s = new_slide()
    header(s, "The layout advisor", "From inputs to a recommendation in under a second")
    y = Inches(2.0)
    ins = ["hw.json\ncalibrated machine", "data.yaml\nrows, columns, distributions",
           "workload.yaml\nweighted queries", "space.yaml\ncandidates"]
    for i, t in enumerate(ins):
        a, b = t.split("\n")
        boxed_text(s, ML, y + i * Inches(1.05), Inches(3.0), Inches(0.85),
                   [[(a, {"bold": True, "size": 16, "color": INK, "font": TEXT})],
                    [(b, {"size": 13, "color": MUTED})]], fill=CARD, align=PP_ALIGN.LEFT, pad=0.18)
    stages = [("Selectivity\nestimator", "profile · histogram · fixed"),
              ("Exhaustive\nsearch", "12 storages × options per query"),
              ("Cost model v3", "patterns per cache level")]
    for i, (t, d) in enumerate(stages):
        x = Inches(4.15) + i * Inches(2.65)
        boxed_text(s, x, Inches(3.0), Inches(2.3), Inches(1.6),
                   [[(t.replace("\n", " "), {"bold": True, "size": 16, "color": WHITE})],
                    [(d, {"size": 13, "color": DARK_TEXT})]], fill=INK)
        if i < 2:
            arrow(s, x + Inches(2.33), Inches(3.8), x + Inches(2.62), Inches(3.8))
    for i in range(4):
        arrow(s, ML + Inches(3.05), y + i * Inches(1.05) + Inches(0.42), Inches(4.1), Inches(3.8))
    arrow(s, Inches(11.47), Inches(3.8), Inches(11.75), Inches(3.8))
    boxed_text(s, Inches(11.8), Inches(2.4), Inches(0.93), Inches(2.8), "Report", fill=CARD,
               size=15, bold=True, color=INK)
    bullets(s, Inches(4.15), Inches(5.0), Inches(7.6), Inches(1.6),
            ["**Output:** storage once per table; execution options per query; predicted time, "
             "runner-up and margin, where the time goes, sensitivity.",
             "**Pluggable:** cost models, search strategies, estimators, layouts: one class each."],
            size=15, gap=6)
    footer(s, "python3 -m advisor recommend | predict | maps | validate | evaluate | calibrate")
    notes(s, "Exhaustive search is enough: a few hundred predictions per workload, each well under "
             "a millisecond.")


def s_model():
    s = new_slide()
    header(s, "Cost model", "Manegold’s 2002 model, extended for modern CPUs")
    text(s, ML, Inches(1.75), Inches(5.6), Inches(1.0),
         "Each layout × operator is described as memory access patterns; each pattern gives "
         "misses per cache and TLB level.", size=16)
    boxed_text(s, ML, Inches(2.75), Inches(5.6), Inches(0.95),
               [[("T = Σ", {"font": HEAD, "size": 18, "color": INK}),
                 ("levels", {"font": HEAD, "size": 12, "color": INK}),
                 (" (seq. misses · lˢ + random misses · lʳ) + T", {"font": HEAD, "size": 18, "color": INK}),
                 ("cpu", {"font": HEAD, "size": 12, "color": INK})]], fill=CARD)
    pats = [("s_tra", "sequential pass (scans)"), ("ss_tra", "sparse pass after a selection (new)"),
            ("r_acc", "random accesses (lookups)"), ("rs_tra", "re-used buffers")]
    for i, (p, d) in enumerate(pats):
        yy = Inches(3.85) + i * Inches(0.6)
        boxed_text(s, ML, yy, Inches(1.25), Inches(0.45), p, fill=INK, color=WHITE, size=14,
                   bold=True, font=MONO)
        text(s, ML + Inches(1.45), yy + Inches(0.08), Inches(4.2), Inches(0.4), d, size=15)
    exts = [("B_eff = 145 B", "effective DRAM transfer unit (line + neighbour)"),
            ("Over-fetch", "prefetchers run past short runs (PAX)"),
            ("Overlapping misses", "MLP 15: random misses cost l / 15"),
            ("Hidden page walks", "sequential streams prefetch the next page"),
            ("Branch misses", "2·f·(1−f) per branchy predicate × 26 cycles"),
            ("Overheads + overlap", "per batch/page; T = max + γ·min")]
    x0 = Inches(6.65)
    cw = Inches(2.95)
    for i, (t, d) in enumerate(exts):
        x = x0 + (i % 2) * (cw + Inches(0.2))
        yy = Inches(1.75) + (i // 2) * Inches(1.55)
        box(s, x, yy, cw, Inches(1.35), CARD)
        text(s, x + Inches(0.2), yy + Inches(0.15), cw - Inches(0.4), Inches(0.4), t, size=16,
             bold=True, color=INK)
        text(s, x + Inches(0.2), yy + Inches(0.55), cw - Inches(0.4), Inches(0.75), d, size=13)
    footer(s, "Model ladder: v0 bytes ÷ bandwidth → v1 Manegold as published → v2 + modern memory → "
              "v3 + modern CPU (6 fitted scale factors).")
    notes(s, "Each extension is motivated by a measurement from the findings: the 128-byte "
             "transfer unit, PAX over-fetch, branch misses. Adding a layout means describing its "
             "access patterns, not deriving new formulas.")


def s_calibration():
    s = new_slide()
    header(s, "Calibration", "The machine in numbers, measured in 20 seconds")
    lat = D["cal_latency"]
    xy = XyChartData()
    ser = xy.add_series("dependent-load latency")
    for b, ns in lat:
        ser.add_data_point(b, ns)
    gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES, ML, Inches(1.75), Inches(6.6), Inches(4.7), xy)
    ch = gf.chart
    style_chart(ch, legend=False)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Latency vs region size (log scale): one step per cache level"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, val_title="ns per load", cat_title="region size")
    log_axis(ch.category_axis, 10, 1000, 1e9)
    log_axis(ch.value_axis, 10, 1, 200)
    ch.category_axis.tick_labels.number_format = '[>=1000000]0,," MB";[>=1000]0," kB";0" B"'
    ch.category_axis.tick_labels.number_format_is_linked = False
    ch.category_axis.has_major_gridlines = False
    color_series(ch, [INK], line=True, width=2)
    lv = {x["name"]: x for x in D["hw"]["levels"]}
    rows = [["Level", "Size", "Bandwidth", "Latency"],
            ["L1", "48 KB", f"{lv['L1']['seq_gb_s']:.0f} GB/s", f"{lv['L1']['lat_ns']:.1f} ns"],
            ["L2", "1.25 MB", f"{lv['L2']['seq_gb_s']:.0f} GB/s", f"{lv['L2']['lat_ns']:.1f} ns"],
            ["L3", "18 MB", f"{lv['L3']['seq_gb_s']:.0f} GB/s", f"{lv['L3']['lat_ns']:.0f} ns"],
            [("DRAM", True), "—", (f"{lv['DRAM']['seq_gb_s']:.1f} GB/s", True),
             (f"{lv['DRAM']['lat_ns']:.0f} ns", True)]]
    table(s, Inches(7.55), Inches(1.85), Inches(5.18), rows, [1.1, 1.2, 1.5, 1.38], size=14, row_h=0.42)
    hw = D["hw"]
    facts = [f"Effective transfer unit {hw['dram']['B_eff']:.0f} B",
             f"Overlapping random misses (MLP) {hw['dram']['mlp']:.0f}",
             f"Page walk ≈ {hw['tlb']['walk_ns']:.0f} ns · branch miss {hw['branch']:.0f} cycles",
             f"Clock {hw['freq']:.2f} GHz"]
    bullets(s, Inches(7.55), Inches(4.15), Inches(5.2), Inches(2.3), facts, size=15, gap=6)
    footer(s, "Nothing is hard-coded: the same calibration runs on any machine. Bandwidth and "
              "branch cost match the scan and filter measurements.")
    notes(s, "Calibration uses only micro-benchmarks, never the validation queries, so validation "
             "stays honest. Huge pages are used for the latency curve so TLB misses do not blur "
             "the cache steps.")


def s_accuracy():
    s = new_slide()
    header(s, "Accuracy (V1)", "Each modern mechanism removes much of the error")
    lad = {r["model"]: r for r in D["ladder"]}
    cd = CategoryChartData()
    cd.categories = ["v0 bytes ÷ BW", "v1 Manegold 2002", "v2 + memory", "v3 + CPU"]
    cd.add_series("median |error|", [lad[m]["median_abs_err"] for m in ("v0", "v1", "v2", "v3")])
    cd.add_series("within ±15 %", [lad[m]["within_15%"] for m in ("v0", "v1", "v2", "v3")])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ML, Inches(1.75), Inches(6.2), Inches(4.4), cd)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Held-out points (fit on 30 %, test on 70 %)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch)
    ch.value_axis.tick_labels.number_format = "0%"
    ch.value_axis.tick_labels.number_format_is_linked = False
    ch.value_axis.maximum_scale = 1.0
    color_series(ch, [DSM, PAX])
    bar_gap(ch, 60, -5)
    labels(ch, "0%", size=11)
    # scatter, predicted vs measured, log-log
    xy = XyChartData()
    allv = []
    for lay, name in (("pax", "PAX"), ("dsm", "DSM"), ("nsm", "NSM")):
        ser = xy.add_series(name)
        for m, p in D["scatter"][lay]:
            ser.add_data_point(m, p)
            allv += [m, p]
    ser = xy.add_series("exact")
    ser.add_data_point(0.2, 0.2)
    ser.add_data_point(5000, 5000)
    gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(7.05), Inches(1.75), Inches(5.7),
                            Inches(4.4), xy)
    ch = gf.chart
    style_chart(ch)
    ch.has_title = True
    ch.chart_title.text_frame.text = "v3: predicted vs measured ns per item (log–log)"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, val_title="predicted", cat_title="measured")
    for ax in (ch.category_axis, ch.value_axis):
        log_axis(ax, 10, 0.2, 5000)
        ax.tick_labels.number_format = "#,##0.#"
        ax.tick_labels.number_format_is_linked = False
    ch.category_axis.has_major_gridlines = False
    for sr, c in zip(list(ch.plots[0].series)[:3], (PAX, DSM, NSM)):
        sr.marker.style = XL_MARKER_STYLE.CIRCLE
        sr.marker.size = 4
        sr.marker.format.fill.solid()
        sr.marker.format.fill.fore_color.rgb = c
        sr.marker.format.line.fill.background()
        sr.format.line.fill.background()
    ex = ch.plots[0].series[3]
    ex.marker.style = XL_MARKER_STYLE.NONE
    ex.format.line.color.rgb = INK
    ex.format.line.width = Pt(1.25)
    ex.format.line.dash_style = MSO_LINE.DASH
    e = D["err_by_dataset"]
    footer(s, f"v3 median error {lad['v3']['median_abs_err'] * 100:.0f} % (16 cols {e['w16_uniform'] * 100:.0f} %, "
              f"sorted {e['w16_c0sorted'] * 100:.0f} %, 64 cols {e['w64_uniform'] * 100:.0f} %); "
              f"90th pct {lad['v3']['p90_abs_err'] * 100:.0f} %. v1’s 90th pct is "
              f"{lad['v1']['p90_abs_err'] * 100:.0f} %.")
    notes(s, "The 2002 model applied as published is worse than plain bytes over bandwidth: it "
             "knows nothing of prefetching, overlapping misses or hidden page walks. Counters "
             "revealed the last one: sequential scans did 10 to 35 times fewer page walks than "
             "pages crossed, while random lookups matched the model exactly. Honest status: the "
             "median is on target (16% vs 15%), the tail is not (90th percentile 47%).")


def s_decisions():
    s = new_slide()
    header(s, "Decision quality (V2, V3)", "The advisor never loses badly; fixed rules do")
    dec = {r["chooser"]: r for r in D["decisions"]}
    rows = [["Chooser", "Top-1", "Median regret", "Worst regret"]]
    names = [("advisor", "Advisor (storage + options)"), ("always-dsm", "Always DSM"),
             ("dsm-unless-lookups>25%", "DSM unless lookups > 25 %"),
             ("always-pax:page=16K", "Always PAX 16 KB"), ("always-nsm", "Always NSM")]
    for k, n in names:
        r = dec[k]
        b = k == "advisor"
        rows.append([(n, b), (f"{r['top1_accuracy'] * 100:.0f} %", b), (f"{r['median_regret']:.2f}×", b),
                     (f"{r['max_regret']:.2f}×", b)])
    table(s, ML, Inches(1.85), Inches(6.3), rows, [2.85, 0.95, 1.25, 1.25], size=14, row_h=0.46)
    text(s, ML, Inches(4.8), Inches(6.3), Inches(1.6),
         ["Regret = measured time of the choice ÷ best measured time, over 500 random workload "
          "mixes (2–6 queries, frequency weights 1–1000).",
          "Baselines get the best measured execution options; the advisor is charged for its own."],
         size=13, color=MUTED, spacing=6)
    xy = XyChartData()
    q = D["cdf_q"]
    series = [("advisor", "Advisor", ADV), ("always-dsm", "Always DSM", DSM),
              ("dsm-unless-lookups>25%", "DSM unless lookups", GOLD),
              ("always-pax:page=16K", "Always PAX 16K", PAX), ("always-nsm", "Always NSM", NSM)]
    for k, n, _ in series:
        ser = xy.add_series(n)
        for qq, r in zip(q, D["regret_cdf"][k]):
            ser.add_data_point(max(r, 1.0), qq)
    gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES_NO_MARKERS, Inches(7.15), Inches(1.75),
                            Inches(5.6), Inches(4.7), xy)
    ch = gf.chart
    style_chart(ch, legend_pos=XL_LEGEND_POSITION.BOTTOM, size=11)
    ch.has_title = True
    ch.chart_title.text_frame.text = "Share of workloads with regret ≤ x"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
    quiet_axes(ch, cat_title="regret (log scale)", size=11)
    log_axis(ch.category_axis, 10, 1, 20)
    ch.category_axis.tick_labels.number_format = '0.#"×"'
    ch.category_axis.tick_labels.number_format_is_linked = False
    ch.value_axis.tick_labels.number_format = "0%"
    ch.value_axis.tick_labels.number_format_is_linked = False
    ch.value_axis.maximum_scale = 1.0
    ch.category_axis.has_major_gridlines = False
    for sr, (_, _, c) in zip(ch.plots[0].series, series):
        sr.format.line.color.rgb = c
        sr.format.line.width = Pt(2.5 if c == ADV else 1.75)
        sr.smooth = False
    footer(s, "Wrong picks are mostly DSM vs PAX with 1 MB pages: two layouts that read nearly the "
              "same bytes. Storage choice alone: median regret 1.00, worst 1.49×.")
    notes(s, "On this machine DSM is a strong default and wins the median with oracle options. "
             "But every fixed rule has a catastrophic tail of 6.5 to 12 times; the advisor's worst "
             "workload is 1.54 times. Most of its 7% median regret comes from execution-option "
             "choices, not the storage choice.")


def s_maps():
    s = new_slide()
    header(s, "Decision maps (V5)", "Right regions, fuzzy boundaries")
    maps = D["maps"]
    picks = [("w64_uniform|sum", "SUM, 64 columns"), ("w16_uniform|materialize", "SELECT, 16 columns")]
    projs = [("p1", "1 col"), ("p25", "25 %"), ("s25", "25 % spread"), ("p100", "all")]
    cw, chh, gap = Inches(0.46), Inches(0.46), Inches(0.03)
    for mi, (key, title) in enumerate(picks):
        m = maps[key]
        rates = sorted({c["rate"] for c in m["cells"]})
        x0 = ML + Inches(1.0) + mi * Inches(5.95)
        y0 = Inches(2.45)
        text(s, x0 - Inches(1.0), Inches(1.65), Inches(5.9), Inches(0.35), title, size=16, bold=True,
             color=INK)
        text(s, x0 - Inches(1.0), Inches(1.98), Inches(5.9), Inches(0.3),
             f"predicted = measured winner in {m['agree'] * 100:.0f} % of cells", size=13, color=MUTED)
        cells = {(c["proj"], c["rate"]): c for c in m["cells"]}
        for i, (pk, pn) in enumerate(projs):
            y = y0 + i * (chh + gap)
            text(s, x0 - Inches(1.0), y + Inches(0.12), Inches(0.9), Inches(0.3), pn, size=11,
                 color=MUTED, align=PP_ALIGN.RIGHT)
            for j, r in enumerate(rates):
                c = cells.get((pk, r))
                if not c:
                    continue
                x = x0 + j * (cw + gap)
                lay = c["pred"].split(":")[0]
                shp = boxed_text(s, x, y, cw, chh, short(c["pred"]).replace("NSM", "N").replace("DSM", "D"),
                                 fill=TINT[lay], size=10, color=INK, bold=True, pad=0.01)
                shp.adjustments[0] = 0.12
                if not c["agree"]:
                    shp.line.color.rgb = INK
                    shp.line.width = Pt(2.25)
                    if c["near"]:
                        shp.line.dash_style = MSO_LINE.DASH
        for j, r in enumerate(rates):
            text(s, x0 + j * (cw + gap), y0 + 4 * (chh + gap) + Inches(0.05),
                 cw, Inches(0.3), f"{r * 100:g}", size=10, color=MUTED, align=PP_ALIGN.CENTER)
        text(s, x0, y0 + 4 * (chh + gap) + Inches(0.35), Inches(4.9), Inches(0.3),
             "selectivity (%)", size=11, color=MUTED, align=PP_ALIGN.CENTER)
    # legend
    ly = Inches(5.45)
    items = [("nsm", "N = NSM predicted"), ("dsm", "D = DSM predicted"), ("pax", "1M, 4M … = PAX page")]
    for i, (lay, t) in enumerate(items):
        x = ML + i * Inches(2.9)
        box(s, x, ly, Inches(0.32), Inches(0.32), TINT[lay])
        text(s, x + Inches(0.42), ly + Inches(0.03), Inches(2.4), Inches(0.3), t, size=12)
    x = ML + Inches(8.7)
    b1 = box(s, x, ly, Inches(0.32), Inches(0.32), WHITE, line=INK)
    b1.line.width = Pt(2.25)
    text(s, x + Inches(0.42), ly + Inches(0.03), Inches(3.4), Inches(0.3), "disagrees with measurement",
         size=12)
    footer(s, "30 of 38 SUM disagreements: measured winner leads by < 10 % (dashed). Worst cell "
              "regret 1.14–1.55×. SELECT maps agree in 32–65 % of cells.")
    notes(s, "The maps show the model finds the regions: DSM for narrow scans, NSM or large-page "
             "PAX for wide ones. It misses exact boundaries where two layouts are within a few "
             "percent, which costs little.")


def s_robust():
    s = new_slide()
    header(s, "Generalisation and robustness (V4, V7)", "Stable under unseen data and wrong inputs")
    box(s, ML, Inches(1.8), Inches(5.6), Inches(4.6), CARD)
    text(s, ML + Inches(0.3), Inches(2.0), Inches(5.0), Inches(0.5), "V4  Unseen width and data",
         size=18, bold=True, color=INK)
    text(s, ML + Inches(0.3), Inches(2.6), Inches(5.0), Inches(1.1),
         "Fit only on 16-column uniform tables without lookups; test on 64 columns and sorted data.",
         size=15)
    stat(s, ML + Inches(0.3), Inches(3.75), Inches(2.4), "20 %", "median error,\n64 columns", color=NSM)
    stat(s, ML + Inches(2.9), Inches(3.75), Inches(2.4), "16 %", "median error,\nsorted c0", color=PAX)
    text(s, ML + Inches(0.3), Inches(5.55), Inches(5.0), Inches(0.7),
         "About the same as with a random split.", size=14, color=MUTED)
    rob = D["robustness"]
    lab = {"dram_bandwidth x0.8": "DRAM bandwidth −20 %", "cpu x1.2": "CPU constants +20 %",
           "selectivity x0.5": "Selectivity ×0.5", "selectivity x2": "Selectivity ×2"}
    rows = [["V7  Perturbation (no refit)", "Decisions changed", "Median regret"]]
    for r in rob:
        rows.append([lab.get(r["perturbation"], r["perturbation"]),
                     f"{r['decisions_changed'] * 100:.0f} %", f"{r['median_regret']:.2f}×"])
    table(s, Inches(6.65), Inches(1.8), Inches(6.08), rows, [3.0, 1.65, 1.43], size=14, row_h=0.5)
    text(s, Inches(6.65), Inches(4.6), Inches(6.08), Inches(1.6),
         "Wrong bandwidth barely matters; selectivity estimates matter most, yet the median regret "
         "stays within 1.07–1.10×.", size=15)
    footer(s)
    notes(s, "V4 fits the model's six scale factors on one data set only. V7 perturbs the inputs "
             "without refitting and re-runs all 500 workload decisions.")


def s_demo():
    s = new_slide()
    header(s, "Demo", "python3 -m advisor recommend …")
    import re
    keep = []
    for ln in D["recommend"]["wide_mixed"].splitlines()[4:]:
        ln = re.sub(r"\[.*?\]\s*", "", ln).split("  -- ")[0]
        ln = ln.replace(" x{0.25, 0.5, 0.8, 1.25, 2, 4}", "").replace("pax: ", "").replace("dsm: ", "")
        ln = ln.replace("nsm: ", "")
        ln = re.sub(r"(\S)\s{3,}(?=\S)", r"\1  ", ln) if ln.startswith("  ") else ln
        if ln.strip() or (keep and keep[-1]):
            keep.append(ln.rstrip())
    term = box(s, ML, Inches(1.75), Inches(7.5), Inches(3.75), INK)
    text(s, ML + Inches(0.25), Inches(1.95), Inches(7.05), Inches(4.4),
         [[(ln or " ", {"color": DARK_TEXT})] for ln in keep], size=11, font=MONO, spacing=2)
    rows = [["Workload", "Pick", "Runner-up"],
            ["Lookup-heavy", ("NSM", True), "PAX 64K, 1.27×"],
            ["Scan-heavy", ("DSM", True), "PAX 1M, 1.04×"],
            ["Wide mixed", ("PAX 4M", True), "PAX 1M, 1.00×"]]
    table(s, Inches(8.3), Inches(1.85), Inches(4.43), rows, [1.55, 1.0, 1.88], size=13, row_h=0.48,
          align=[PP_ALIGN.LEFT] * 3)
    text(s, Inches(8.3), Inches(4.05), Inches(4.43), Inches(2.4),
         ["Close calls are flagged, never hidden.",
          "Each query gets its own execution options.",
          "The sensitivity line says how far the inputs can move before the pick changes."],
         size=14, spacing=8)
    footer(s, "8M rows × 64 columns. Wide mixed: 50 % SELECT * at 1 %, 30 % narrow sums, 20 % lookups. "
              "Output trimmed.")
    notes(s, "Live demo if time permits: run recommend on the three example workloads in "
             "advisor/workloads/examples.")


def s_lessons():
    s = new_slide()
    header(s, "Wrap-up", "Lessons, limitations and next steps")
    cols = [("Lessons", ["Bytes moved explain scans; prefetchers, branch prediction and "
                         "overlapping misses decide the close cases.",
                         "Execution options matter as much as the layout.",
                         "Many picks are within 5–10 %: report the margin."], INK),
            ("Limitations", ["One machine so far (laptop)", "Single thread, read-only",
                             "Predicates assumed independent",
                             "Tail error 47 % (90th pct); SELECT-all scans over-predicted",
                             "Laptop noise: ~4 % median, up to 20 %"], DSM),
            ("Next steps", ["Second machine: AMD EPYC VM (recalibrate only)",
                            "Multithreading: bandwidth per thread",
                            "Column groups, zone maps, updates",
                            "Robust pick: minimise the worst case"], PAX)]
    cw = (CW - 2 * Inches(0.35)) / 3
    for i, (t, items, c) in enumerate(cols):
        x = ML + i * (cw + Inches(0.35))
        box(s, x, Inches(1.8), cw, Inches(4.8), CARD)
        box(s, x + Inches(0.25), Inches(2.05), Inches(0.28), Inches(0.28), c, shape=MSO_SHAPE.OVAL)
        text(s, x + Inches(0.65), Inches(2.0), cw - Inches(0.9), Inches(0.45), t, size=20, bold=True,
             font=HEAD, color=INK)
        bullets(s, x + Inches(0.25), Inches(2.7), cw - Inches(0.5), Inches(3.8), items, size=15, gap=8)
    footer(s)
    notes(s, "The model is good enough to avoid bad choices, not yet good enough to always pick "
             "the exact winner in close cases.")


def s_conclusion():
    s = new_slide(dark=True)
    text(s, ML, Inches(0.9), CW, Inches(0.4), "CONCLUSION", size=14, bold=True, color=GOLD)
    text(s, ML, Inches(1.35), Inches(11.5), Inches(0.9), "What we take away", size=36, bold=True,
         font=HEAD, color=WHITE)
    pts = [("DSM", DSM, "wins most scans and filters; NSM owns full-tuple lookups; PAX needs ≥ 256 KB pages."),
           ("Why", GOLD, "bytes moved, plus L1 sets, prefetcher page limits, TLB reach and "
                         "where the core stalls."),
           ("Advisor", ADV, "16 % median error; on 500 workloads never worse than 1.54× the best, "
                            "while every fixed rule loses up to 6.5–12×.")]
    for i, (t, c, d) in enumerate(pts):
        y = Inches(2.65) + i * Inches(1.15)
        box(s, ML, y + Inches(0.08), Inches(0.3), Inches(0.3), c, shape=MSO_SHAPE.OVAL)
        text(s, ML + Inches(0.5), y, Inches(1.7), Inches(0.5), t, size=22, bold=True, color=WHITE)
        text(s, ML + Inches(2.2), y + Inches(0.03), Inches(10.0), Inches(1.0), d, size=20, color=DARK_TEXT)
    text(s, ML, Inches(6.3), CW, Inches(0.4), "Thank you — questions?", size=20, bold=True, color=GOLD)
    footer(s, dark=True)
    notes(s, "Three takeaways and the headline number. Open the backup slides for details.")


def s_backup_divider():
    s = new_slide(dark=True)
    text(s, ML, Inches(3.0), CW, Inches(0.5), "BACKUP", size=14, bold=True, color=GOLD)
    text(s, ML, Inches(3.45), CW, Inches(0.9), "Details for questions", size=36, bold=True, font=HEAD,
         color=WHITE)
    footer(s, dark=True)


def s_backup_pax_wins():
    s = new_slide()
    header(s, "Backup", "Where PAX genuinely wins: wide SELECT * at ~1 %")
    cd = CategoryChartData()
    cd.categories = ["PAX 4 KB", "DSM", "NSM"]
    cd.add_series("ns per row", [6.86, 9.32, 10.00])
    gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, ML, Inches(1.8), Inches(6.5), Inches(3.6), cd)
    ch = gf.chart
    style_chart(ch, legend=False)
    ch.has_title = False
    quiet_axes(ch, val_title="ns per row")
    pts = ch.plots[0].series[0].points
    for i, c in enumerate((PAX, DSM, NSM)):
        pts[i].format.fill.solid()
        pts[i].format.fill.fore_color.rgb = c
    bar_gap(ch, 50)
    labels(ch, "0.00", size=12)
    ch.category_axis.reverse_order = True
    bullets(s, Inches(7.5), Inches(1.9), Inches(5.2), Inches(4.5),
            ["Earlier experiment: 64 columns, 1 GB, `SELECT * WHERE c0 < 1` (1–3 % pass).".replace("`", ""),
             "DRAM per row: PAX 106 B, DSM 168 B, NSM 157 B.",
             "PAX reads the qualifying rows’ pages once; DSM touches a line in all 64 columns.",
             "In the ground truth DSM gets its best batch size and branch-free filters, which "
             "closes most of this gap."], size=15, gap=8)
    footer(s, "Source: results/pax_wins (earlier warm-mode experiment, 1 GB table).")


def s_backup_patterns():
    s = new_slide()
    header(s, "Backup", "Access patterns per layout and operator")
    rows = [["Query", "NSM", "DSM", "PAX (T rows per page)"],
            ["Scan of columns Q", "s_tra(table, bytes of Q per row)", "⊕ s_tra(column c), c ∈ Q",
             "s_tra(pages, minipages of Q) + over-fetch"],
            ["Filter + sum", "s_tra(predicate) ⊙ ss_tra(rest of Q, f)",
             "per batch: s_tra(pred) ⊕ ss_tra(Q, f); nest over columns", "the same per page"],
            ["Reconstruction", "as filter + output buffer", "gathers concurrently (⊙)",
             "gathers within one page"],
            ["N point lookups", "r_acc(N, table, Q bytes)", "⊙ r_acc(N, column c)",
             "r_acc(N, pages, |Q| slots)"]]
    table(s, ML, Inches(1.9), CW, rows, [2.2, 3.4, 3.4, 3.133], size=13, row_h=0.62,
          align=[PP_ALIGN.LEFT] * 4)
    text(s, ML, Inches(5.3), CW, Inches(1.0),
         "⊕ = one after another (cache state carried); ⊙ = concurrent (cache shared by footprint); "
         "ss_tra = sparse ordered pass, line touched with probability 1 − (1 − f)^(values per line).",
         size=14, color=MUTED)
    footer(s, "advisor/models/patterns/describe.py: one function per layout; a new layout = one new function.")


def s_backup_repro():
    s = new_slide()
    header(s, "Backup", "Reproducing every number")
    cmds = ["make && make test",
            "python3 scripts/run_ground_truth.py            # ~100 min, idle machine",
            "python3 scripts/analyze_ground_truth.py results/ground_truth/laptop_v1",
            "FAST=1 scripts/run_validation.sh                # calibrate + V1–V7",
            "python3 -m advisor recommend --hw configs/hw/laptop.json \\",
            "    --data configs/data/w64_8m.yaml --workload advisor/workloads/examples/wide_mixed.yaml",
            "python3 scripts/deck_data.py && python3 scripts/build_presentation.py"]
    box(s, ML, Inches(1.85), CW, Inches(2.7), INK)
    text(s, ML + Inches(0.3), Inches(2.1), CW - Inches(0.6), Inches(2.3),
         [[(c, {"color": DARK_TEXT})] for c in cmds], size=14, font=MONO, spacing=6)
    text(s, ML, Inches(4.85), CW, Inches(0.8),
         "Results: results/ground_truth/laptop_v1 (10,368 points) · results/validation/laptop_v1 "
         "(reports, CSVs, plots) · configs/hw/laptop.json (calibration).", size=14)
    footer(s)


# ================================================================= hardware insights
def chart_title(ch, t, size=14):
    ch.has_title = True
    ch.chart_title.text_frame.text = t
    r = ch.chart_title.text_frame.paragraphs[0].runs[0]
    r.font.size = Pt(size)
    r.font.bold = True


def s_ins_divider():
    s = new_slide(dark=True)
    text(s, ML, Inches(1.0), CW, Inches(0.4), "UNDER THE HOOD", size=14, bold=True, color=GOLD)
    text(s, ML, Inches(1.45), CW, Inches(0.9), "What the hardware is really doing", size=36, bold=True,
         font=HEAD, color=WHITE)
    items = ["L1 associativity: DSM tuple rebuilds thrash beyond ~10 columns",
             "The prefetcher fills the rest of the 4-KB page, then stops",
             "Touch two adjacent lines per row and you pay for the whole row",
             "A mispredict costs 26 cycles, unless the core is waiting for DRAM anyway",
             "For lookups the TLB, not the cache, separates PAX from DSM"]
    for i, t in enumerate(items):
        y = Inches(2.75) + i * Inches(0.72)
        boxed_text(s, ML, y, Inches(0.5), Inches(0.5), str(i + 1), fill=GOLD, color=INK, size=18,
                   bold=True, pad=0.02)
        text(s, ML + Inches(0.75), y + Inches(0.08), Inches(11), Inches(0.45), t, size=20, color=DARK_TEXT)
    footer(s, "Each one found in the counters and confirmed by a targeted experiment.", dark=True)
    notes(s, "These are the non-obvious effects we found while explaining the measurements. Each "
             "comes with counter evidence and, where possible, a controlled experiment.")


def s_ins_assoc():
    s = new_slide()
    header(s, "Hardware insight 1", "DSM rebuilds thrash the 12-way L1 past ~10 columns")
    a = D["assoc"]
    xy = XyChartData()
    for name, key in (("columns at the same page offset (DSM)", "aligned"),
                      ("columns shifted by 64 B each", "staggered")):
        ser = xy.add_series(name)
        for k, v in zip(a[key]["k"], a[key]["l1"]):
            ser.add_data_point(k, v)
    ways = xy.add_series("12 L1 ways")
    ways.add_data_point(12, 0)
    ways.add_data_point(12, 25)
    gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES, ML, Inches(1.65), Inches(6.9), Inches(4.85), xy)
    ch = gf.chart
    style_chart(ch, legend_pos=XL_LEGEND_POSITION.BOTTOM, size=12)
    chart_title(ch, "L1 misses per row while rebuilding k-column tuples (in cache)")
    quiet_axes(ch, val_title="L1 misses per row", cat_title="columns rebuilt (k)")
    ch.category_axis.minimum_scale, ch.category_axis.maximum_scale = 0, 32
    ch.category_axis.major_unit = 4
    ch.value_axis.minimum_scale, ch.value_axis.maximum_scale = 0, 25
    ch.category_axis.has_major_gridlines = False
    color_series(ch, [DSM, PAX, MUTED], line=True, width=2.25, dashes=[False, False, True],
                 markers=[XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.NONE])
    for sr in list(ch.plots[0].series)[:2]:
        sr.marker.size = 5
    k32 = a["aligned"]["l1"][-1] / a["staggered"]["l1"][-1]
    t32 = a["aligned"]["ns"][-1] / a["staggered"]["ns"][-1]
    x = Inches(7.85)
    stat(s, x, Inches(1.7), Inches(4.9), f"{k32:.0f}× more misses",
         f"at 32 columns ({a['aligned']['l1'][-1]:.1f} vs {a['staggered']['l1'][-1]:.1f} per row), "
         f"{t32:.1f}× slower", color=DSM, big_size=34)
    bullets(s, x, Inches(3.25), Inches(4.88), Inches(3.4),
            ["L1 = 48 KB = **64 sets × 12 ways**; the set is chosen by address bits 6–11, i.e. "
             "the offset inside a 4-KB page.",
             "Every DSM column array starts at the same page offset, so value r of **every column "
             "lands in the same set**.",
             "More columns than ways: lines are evicted before their next 7 uses.",
             f"Engine at 32 columns: DSM {D['assoc_engine']['DSM']['l1'][-1]:.1f}, PAX (64 KB pages, "
             f"minipages 2 KB apart) {D['assoc_engine']['PAX']['l1'][-1]:.1f}, NSM "
             f"{D['assoc_engine']['NSM']['l1'][-1]:.1f} L1 misses/row.",
             "**Fix: offset column c by c × 64 B.**"], size=14, gap=5)
    footer(s, "Probe: k arrays of 64 KB read tuple-at-a-time, warm (tools/assoc_probe.cpp). Staggered = "
              "compulsory k/8 misses; thrashing starts before 12 as prefetches share the ways.")
    notes(s, "The same mechanism as the classic power-of-two stride problem. DSM allocates each "
             "column with the same alignment, so the i-th value of every column maps to one L1 set. "
             "With 12 ways, rebuilding more than about 10 columns evicts lines before their "
             "remaining 7 values are used. Shifting each column by one cache line spreads them over "
             "different sets and restores the compulsory miss rate of k/8 per row. PAX inherits "
             "the problem when minipage sizes are powers of two.")


def s_ins_prefetch():
    s = new_slide()
    header(s, "Hardware insight 2", "The prefetcher fills the 4-KB page, then stops")
    pts = D["overfetch_pts"]
    runs = [128, 256, 512, 1024, 2048]
    cats = [f"{r} B" for r in runs]

    def avg(run, off):
        v = [p[1] for p in pts if p[2] == run and p[3] == off]
        return round(sum(v) / len(v), 1) if v else 0

    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series("run at page start: extra lines", [avg(r, 0) for r in runs])
    cd.add_series("lines left in the page", [(4096 - r) / 64 for r in runs])
    cd.add_series("run at mid-page: extra lines", [avg(r, 2048) for r in runs])
    cd.add_series("lines left in the page ", [(2048 - r) / 64 for r in runs])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ML, Inches(1.65), Inches(6.9), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend_pos=XL_LEGEND_POSITION.BOTTOM, size=12)
    chart_title(ch, "Lines fetched beyond a sequential run, by run length")
    quiet_axes(ch, val_title="64-B lines", cat_title="length of the sequential run")
    color_series(ch, [PAX, TINT["pax"], NSM, TINT["nsm"]])
    bar_gap(ch, 50, -5)
    labels(ch, "0", size=10)
    x = Inches(7.85)
    bullets(s, x, Inches(1.75), Inches(4.88), Inches(4.8),
            ["A 1 KB run at the start of a page pulls in **~41 more lines** (48 were left); the "
             "same run 2 KB into the page pulls in **~21** (16 were left).",
             "Short runs fetch less (128 B → ~12 lines): the streamer ramps up with the run.",
             "It does not cross the 4-KB page; a run that ends exactly at the boundary instead "
             "pulls 20–50 lines of the next page.",
             "**PAX consequence:** every minipage that starts in a 4-KB page costs up to a page "
             "of traffic. At 1 KB pages SUM(c0) reads **65 B/row for 8 useful bytes**; from "
             "64 KB pages the runs span many OS pages and the tax fades."], size=15, gap=8)
    footer(s, "Calibration kernel: one run of R bytes at offset 0 or 2 KB of every block (block ≥ 8 KB), "
              "DRAM bytes from the memory-controller counters.")
    notes(s, "This matches how Intel's L2 streamer is documented to behave: it tracks ascending "
             "accesses within a 4 KB page and runs ahead of them, but never across the page "
             "boundary. The over-fetch is therefore a per-page tax, which is exactly why small "
             "PAX pages are bad on this machine and why the cost model needed an over-fetch table.")


def s_ins_rows():
    s = new_slide()
    header(s, "Hardware insight 3", "Touch two lines per row, pay for the whole row")
    nr = D["nsm_rows"]["w64_uniform"]
    cats = ["1 column (8 B, 1 line)", "16 columns (128 B, 2 lines)", "64 columns (512 B, 8 lines)"]
    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series("DRAM bytes per row", [nr[p][1] for p in ("p1", "p25", "p100")])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ML, Inches(1.65), Inches(4.4), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend=False, size=12)
    chart_title(ch, "NSM, 512-B rows: DRAM bytes per row")
    quiet_axes(ch)
    color_series(ch, [NSM])
    bar_gap(ch, 60)
    labels(ch, "0", size=12)
    cd = CategoryChartData()
    cd.categories = cats
    cd.add_series("ns per row", [nr[p][0] for p in ("p1", "p25", "p100")])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5.2), Inches(1.65), Inches(3.6), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend=False, size=12)
    chart_title(ch, "ns per row")
    quiet_axes(ch)
    color_series(ch, [INK])
    bar_gap(ch, 60)
    labels(ch, "0.0", size=12)
    x = Inches(9.15)
    stat(s, x, Inches(1.7), Inches(3.6), f"{nr['p25'][1] / nr['p100'][1] * 100:.0f} %",
         "of the bytes for 25 % of the columns", color=NSM, big_size=40)
    bullets(s, x, Inches(3.35), Inches(3.58), Inches(3.3),
            ["One line per row: the core sees a 512-B stride. Only the line’s 128-B pair comes "
             "in (162 B/row).",
             "Two consecutive lines per row: the streamer sees an ascending stream and fetches "
             "the rest of the page: whole rows.",
             "On 16-column (128-B) rows every case reads the whole row (135–148 B)."], size=14, gap=6)
    footer(s, "SUM over the projected columns, 64-column table (512-B rows), 256 MB, cold. "
              "Calibration agrees: one 8-B read per 512 B costs 148 B.")
    notes(s, "So the rule 'NSM reads the whole row' is too simple. It reads the 128-byte line pair "
             "around what you touch, unless your access pattern looks like a stream to the L2 "
             "prefetcher, and then it reads everything. Reading a quarter of the columns costs "
             "the same DRAM traffic as reading all of them.")


def s_ins_branch():
    s = new_slide()
    header(s, "Hardware insight 4", "Mispredicts cost 26 cycles, or almost nothing")
    mp = D["mispredict"]
    cd = CategoryChartData()
    cd.categories = ["DSM", "PAX", "NSM"]
    cd.add_series("25 % pass", [mp[f"{k}|0.25"]["cyc_per_miss"] for k in ("dsm", "pax", "nsm")])
    cd.add_series("50 % pass", [mp[f"{k}|0.5"]["cyc_per_miss"] for k in ("dsm", "pax", "nsm")])
    gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, ML, Inches(1.65), Inches(5.0), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend_pos=XL_LEGEND_POSITION.BOTTOM, size=12)
    chart_title(ch, "Cycles lost per mispredicted branch")
    quiet_axes(ch)
    ch.value_axis.maximum_scale = 30
    color_series(ch, [TINT["dsm"], DSM])
    bar_gap(ch, 60, -5)
    labels(ch, "0.0", size=11)
    pr = D["predictor"]
    cd = CategoryChartData()
    cd.categories = [f"{r * 100:g}%" for r in pr["rates"]]
    cd.add_series("coin-flip bound 2f(1−f)", pr["coin"])
    cd.add_series("PAX", pr["pax"])
    cd.add_series("DSM", pr["dsm"])
    cd.add_series("NSM", pr["nsm"])
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(5.85), Inches(1.65), Inches(6.88), Inches(3.75), cd)
    ch = gf.chart
    style_chart(ch, legend_pos=XL_LEGEND_POSITION.BOTTOM, size=11)
    chart_title(ch, "Mispredictions ÷ min(f, 1−f), the best possible on random data")
    quiet_axes(ch, cat_title="pass rate", size=11)
    ch.value_axis.minimum_scale, ch.value_axis.maximum_scale = 0.8, 2.1
    ch.value_axis.tick_labels.number_format = '0.0"×"'
    ch.value_axis.tick_labels.number_format_is_linked = False
    color_series(ch, [MUTED, PAX, DSM, NSM], line=True, dashes=[True, False, False, False],
                 markers=[XL_MARKER_STYLE.NONE, XL_MARKER_STYLE.CIRCLE, XL_MARKER_STYLE.CIRCLE,
                          XL_MARKER_STYLE.CIRCLE])
    text(s, Inches(5.85), Inches(5.5), Inches(6.88), Inches(1.4),
         ["CPU-bound DSM/PAX loops pay the full 26 cycles. NSM streams 135 B/row from DRAM: the "
          "pipeline refill overlaps the outstanding misses, so a mispredict costs 1–10 cycles.",
          "Observation: NSM’s branch mispredicts at the optimum min(f, 1−f); the tight DSM/PAX "
          "selection loops 1.2–1.8× more (hypothesis: the history holds only the branch’s own "
          "random outcomes, and the predictor chases noise)."], size=13, spacing=4)
    footer(s, "16 columns, 256 MB, SUM(c0) WHERE c0 < K, cold. Cycles per miss = Δcycles ÷ Δmisses, "
              "branchy vs branch-free.")
    notes(s, "Same data, same predicate, three layouts. The penalty of a mispredict is only paid "
             "when the core had useful work to do; in a memory-bound loop the refill hides behind "
             "DRAM latency. This is why branch-free filtering pays off for DSM (up to 1.8x) but "
             "barely for NSM.")


def s_ins_tlb():
    s = new_slide()
    header(s, "Hardware insight 5", "For lookups, the TLB separates PAX from DSM")
    lt = D["lookup_tlb"]["w64_uniform"]
    cd = CategoryChartData()
    cd.categories = lt["pages"]
    cd.add_series("TLB misses per lookup", lt["tlb"])
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, ML, Inches(1.65), Inches(4.0), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend=False, size=12)
    chart_title(ch, "TLB misses per full-tuple lookup")
    quiet_axes(ch, cat_title="PAX page size")
    color_series(ch, [PAX], line=True)
    cd = CategoryChartData()
    cd.categories = lt["pages"]
    cd.add_series("ns per lookup", lt["ns"])
    gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(4.75), Inches(1.65), Inches(4.0), Inches(4.85), cd)
    ch = gf.chart
    style_chart(ch, legend=False, size=12)
    chart_title(ch, "ns per full-tuple lookup")
    quiet_axes(ch, cat_title="PAX page size")
    color_series(ch, [INK], line=True)
    x = Inches(9.1)
    bullets(s, x, Inches(1.75), Inches(3.63), Inches(4.8),
            ["A PAX page of ≤ 4 KB is one OS page: **1 walk** per lookup. At ≥ 256 KB each "
             f"column’s slot is on its own page: **{lt['tlb'][-1]:.0f} walks**, like DSM "
             f"({lt['dsm'][0]:.0f}).",
             f"Time follows: {lt['ns'][0]:.0f} ns (1 KB) → {lt['ns'][-1]:.0f} ns (16 MB); DSM "
             f"{lt['dsm'][1]:.0f}, NSM {lt['nsm'][1]:.0f} ns.",
             "TLB reach (calibrated): **96** first-level and **2,048** second-level entries = "
             "**8 MB, less than the 18 MB L3**. A walk costs ~15 ns with cached page tables, "
             "~100 ns without.",
             "Walks also eat parallelism: 15 misses in flight with 2-MB pages, 12 with 4-KB "
             "pages; DSM’s 64-column lookup reaches only ~7."], size=14, gap=6)
    footer(s, "64-column table, 256 MB, 100,000 uniform lookups of all columns, cold. Same picture on 16 columns "
              "(1 → 15.5 walks; 108 → 212 ns).")
    notes(s, "Random lookups miss the cache in every layout; what differs is how many distinct 4 KB "
             "pages a tuple is spread over, because each one needs a page walk. Since the "
             "second-level TLB maps only 8 MB, even data that fits in L3 pays those walks. "
             "Huge pages would cut DSM's lookup cost sharply; that is a cheap experiment to add.")


def s_ins_summary():
    s = new_slide()
    header(s, "Hardware lessons", "What the hardware means for the layout choice")
    rows = [["Mechanism (measured)", "Consequence for storage design"],
            ["12-way L1, columns at equal page offsets", "Stagger DSM / PAX column starts by 64 B; "
                                                          "rebuilding > 10 columns thrashes otherwise"],
            ["Prefetcher fills the rest of each 4-KB page", "Small PAX pages pay a per-page tax: "
                                                            "use ≥ 64–256 KB pages"],
            ["2 adjacent lines per row trigger the streamer", "NSM projections rarely save DRAM "
                                                               "bytes on wide rows"],
            ["Mispredicts hide behind DRAM stalls", "Branch-free filters pay off for DSM/PAX, "
                                                     "barely for NSM"],
            ["STLB reach 8 MB < L3 18 MB; walks cut MLP", "Lookups: NSM or small-page PAX; DSM "
                                                            "needs huge pages"],
            ["~15 random misses in flight", "Independent lookups cost ~latency ÷ 15, not latency"],
            ["Cold ~1 ms runs vary up to 30–40 %", "Trust repeated, longer measurements; report margins"]]
    table(s, ML, Inches(1.7), CW, rows, [5.2, 6.933], size=14, row_h=0.6,
          align=[PP_ALIGN.LEFT, PP_ALIGN.LEFT])
    footer(s, "Laptop: Intel i7-1360P (Raptor Lake P-core). Re-calibrate on another machine: the numbers move, "
              "the mechanisms stay.")
    notes(s, "Each row pairs a measured mechanism with what it means for choosing and building a "
             "layout. The 15 misses in flight is consistent with the core's line fill buffers. "
             "The last row is a methodological lesson: two apparent anomalies in the ground truth "
             "(PAX 4 MB pages, branchy DSM traffic) did not reproduce in a controlled re-run.")


SLIDES = [s_title, s_question, s_layouts, s_engine, s_method, s_ground_truth, s_bytes, s_branches,
          s_pagesize, s_lookups, s_winners, s_exec, s_ins_divider, s_ins_assoc, s_ins_prefetch,
          s_ins_rows, s_ins_branch, s_ins_tlb, s_ins_summary, s_advisor, s_model, s_calibration, s_accuracy,
          s_decisions, s_maps, s_robust, s_demo, s_lessons, s_conclusion, s_backup_divider,
          s_backup_pax_wins, s_backup_patterns, s_backup_repro]

if __name__ == "__main__":
    for fn in SLIDES:
        fn()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUT)
    print(f"{len(prs.slides)} slides -> {OUT}")
