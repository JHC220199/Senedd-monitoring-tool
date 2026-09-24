"""Press release alerts — an email when the Welsh Government publishes
something the NRLA should know about.

WHAT THIS REPLACES
------------------
Camlas's "Alert - Press Release" emails. On 23 September 2026 at 14.46 they
sent "Welsh Government to fund interim alarm measures for leaseholders facing
waking watch costs" — the notice's own three bullet points and a link. The
notice had been published at 18.00 the evening before.

This does the same within the hour: the hourly news run (news.yml) also reads
the Welsh Government's announcements, and any that match the NRLA's
relevance rules — the same rules as the live page and the NRLA tags in the
morning briefing — are emailed straight away.

WHAT IS INCLUDED
----------------
Press notices from the Welsh Government newsroom, and written statements and
announcements from the gov.wales feed (the only place written statements are
published). "Oral Statement:" items are not: they are the text of statements
already made in the Chamber, which the debate summaries cover.

The email carries the notice's own summary points, verbatim, as Camlas's
does — Welsh Government material is published under the Open Government
Licence, which allows exactly this with attribution. Nothing is paraphrased.

MEMORY
------
Kept with the news alerts' memory (the Actions cache): which notices have
been seen. The first run learns what is already out there and sends nothing.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, OFF_WHITE,
                      ORANGE, WIDTH)
from .models import Item


# How far back a run looks. Each run starts where the last one finished, with
# this overlap, so a notice published during a run is not missed.
OVERLAP = timedelta(hours=2)
FIRST_LOOK = timedelta(days=3)
MAX_LOOK = timedelta(days=4)
SEEN_DAYS = 30

LABELS = {
    "written_statement": "Written statement",
    "consultation": "Consultation",
    "announcement": "Press release",
}


@dataclass
class Release:
    item: Item
    published_utc: datetime | None
    points: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS.get(self.item.source_kind, "Press release")


def look_since(state: dict, now: datetime) -> datetime:
    """Where this run starts reading from."""
    last = state.get("press_checked")
    try:
        start = datetime.fromisoformat(last) - OVERLAP if last else now - FIRST_LOOK
    except ValueError:
        start = now - FIRST_LOOK
    return max(start, now - MAX_LOOK)


def select(announcements: list[tuple], marker, state: dict,
           test: bool = False) -> list[Release]:
    """Relevant notices not alerted on before (or, for a test, the latest
    relevant ones whether or not they were)."""
    seen = state.get("press_seen", {})
    out = []
    for at, item, lead in announcements:
        if item.title.lower().startswith("oral statement"):
            continue
        if not test and item.url in seen:
            continue
        if not marker.item(item):
            continue
        points = list(getattr(item, "points", []) or ([lead] if lead else []))
        out.append(Release(item=item, published_utc=at, points=points))
    out.sort(key=lambda r: r.published_utc or datetime.min, reverse=True)
    return out[:3] if test else out


def remember(state: dict, announcements: list[tuple], now: datetime) -> dict:
    seen = state.setdefault("press_seen", {})
    for _, item, _ in announcements:
        if item.url:
            seen.setdefault(item.url, now.isoformat(timespec="seconds"))
    cutoff = now - timedelta(days=SEEN_DAYS)
    kept = {}
    for url, when in seen.items():
        try:
            if datetime.fromisoformat(when) >= cutoff:
                kept[url] = when
        except ValueError:
            pass
    state["press_seen"] = kept
    state["press_checked"] = now.isoformat(timespec="seconds")
    return state


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------

def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _when(r: Release) -> str:
    if r.published_utc is not None:
        from .morning import LONDON
        local = r.published_utc.replace(tzinfo=timezone.utc).astimezone(LONDON)
        return local.strftime("%a %-d %B, %H.%M")
    if r.item.item_date:
        return r.item.item_date.strftime("%a %-d %B")
    return ""


def _card(r: Release) -> str:
    tag = (f'<span style="background:#FDEDE0;color:#A8501A;font-size:10px;font-weight:700;'
           f'letter-spacing:.6px;padding:2px 6px;margin-right:7px">NRLA</span>'
           f'<span style="background:#E8EEF2;color:{DARK_BLUE};font-size:10px;font-weight:700;'
           f'letter-spacing:.6px;padding:2px 6px;text-transform:uppercase">{_e(r.label)}</span>')
    points = "".join(
        f'<tr><td width="16" style="width:16px;vertical-align:top;padding:3px 0 0;'
        f'font-family:{FONT};font-size:13.5px;color:{ORANGE}">&#8226;</td>'
        f'<td style="padding:3px 0 0;font-family:{FONT};font-size:13.5px;line-height:1.55;'
        f'color:{OFF_BLACK}">{_e(p)}</td></tr>' for p in r.points[:5])
    meta = " · ".join(_e(x) for x in (_when(r), r.item.speaker) if x)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;width:100%;background:#ffffff;border:1px solid {EDGE}">'
        f'<tr><td style="padding:14px 18px 4px;font-family:{FONT}">{tag}</td></tr>'
        f'<tr><td style="padding:6px 18px 0;font-family:{FONT}">'
        f'<a href="{_e(r.item.url)}" style="font-size:16px;line-height:1.4;font-weight:700;'
        f'color:{OFF_BLACK};text-decoration:none">{_e(r.item.title)}</a>'
        f'<div style="font-size:12px;color:{MUTED};padding-top:4px">{meta}</div></td></tr>'
        + (f'<tr><td style="padding:8px 18px 4px"><table role="presentation" cellpadding="0" '
           f'cellspacing="0" border="0" style="border-collapse:collapse">{points}</table></td></tr>'
           if points else "")
        + f'<tr><td style="padding:10px 18px 14px;font-family:{FONT};font-size:12.5px;'
        f'border-top:1px solid {LINE}"><a href="{_e(r.item.url)}" style="color:{DARK_BLUE};'
        f'font-weight:600;text-decoration:none">Read the full notice &rsaquo;</a></td></tr>'
        f'</table><div style="height:16px;line-height:16px;font-size:0">&nbsp;</div>')


def render_press(releases: list[Release], test: bool = False) -> tuple[str, str, int]:
    count = len(releases)
    if not count:
        return "", "", 0
    first = releases[0]
    kind = "written statement" if first.label == "Written statement" else "press release"
    subject = (("TEST — " if test else "")
               + (f"Welsh Government {kind}: {first.item.title}" if count == 1 else
                  f"Welsh Government: {first.item.title} (+{count - 1} more)"))
    banner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'border="0" style="border-collapse:collapse;background:#FFF4E5;'
              f'border:1px solid #F3C98B"><tr><td style="padding:12px 16px;font-family:{FONT};'
              f'font-size:13px;line-height:1.5;color:#8A3B06"><b>This is a test.</b> It shows '
              f'the most recent relevant Welsh Government notices, which you may have seen '
              f'already. Real alerts carry no banner and only ever contain something new.'
              f'</td></tr></table><div style="height:16px;line-height:16px;font-size:0">'
              f'&nbsp;</div>') if test else ""
    cards = "".join(_card(r) for r in releases)
    body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">
  <tr><td bgcolor="{DARK_BLUE}" style="padding:20px 28px 16px;font-family:{FONT};color:#ffffff">
    <div style="font-size:19px;font-weight:700;color:#ffffff">Welsh Government press alert</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:4px">
      National Residential Landlords Association</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;font-size:0">&nbsp;</td></tr>
  <tr><td style="padding:20px 28px 0;font-family:{FONT}">{banner}{cards}</td></tr>
  <tr><td style="padding:0 28px 30px;font-family:{FONT}">
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;padding-top:4px">
      Welsh Government notices that match the NRLA's relevance rules — the same
      rules as the live page. The points under each are the notice's own words.
      Contains public sector information licensed under the Open Government
      Licence v3.0.
    </p>
  </td></tr>
</table>
</td></tr></table>"""
    return subject, body, count
