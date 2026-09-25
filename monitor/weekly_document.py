"""The weekly briefing in detail — a Word document attached to the Friday email.

WHAT THIS REPLACES
------------------
Camlas's weekly briefing is a Word document ("W39 NRLA Weekly Briefing
25 September 2026") with, under each theme, the relevant business of the week
and who said what: the Member's question and the minister's reply, word for
word, for the Business Statement and oral questions, and extracts for the
statements. The Friday email is deliberately short — a line and a link per
item — so the detail goes here, attached to it.

WHAT IS IN IT
-------------
Exactly the items in the email's "This week in the Senedd" (the same
selection, the same NRLA themes: monitor/weekly_review.py), each with:

  * Questions and requests (oral questions, the Business Statement, the
    questions after a statement): every contribution, verbatim.
  * Whole statements and debates: every contribution, verbatim when short,
    and otherwise cut to its passages on NRLA issues. "[…]" marks each cut,
    and a link to the Record gives the full text. The opening passage is
    always kept, so an extract never starts mid-argument.
  * Welsh Government press releases and written statements: the notice's
    own summary points and opening paragraphs.

Then the week's political changes and everything coming up, as in the email.

Nothing is summarised or paraphrased: every word attributed to a speaker is
from the Senedd's Record of Proceedings (English, with the official
interpretation where the words were spoken in Welsh).
"""

from __future__ import annotations

import io
import re
from datetime import date

from .relevance import Taxonomy, find_terms

DARK_BLUE = "113B54"
ORANGE = "E96C19"
OFF_BLACK = "0F2636"
MUTED = "5A7286"
FONT = "Arial"

# How long a contribution may run before it is cut to its relevant passages.
# In a question-and-answer exchange everything is short enough to keep; a
# ministerial statement is not (22 September 2026: the Building Safety
# statement alone was 1,800 words).
FULL_WORDS_EXCHANGE = 600
FULL_WORDS_DEBATE = 250
CAP_WORDS = 450
CAP_WORDS_LEAD = 900          # the statement itself, by the minister
NOTICE_WORDS = 350
CUT = "[…]"

_COURTESY_ONLY = re.compile(
    r"^(diolch|thank you|thanks)\b[^.]{0,60}[.!]?$", re.I)


def filename(today: date) -> str:
    return f"NRLA Senedd weekly briefing {today.strftime('%-d %B %Y')}.docx"


# ---------------------------------------------------------------------------
# Extracts — which of a speaker's own words to keep
# ---------------------------------------------------------------------------

def _words(text: str) -> int:
    return len((text or "").split())


def excerpt(text: str, tax: Taxonomy, full_words: int, cap: int) -> list[str]:
    """The paragraphs of ``text`` to print, verbatim, with CUT where some
    were left out. Short texts come back whole."""
    paras = [tidy(p.strip()) for p in (text or "").split("\n") if p.strip()]
    if not paras:
        return []
    if sum(_words(p) for p in paras) <= full_words:
        return paras

    terms = [t for spec in tax.themes.values() for t in spec.get("terms", [])]
    hits = {i: len(find_terms(p, terms)) for i, p in enumerate(paras)}

    # The opening: the first paragraph that says something, not "Thank you,
    # Dirprwy Lywydd."
    opening = next((i for i, p in enumerate(paras)
                    if _words(p) >= 12 and not _COURTESY_ONLY.match(p)), 0)

    keep = {opening}
    used = _words(paras[opening])
    relevant = sorted((i for i, n in hits.items() if n and i != opening),
                      key=lambda i: (-hits[i], i))
    for i in relevant:
        w = _words(paras[i])
        if used + w > cap and used > 0:
            continue
        keep.add(i)
        used += w
    if len(keep) == 1:
        # Nothing on NRLA issues in this contribution (a colleague's point
        # about something else in a relevant debate): the opening, and the
        # next paragraph if there is room, give its gist in its own words.
        nxt = opening + 1
        if nxt < len(paras) and used + _words(paras[nxt]) <= cap // 2:
            keep.add(nxt)

    out: list[str] = []
    last = -1
    for i in sorted(keep):
        if i > last + 1:
            out.append(CUT)
        p = paras[i]
        if _words(p) > cap:
            p = " ".join(p.split()[:cap]) + " " + CUT
        out.append(p)
        last = i
    if last < len(paras) - 1:
        out.append(CUT)
    return out


