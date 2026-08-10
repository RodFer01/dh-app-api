"""
PPTX Export router — generates SAP-branded PowerPoint presentations
from pipeline/analysis data using the official SAP KOM template.
"""
import os, io, math
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from auth import verify_token

router = APIRouter(prefix="/api/export", tags=["export"])

TEMPLATE_PATH = os.path.join(
    os.path.expanduser("~"),
    "Downloads/PPT/SAP_GTMKOM_PPT Template_V2.pptx"
)

SAP_BLUE   = (0,   112, 184)
SAP_GREEN  = (37,  111,  58)
SAP_ORANGE = (231, 101,   0)
SAP_RED    = (187,   0,   0)
SAP_GRAY   = (120, 143, 166)
WHITE      = (255, 255, 255)


# ── Pydantic models ───────────────────────────────────────────────────────────

class KpiItem(BaseModel):
    label: str
    value: str
    sub:   Optional[str] = ""
    color: Optional[str] = "blue"   # blue | green | orange | red | grey

class ChartBarItem(BaseModel):
    label: str
    value: float

class LobRow(BaseModel):
    lob:          str
    n_booked:     int
    acv_booked:   float
    n_committed:  int
    n_probable:   int
    n_upside:     int
    n_lost:       int
    conversion_rate: Optional[float] = None
    is_finance_sub: bool = False

class ChartImage(BaseModel):
    title:   str
    dataUrl: str   # base64 data URL: "data:image/png;base64,..."

class PptxExportPayload(BaseModel):
    page:         str
    title:        str
    subtitle:     Optional[str] = ""
    filters:      Optional[Dict[str, Any]] = {}
    kpis:         List[KpiItem] = []
    bar_charts:   List[Dict[str, Any]] = []
    lob_rows:     List[LobRow] = []
    chart_images: List[ChartImage] = []   # captured from live ECharts
    currency:     str = "USD"
    author:       Optional[str] = ""
    date_label:   Optional[str] = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _rgb(t):
    from pptx.dml.color import RGBColor
    return RGBColor(*t)

def _color_for(name: str):
    return {
        "green":  SAP_GREEN,
        "orange": SAP_ORANGE,
        "red":    SAP_RED,
        "grey":   SAP_GRAY,
        "gray":   SAP_GRAY,
        "blue":   SAP_BLUE,
    }.get(name, SAP_BLUE)

def _add_rect(slide, l, t, w, h, fill_rgb, text=None, font_size=11,
              bold=False, font_rgb=WHITE, align="left"):
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    shape = slide.shapes.add_shape(1, Inches(l), Inches(t), Inches(w), Inches(h))
    shape.fill.solid(); shape.fill.fore_color.rgb = _rgb(fill_rgb)
    shape.line.fill.background()
    if text is not None:
        tf = shape.text_frame; tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
        run = p.add_run(); run.text = str(text)
        run.font.size = Pt(font_size); run.font.bold = bold
        run.font.color.rgb = _rgb(font_rgb)
    return shape

def _add_text(slide, text, l, t, w, h, font_size=11, bold=False,
              color=SAP_BLUE, align="left"):
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    txb = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    txb.word_wrap = True
    tf = txb.text_frame
    p = tf.paragraphs[0]
    p.alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
    run = p.add_run(); run.text = str(text)
    run.font.size = Pt(font_size); run.font.bold = bold
    run.font.color.rgb = _rgb(color)
    return txb

def _bar_chart(slide, l, t, w, h, data: list, title: str, bar_color=SAP_BLUE,
               currency="USD", fmt="M"):
    """Draw a simple horizontal bar chart using rectangles."""
    from pptx.util import Inches, Pt
    _add_text(slide, title, l, t, w, 0.3, font_size=10, bold=True, color=(50,54,58))
    if not data: return
    max_val = max(abs(d["value"]) for d in data) or 1
    bar_area_w = w * 0.6
    label_w    = w * 0.35
    val_w      = w * 0.18
    row_h      = min((h - 0.35) / len(data), 0.28)
    y = t + 0.35

    for d in data:
        lbl = str(d.get("label",""))[:22]
        val = float(d.get("value", 0))
        ratio = abs(val) / max_val
        bw = bar_area_w * ratio

        # label
        _add_text(slide, lbl, l, y, label_w, row_h*0.8,
                  font_size=8, color=(86,107,130))
        # bar
        if bw > 0.01:
            _add_rect(slide, l + label_w, y + row_h*0.1,
                      bw, row_h*0.7, bar_color)
        # value label
        if fmt == "M":
            if abs(val) >= 1_000_000:
                v_str = f"${val/1_000_000:.1f}M"
            elif abs(val) >= 1_000:
                v_str = f"${val/1_000:.0f}K"
            else:
                v_str = f"${val:.0f}"
        else:
            v_str = f"{int(val):,}"
        _add_text(slide, v_str, l + label_w + bw + 0.05, y, val_w, row_h*0.8,
                  font_size=8, bold=True, color=_rgb(bar_color) if isinstance(bar_color, tuple) else (0,112,184))
        y += row_h