_STAGE = re.compile(r"\[\s*([A-Z][a-z]{2,}[^\[\]]{0,40}?)\s*\.?\s*\]")


def tidy(text: str) -> str:
    """"[ Laughter .]" as the page renders it → "[Laughter.]"."""
    return _STAGE.sub(lambda m: f"[{m.group(1).strip()}.]", text)


def _speaker(c) -> str:
    return f"{c.speaker} MS" if getattr(c, "member", False) else c.speaker


# ---------------------------------------------------------------------------
# Word plumbing (python-docx)
# ---------------------------------------------------------------------------

def _rgb(hex_):
    from docx.shared import RGBColor
    return RGBColor.from_string(hex_)


def _hyperlink(paragraph, url: str, text: str, colour: str = DARK_BLUE,
               bold: bool = False, size: float | None = None, underline: bool = True):
    """A clickable link. python-docx has no API for this, so it is built from
    the underlying XML, as its own documentation suggests.

    The run properties must be in the order the schema lays down — b, color,
    sz, u. Word refuses a file with them in any other order ("Word found
    unreadable content", 25 September 2026), though LibreOffice opens it."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    if not url:
        run = paragraph.add_run(text)
        run.bold = bold
        return run
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    if bold:
        props.append(OxmlElement("w:b"))
    col = OxmlElement("w:color")
    col.set(qn("w:val"), colour)
    props.append(col)
    if size:
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size * 2)))
        props.append(sz)
    if underline:
        u = OxmlElement("w:u")
        u.set(qn("w:val"), "single")
        props.append(u)
    run.append(props)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)
    return link


# Everything that may follow <w:pBdr> inside <w:pPr>, in schema order. The
# border has to go before the first of these that is present: appended at the
# end, after <w:spacing> and <w:ind>, it made Word refuse the whole file.
_AFTER_PBDR = ("w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku",
               "w:wordWrap", "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
               "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid",
               "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
               "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
               "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
               "w:rPr", "w:sectPr", "w:pPrChange")
_BORDER_SIDES = ("top", "left", "bottom", "right", "between", "bar")


def _insert_before(parent, child, successors) -> None:
    """Put ``child`` before the first of ``successors`` present in
    ``parent``, or at the end — the schema's order, whatever is there."""
    from docx.oxml.ns import qn
    tags = {qn(t) for t in successors}
    for i, existing in enumerate(parent):
        if existing.tag in tags:
            parent.insert(i, child)
            return
    parent.append(child)


def _border(paragraph, side: str, colour: str, size: int, space: int):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    ppr = paragraph._p.get_or_add_pPr()
    bdr = ppr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        _insert_before(ppr, bdr, _AFTER_PBDR)
    edge = OxmlElement(f"w:{side}")
    edge.set(qn("w:val"), "single")
    edge.set(qn("w:sz"), str(size))
    edge.set(qn("w:space"), str(space))
    edge.set(qn("w:color"), colour)
    later = [f"w:{s}" for s in _BORDER_SIDES[_BORDER_SIDES.index(side) + 1:]]
    old = bdr.find(qn(f"w:{side}"))
    if old is not None:
        bdr.remove(old)
    _insert_before(bdr, edge, later)


def _border_bottom(paragraph, colour: str = ORANGE, size: int = 12, space: int = 4):
    _border(paragraph, "bottom", colour, size, space)


def _border_left(paragraph, colour: str = "D9E2E8", size: int = 12, space: int = 8):
    _border(paragraph, "left", colour, size, space)


def _page_number(paragraph):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        run = paragraph.add_run()
        run.font.size = _pt(8.5)
        run.font.color.rgb = _rgb(MUTED)
        if kind:
            fld = OxmlElement("w:fldChar")
            fld.set(qn("w:fldCharType"), kind)
            run._r.append(fld)
        else:
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = text
            run._r.append(instr)


def _pt(n):
    from docx.shared import Pt
    return Pt(n)


class _Doc:
    """The house style, applied once, and the few kinds of paragraph used."""

    def __init__(self):
        from docx import Document
        from docx.shared import Cm

        self.doc = Document()
        sec = self.doc.sections[0]
        sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
        sec.left_margin = sec.right_margin = Cm(2.0)
        sec.top_margin, sec.bottom_margin = Cm(1.8), Cm(1.8)

        st = self.doc.styles
        normal = st["Normal"]
        normal.font.name = FONT
        normal.font.size = _pt(10.5)
        normal.font.color.rgb = _rgb(OFF_BLACK)
        normal.paragraph_format.space_after = _pt(4)
        normal.paragraph_format.line_spacing = 1.15
        self._east_asian_font(normal)
        for name, size, colour, before, after in (
                ("Title", 24, DARK_BLUE, 0, 2),
                ("Heading 1", 15, DARK_BLUE, 18, 6),
                ("Heading 2", 12, OFF_BLACK, 14, 2),
                ("Heading 3", 10.5, DARK_BLUE, 8, 2)):
            s = st[name]
            s.font.name = FONT
            s.font.size = _pt(size)
            s.font.bold = True
            s.font.italic = False
            s.font.color.rgb = _rgb(colour)
            s.paragraph_format.space_before = _pt(before)
            s.paragraph_format.space_after = _pt(after)
            s.paragraph_format.keep_with_next = True
            self._east_asian_font(s)
        st["List Bullet"].font.name = FONT
        st["List Bullet"].font.size = _pt(10.5)

    @staticmethod
    def _east_asian_font(style):
        # Word falls back to Calibri/Cambria for headings unless every font
        # slot is named.
        from docx.oxml.ns import qn
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            from docx.oxml import OxmlElement
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for slot in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            fonts.set(qn(slot), FONT)
        for theme in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            if fonts.get(qn(theme)) is not None:
                del fonts.attrib[qn(theme)]

    def para(self, text: str = "", size: float | None = None, colour: str | None = None,
             bold: bool = False, italic: bool = False, after: float | None = None,
             style: str | None = None, indent_cm: float | None = None):
        from docx.shared import Cm
        p = self.doc.add_paragraph(style=style)
        if text:
            r = p.add_run(text)
            r.bold, r.italic = bold, italic
            if size:
                r.font.size = _pt(size)
            if colour:
                r.font.color.rgb = _rgb(colour)
        if after is not None:
            p.paragraph_format.space_after = _pt(after)
        if indent_cm is not None:
            p.paragraph_format.left_indent = Cm(indent_cm)
        return p

    def muted(self, p, text: str, size: float = 9):
        r = p.add_run(text)
        r.font.size = _pt(size)
        r.font.color.rgb = _rgb(MUTED)
        return r

    def heading(self, text: str, level: int, url: str = ""):
        h = self.doc.add_heading("", level=level)
        if url:
            _hyperlink(h, url, text, colour=DARK_BLUE if level != 2 else OFF_BLACK,
                       bold=True, underline=False)
        else:
            h.add_run(text)
        return h

    def bullet(self, text: str = ""):
        p = self.doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = _pt(2)
        if text:
            p.add_run(text)
        return p


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def _week(review) -> str:
    s, e = review.week_start, review.week_end
    if s.month == e.month:
        return f"{s:%-d} to {e:%-d %B %Y}"
    return f"{s:%-d %B} to {e:%-d %B %Y}"


def _meta_line(d: _Doc, e) -> None:
    p = d.para(after=4)
    d.muted(p, e.meta)
    links = [("Read in the Record" if e.kind == "debate" else "Read the full notice", e.url)]
    if e.video_url:
        links.append(("Watch on Senedd.tv", e.video_url))
    for label, url in links:
        if url:
            d.muted(p, "  ·  ")
            _hyperlink(p, url, label, size=9)