# ── slide builders ────────────────────────────────────────────────────────────

def _slide_title(prs, title, subtitle, author, date_label):
    layout = prs.slide_layouts[0]   # KOM 2025 Title 1
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title
            for para in ph.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = __import__('pptx').util.Pt(28)
                    run.font.bold = True
        elif ph.placeholder_format.idx == 11:
            ph.text = f"{author}  |  {date_label}" if author else date_label
    return slide


def _slide_kpis(prs, kpis: list, title: str):
    layout = prs.slide_layouts[3]   # 1-Col Content
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title
    if not kpis: return slide

    n     = len(kpis)
    cols  = min(n, 6)
    rows  = math.ceil(n / cols)
    cw    = 12.5 / cols
    ch    = 1.6
    x0    = 0.42
    y0    = 1.6

    for i, kpi in enumerate(kpis):
        col = i % cols
        row = i // cols
        x   = x0 + col * cw
        y   = y0 + row * (ch + 0.15)
        color = _color_for(kpi.get("color","blue"))

        # card background
        _add_rect(slide, x, y, cw-0.08, ch, (245,246,247))
        # top accent bar
        _add_rect(slide, x, y, cw-0.08, 0.06, color)
        # label
        _add_text(slide, kpi.get("label",""), x+0.08, y+0.1, cw-0.2, 0.3,
                  font_size=8, color=SAP_GRAY)
        # value
        _add_text(slide, kpi.get("value",""), x+0.08, y+0.38, cw-0.2, 0.7,
                  font_size=20, bold=True, color=color)
        # sub
        if kpi.get("sub"):
            _add_text(slide, kpi["sub"], x+0.08, y+1.1, cw-0.2, 0.3,
                      font_size=8, color=SAP_GRAY)
    return slide


def _slide_charts(prs, charts: list, title: str, currency="USD"):
    layout = prs.slide_layouts[11]  # 2-Column
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title

    colors = [SAP_BLUE, SAP_GREEN, SAP_ORANGE, (100,60,167), SAP_RED]
    n = len(charts)
    if n == 0: return slide

    cols = min(n, 2)
    cw   = 5.8
    ch   = 5.2
    x0   = 0.5
    y0   = 1.5
    gap  = 0.5

    for i, chart in enumerate(charts[:4]):
        col = i % 2
        row = i // 2
        x   = x0 + col * (cw + gap)
        y   = y0 + row * (ch + 0.3)
        data = [{"label": d.get("label",""), "value": d.get("value",0)}
                for d in chart.get("data", [])]
        fmt = "M" if "ACV" in chart.get("title","") else "N"
        _bar_chart(slide, x, y, cw, ch, data,
                   chart.get("title",""), colors[i % len(colors)],
                   currency, fmt)
    return slide


def _slide_chart_image(prs, img_b64: str, title: str):
    """Embed a base64 PNG image as a full-width slide using the 1-Col Content layout."""
    import base64, io as _io
    layout = prs.slide_layouts[3]   # 1-Col Content
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title

    # Strip data URL prefix
    if ',' in img_b64:
        img_b64 = img_b64.split(',', 1)[1]
    img_bytes = base64.b64decode(img_b64)
    img_stream = _io.BytesIO(img_bytes)

    from pptx.util import Inches
    # Full-width image below title
    slide.shapes.add_picture(img_stream, Inches(0.4), Inches(1.45), Inches(12.5), Inches(5.7))
    return slide