def _debate(d: _Doc, e, tax: Taxonomy) -> None:
    lead_speaker = None
    if e.whole and e.exchanges and e.exchanges[0][1]:
        lead_speaker = e.exchanges[0][1][0].speaker
    last_heading = None
    for heading, contribs in e.exchanges:
        if heading and heading != last_heading and heading not in e.title:
            d.heading(heading, 3)
        last_heading = heading
        for c in contribs:
            if _words(c.text) < 3:
                continue            # "Diolch." — procedure, not substance
            if e.whole:
                lead = c.speaker == lead_speaker and c is contribs[0]
                paras = excerpt(c.text, tax, FULL_WORDS_DEBATE,
                                CAP_WORDS_LEAD if lead else CAP_WORDS)
            else:
                paras = excerpt(c.text, tax, FULL_WORDS_EXCHANGE, CAP_WORDS)
            if not paras:
                continue
            who = d.para(after=1)
            who.paragraph_format.space_before = _pt(6)
            who.paragraph_format.keep_with_next = True
            r = who.add_run(_speaker(c))
            r.bold = True
            r.font.color.rgb = _rgb(DARK_BLUE)
            if c.role:
                d.muted(who, f"  {c.role}")
            for i, text in enumerate(paras):
                p = d.para(text, colour=MUTED if text == CUT else None,
                           after=3, indent_cm=0.35)
                _border_left(p)
                if i < len(paras) - 1:
                    p.paragraph_format.keep_with_next = False
            d.doc.paragraphs[-1].paragraph_format.space_after = _pt(4)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def notice_paragraphs(e) -> list[str]:
    """A notice's paragraphs, without its summary points repeated (the
    newsroom page carries them twice: on the card and at the top of the
    story) and without stray punctuation lines ("Siân Gwenllian, said" / ":")."""
    points = " ".join(_norm(p) for p in e.points)
    out: list[str] = []
    seen: set[str] = set()
    for text in e.paragraphs:
        text = (text or "").strip()
        n = _norm(text)
        if not n:
            if out:
                out[-1] = out[-1] + text          # "said" + ":"
            continue
        if n in seen or (points and n in points) or n == _norm(e.title):
            continue
        seen.add(n)
        out.append(text)
    return out


def _notice(d: _Doc, e) -> None:
    for point in e.points[:5]:
        d.bullet(point)
    words = 0
    for text in notice_paragraphs(e):
        w = _words(text)
        if words and words + w > NOTICE_WORDS:
            d.para(CUT, colour=MUTED, after=3, indent_cm=0.35)
            break
        p = d.para(text, after=3, indent_cm=0.35)
        _border_left(p)
        words += w


def _coming_up(d: _Doc, sections: dict, today: date) -> None:
    from .forward import _countdown, _display_date
    d.heading("Coming up", 1)
    groups = (
        ("Oral questions tabled for forthcoming sittings", sections.get("oral", []), "oral"),
        ("Committee meetings", sections.get("committees", []), "committee"),
        ("Plenary business", sections.get("plenary", []), "plenary"),
        ("Consultations closing soonest", sections.get("consultations", []), "consultation"),
    )
    any_rows = False
    for label, rows, kind in groups:
        if not rows:
            continue
        any_rows = True
        d.heading(label, 3)
        for i in rows:
            p = d.bullet()
            text = (i.body or i.title) if kind == "oral" else (i.title or "(untitled)")
            _hyperlink(p, i.url, text, colour=OFF_BLACK, underline=False, bold=True)
            if kind == "oral":
                who = i.speaker + (f" ({i.constituency})" if i.constituency else "")
                when = i.deadline
                extra = [who, i.title, i.agenda_item,
                         f"for answer {_display_date(when)}" if when else ""]
            elif kind == "consultation":
                note, _ = _countdown(i.deadline, today)
                extra = [i.forum or i.source_name or "",
                         (f"closes {_display_date(i.deadline)} ({note})" if i.deadline
                          else "closing date not given — check the source")]
            else:
                extra = [i.agenda_item if kind == "committee" else (i.forum or ""),
                         _display_date(i.item_date) if i.item_date else ""]
            extra = [x for x in extra if x]
            if extra:
                d.muted(p, " — " + " · ".join(extra))
    if not any_rows:
        d.para("Nothing scheduled or open on NRLA issues.", colour=MUTED)


def build_document(review, tax: Taxonomy, sections: dict | None = None,
                   today: date | None = None, page_url: str = "") -> bytes | None:
    """The .docx as bytes, or None when there is nothing this week to detail
    (the Senedd did not sit and nothing was published)."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if review is None or not review.entries:
        return None
    today = today or review.week_end
    d = _Doc()

    # Header and footer
    sec = d.doc.sections[0]
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    d.muted(hp, "NRLA  ·  Senedd weekly briefing  ·  " + _week(review), size=8.5)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    d.muted(fp, "Page ", size=8.5)
    _page_number(fp)

    # Title block
    title = d.doc.add_paragraph(style="Title")
    title.add_run("Senedd weekly briefing")
    sub = d.para(after=2)
    r = sub.add_run(f"In detail  ·  {_week(review)}")
    r.bold, r.font.size, r.font.color.rgb = True, _pt(12), _rgb(ORANGE)
    org = d.para(after=10)
    d.muted(org, "National Residential Landlords Association", size=10)
    _border_bottom(org)

    intro = d.para(after=8)
    intro.add_run(
        "This goes with the Friday email. For each item on NRLA issues this week "
        "it gives who said what, in their own words, from the Senedd's Record of "
        "Proceedings. Questions and their answers are in full. Longer statements "
        "are cut to the passages on NRLA issues; ").font.size = _pt(10)
    cut = intro.add_run(CUT)
    cut.font.size, cut.font.color.rgb = _pt(10), _rgb(MUTED)
    intro.add_run(" marks a cut, and each heading links to the full text.").font.size = _pt(10)

    # Headlines
    heads = review.headlines()
    if heads:
        d.heading("Headlines", 1)
        for e in heads:
            p = d.bullet()
            _hyperlink(p, e.url, e.title, colour=OFF_BLACK, underline=False, bold=True)
            d.muted(p, " — " + e.meta)

    # By theme — every entry, not just the email's top six per theme
    from .weekly_review import OTHER, THEMES
    for key, label, _ in THEMES + [(OTHER[0], OTHER[1], ())]:
        rows = sorted((e for e in review.entries if e.theme == key),
                      key=lambda e: (e.when or date.min, -e.score))
        if not rows:
            continue
        d.heading(label, 1)
        for e in rows:
            d.heading(e.title, 2, url=e.url)
            _meta_line(d, e)
            if e.kind == "notice":
                _notice(d, e)
            else:
                _debate(d, e, tax)

    if review.changes:
        d.heading("Political changes", 1)
        d.para("As reported in this week's news alerts.", colour=MUTED, size=9.5)
        for c in review.changes:
            p = d.bullet()
            _hyperlink(p, c.get("url", ""), c.get("title", ""), colour=OFF_BLACK,
                       underline=False, bold=True)
            if c.get("at"):
                d.muted(p, " — " + c["at"].strftime("%a %-d %b"))

    if review.pending:
        d.para("Not yet in the Record, so not covered: " + "; ".join(review.pending) + ".",
               colour=MUTED, size=9.5)

    if sections is not None:
        _coming_up(d, sections, today)

    # Source note
    end = d.para(after=0)
    end.paragraph_format.space_before = _pt(18)
    _border_bottom(end, colour="D9E2E8", size=6)
    note = d.para(size=8.5, colour=MUTED)
    note.add_run(
        "Every word attributed to a speaker is from the Senedd's Record of "
        "Proceedings: English as spoken, or the official interpretation where the "
        "words were spoken in Welsh. Nothing is summarised or paraphrased. "
        "Senedd Cymru and Welsh Government material is reproduced under the Open "
        "Government Licence v3.0.").font.size = _pt(8.5)
    for run in note.runs:
        run.font.color.rgb = _rgb(MUTED)
    if page_url:
        p = d.para(after=0)
        d.muted(p, "Everything here is also on the ", size=8.5)
        _hyperlink(p, page_url, "live page", size=8.5)
        d.muted(p, ".", size=8.5)

    _fix_settings(d.doc)
    buf = io.BytesIO()
    d.doc.save(buf)
    return buf.getvalue()


def _fix_settings(doc) -> None:
    """python-docx's template has <w:zoom> without the percent the schema
    requires. Word tolerates it; there is no reason to rely on that."""
    from docx.oxml.ns import qn
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")