def _slide_kpi_grid(prs, kpis: list, title: str):
    """KPI cards slide — 2 rows × up to 4 cols, SAP-styled."""
    layout = prs.slide_layouts[3]
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title
    if not kpis: return slide

    n     = len(kpis)
    cols  = min(n, 4)
    rows  = math.ceil(n / cols)
    cw    = 12.4 / cols
    ch    = 2.0
    x0    = 0.42
    y0    = 1.6

    for i, kpi in enumerate(kpis):
        col = i % cols
        row = i // cols
        x   = x0 + col * cw
        y   = y0 + row * (ch + 0.12)
        color = _color_for(kpi.get("color","blue"))

        _add_rect(slide, x, y,         cw-0.1, ch,     (245,246,247))
        _add_rect(slide, x, y,         cw-0.1, 0.07,   color)
        _add_text(slide, kpi.get("label",""), x+0.1, y+0.13, cw-0.25, 0.32,
                  font_size=9, color=SAP_GRAY)
        _add_text(slide, kpi.get("value",""), x+0.1, y+0.42, cw-0.25, 0.85,
                  font_size=24, bold=True, color=color)
        if kpi.get("sub"):
            _add_text(slide, kpi["sub"], x+0.1, y+1.35, cw-0.25, 0.35,
                      font_size=9, color=SAP_GRAY)
    return slide
    layout = prs.slide_layouts[3]
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title
    if not lob_rows: return slide

    headers = ["LoB", "Booked #", f"ACV ({currency})", "Commit", "Prob", "Upside", "Lost", "Conv%"]
    col_ws  = [3.0,   0.8,        1.4,                  0.7,      0.7,    0.7,      0.7,   0.7]
    x0, y0  = 0.42, 1.55
    row_h   = 0.28

    # Header
    x = x0
    for j, (hdr, cw) in enumerate(zip(headers, col_ws)):
        _add_rect(slide, x, y0, cw-0.02, row_h, SAP_BLUE,
                  text=hdr, font_size=8, bold=True, font_rgb=WHITE,
                  align="center" if j > 0 else "left")
        x += cw

    def fmt_acv(v):
        if abs(v) >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if abs(v) >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v:.0f}"

    for ri, row in enumerate(lob_rows):
        y   = y0 + (ri + 1) * row_h
        bg  = (249,250,251) if not row.get("is_finance_sub") else (245,248,252)
        x   = x0
        lob_label = ("  ↳ " if row.get("is_finance_sub") else "") + row.get("lob","")
        vals = [
            lob_label,
            str(row.get("n_booked",0)),
            fmt_acv(row.get("acv_booked",0)),
            str(row.get("n_committed",0)),
            str(row.get("n_probable",0)),
            str(row.get("n_upside",0)),
            str(row.get("n_lost",0)),
            f"{row.get('conversion_rate',0):.1f}%" if row.get("conversion_rate") else "—",
        ]
        cr = row.get("conversion_rate") or 0
        conv_color = SAP_GREEN if cr >= 30 else SAP_ORANGE if cr >= 15 else SAP_RED

        for j, (val, cw) in enumerate(zip(vals, col_ws)):
            fc = SAP_BLUE if j == 0 and not row.get("is_finance_sub") else (50,54,58)
            if j == 7: fc = conv_color
            _add_rect(slide, x, y, cw-0.02, row_h, bg,
                      text=val, font_size=8,
                      bold=(j==0 and not row.get("is_finance_sub")),
                      font_rgb=fc,
                      align="center" if j > 0 else "left")
            x += cw

    return slide


def _slide_lob_table(prs, lob_rows: list, title: str, currency="USD"):
    layout = prs.slide_layouts[3]
    slide  = prs.slides.add_slide(layout)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 0:
            ph.text = title
    if not lob_rows: return slide

    headers = ["LoB", "Booked #", f"ACV ({currency})", "Commit", "Prob", "Upside", "Lost", "Conv%"]
    col_ws  = [3.0, 0.8, 1.4, 0.7, 0.7, 0.7, 0.7, 0.7]
    x0, y0  = 0.42, 1.55
    row_h   = 0.28

    x = x0
    for j, (hdr, cw) in enumerate(zip(headers, col_ws)):
        _add_rect(slide, x, y0, cw-0.02, row_h, SAP_BLUE,
                  text=hdr, font_size=8, bold=True, font_rgb=WHITE,
                  align="center" if j > 0 else "left")
        x += cw

    def fmt_acv(v):
        if abs(v) >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if abs(v) >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v:.0f}"

    for ri, row in enumerate(lob_rows):
        y   = y0 + (ri + 1) * row_h
        bg  = (249, 250, 251) if not row.get("is_finance_sub") else (245, 248, 252)
        x   = x0
        lob_label = ("  ↳ " if row.get("is_finance_sub") else "") + row.get("lob","")
        vals = [
            lob_label,
            str(row.get("n_booked", 0)),
            fmt_acv(row.get("acv_booked", 0)),
            str(row.get("n_committed", 0)),
            str(row.get("n_probable", 0)),
            str(row.get("n_upside", 0)),
            str(row.get("n_lost", 0)),
            f"{row.get('conversion_rate', 0):.1f}%" if row.get("conversion_rate") else "—",
        ]
        cr = row.get("conversion_rate") or 0
        conv_color = SAP_GREEN if cr >= 30 else SAP_ORANGE if cr >= 15 else SAP_RED

        for j, (val, cw) in enumerate(zip(vals, col_ws)):
            fc = SAP_BLUE if j == 0 and not row.get("is_finance_sub") else (50, 54, 58)
            if j == 7: fc = conv_color
            _add_rect(slide, x, y, cw-0.02, row_h, bg,
                      text=val, font_size=8,
                      bold=(j == 0 and not row.get("is_finance_sub")),
                      font_rgb=fc,
                      align="center" if j > 0 else "left")
            x += cw

    return slide


def _slide_filter_summary(prs, filters: dict, page: str):
    layout = prs.slide_layouts[26]   # Footer Only
    slide  = prs.slides.add_slide(layout)
    _add_text(slide, "Filters Applied", 0.42, 0.3, 12, 0.4,
              font_size=12, bold=True, color=SAP_BLUE)
    y = 0.75
    for k, v in filters.items():
        if not v: continue
        label = k.replace("_", " ").title()
        value = ", ".join(v) if isinstance(v, list) else str(v)
        _add_text(slide, f"• {label}: {value}", 0.6, y, 12, 0.28,
                  font_size=9, color=(50,54,58))
        y += 0.3
        if y > 6.8: break
    return slide


# ── main endpoint ─────────────────────────────────────────────────────────────

@router.post("/pptx")
def export_pptx(payload: PptxExportPayload, _user=Depends(verify_token)):
    try:
        from pptx import Presentation
        from pptx.util import Pt
    except ImportError:
        raise HTTPException(status_code=500, detail="python-pptx not installed")

    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(status_code=500,
            detail=f"SAP template not found at {TEMPLATE_PATH}")

    prs = Presentation(TEMPLATE_PATH)

    author = _user.get("name", payload.author or "Digital Hub SA")
    date_l = payload.date_label or __import__('datetime').datetime.now().strftime("%B %Y")

    # 1 — Title slide
    _slide_title(prs, payload.title, payload.subtitle, author, date_l)

    # 2 — KPI summary slide (always include if kpis present)
    if payload.kpis:
        _slide_kpi_grid(prs, [k.dict() for k in payload.kpis],
                        f"Summary — {payload.title}")

    # 3 — Chart image slides (one per chart, full-width)
    if payload.chart_images:
        for ci in payload.chart_images:
            if ci.dataUrl and len(ci.dataUrl) > 100:
                try:
                    _slide_chart_image(prs, ci.dataUrl, ci.title)
                except Exception:
                    pass  # skip malformed images

    # 4 — Fallback: bar chart slides if no images were sent
    elif payload.bar_charts:
        for i in range(0, len(payload.bar_charts), 2):
            chunk = payload.bar_charts[i:i+2]
            _slide_charts(prs, chunk,
                          chunk[0].get("title","Analysis") if len(chunk) == 1
                          else "Pipeline Analysis",
                          payload.currency)

    # 5 — LoB table slide
    if payload.lob_rows:
        _slide_lob_table(prs,
                         [r.dict() for r in payload.lob_rows],
                         "Results by Line of Business",
                         payload.currency)

    # 6 — Filter summary slide
    active_filters = {k: v for k, v in (payload.filters or {}).items()
                      if v and v != [] and v != ""}
    if active_filters:
        _slide_filter_summary(prs, active_filters, payload.page)

    # Stream as download
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)

    safe_title = payload.title.replace(" ", "_").replace("/","_")[:40]
    filename   = f"SAP_{safe_title}_{date_l.replace(' ','_')}.pptx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
