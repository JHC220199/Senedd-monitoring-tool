"""Statement of Opinion alerts — an email when a Member tables one on an NRLA
issue.

WHAT THIS REPLACES
------------------
Camlas's "Alert - Statement of Opinion" emails. On 29 September 2026 they
sent "Kiera Marshall MS has tabled a opinion regarding Local Housing
Allowance which may be of interest", with the reference and a link. Statements
of Opinion are the Senedd's nearest equivalent of Westminster's early day
motions.

This sends the same within the hour: the hourly news run (news.yml) also reads
any new Statements of Opinion (monitor/collectors/opinions.py), and those that
match the NRLA's relevance rules are emailed, with the statement's own words,
who tabled it, and a link. Alerts only, by the directorate's choice
(29 September 2026): they are not added to the morning or Friday emails.

MEMORY
------
The number of the last statement read, kept with the news alerts' memory (the
Actions cache). The first run finds where the numbering has got to and sends
nothing: only statements tabled after it are alerted.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

from .forward import (DARK_BLUE, EDGE, FONT, LINE, MUTED, OFF_BLACK, OFF_WHITE,
                      ORANGE, WIDTH)
from .models import Item

# The newest statement on 29 September 2026 was number 544. A first run with
# no memory reads from just before it rather than from number 1.
FIRST_NUMBER = 540
# On a first run, only statements this recent are alerted; older ones are
# noted and skipped.
FIRST_LOOK = timedelta(days=3)
STATE_KEY = "opinions_last"

_HOMES = re.compile(r"\b(hous(e|es|ing)|homes?|dwellings?|landlords?|tenants?|"
                    r"rent(s|ed|ing|al)?|renters?|leaseholders?)\b", re.I)


def as_item(op) -> Item:
    """A Statement of Opinion as an archive Item, for the relevance rules."""
    return Item(source_kind="statement_of_opinion",
                source_name="Statement of Opinion",
                title=op.title, body=f"{op.title}\n{op.text}", url=op.url,
                item_date=op.tabled,
                speaker=op.tabled_by.name if op.tabled_by else "")


def relevant(op, marker) -> bool:
    """It must match the NRLA's relevance rules, AND on a substantive NRLA
    theme — not only a "Context" one such as housing statistics. A statement
    is a few sentences, all of them its point, so the whole text counts
    (unlike a press notice, where only the title and summary do). Checked
    against every Statement of Opinion from 2024 to September 2026:
    "Provision of public Information" (OPIN-2024-0414) qualified only because
    it cites the National Survey for Wales, and is not NRLA business.
    """
    item = as_item(op)
    if not marker.item(item):
        return False
    tax = marker.tax
    generic = set(tax.site_config.get("non_qualifying_themes", []) or [])
    headline = Item(source_kind=item.source_kind, source_name=item.source_name,
                    title=op.title, body=op.text)
    marker.scorer.score_item(headline)
    themes = [t for t in (headline.themes or [])
              if t not in generic and tax.themes.get(t, {}).get("tier") != "Context"]
    # Planning on its own is not enough: "Protection of the Gwent Levels"
    # (OPIN-2024-0436) is about solar farms on a wildlife site, and names
    # only "planning policy". With a word about homes, planning is NRLA
    # business; without one, it is not.
    if themes == ["planning_system"] and not _HOMES.search(f"{op.title} {op.text}"):
        return False
    return bool(themes)


def select(opinions: list, marker, first_run: bool = False,
           today: date | None = None) -> list:
    today = today or date.today()
    out = []
    for op in opinions:
        if first_run and op.tabled and op.tabled < today - FIRST_LOOK:
            continue
        if relevant(op, marker):
            out.append(op)
    return out


def last_number(state: dict) -> int | None:
    try:
        return int(state[STATE_KEY])
    except (KeyError, TypeError, ValueError):
        return None


def remember(state: dict, newest: int) -> dict:
    if newest and newest > (last_number(state) or 0):
        state[STATE_KEY] = newest
    return state


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------

def _e(text) -> str:
    return html.escape(str(text or ""), quote=True)


def _who(member) -> str:
    if member is None:
        return ""
    return f"{member.name} MS" + (f" ({member.constituency})" if member.constituency else "")


def _card(op) -> str:
    tag = (f'<span style="background:#FDEDE0;color:#A8501A;font-size:10px;font-weight:700;'
           f'letter-spacing:.6px;padding:2px 6px;margin-right:7px">NRLA</span>'
           f'<span style="background:#E8EEF2;color:{DARK_BLUE};font-size:10px;font-weight:700;'
           f'letter-spacing:.6px;padding:2px 6px;text-transform:uppercase">'
           f'Statement of Opinion</span>')
    meta = " · ".join(_e(x) for x in (
        op.reference, f"tabled {op.tabled:%a %-d %B %Y}" if op.tabled else "",
        f"by {_who(op.tabled_by)}" if op.tabled_by else "") if x)
    points = "".join(
        f'<p style="margin:0;padding:3px 0 0;font-family:{FONT};font-size:13.5px;'
        f'line-height:1.55;color:{OFF_BLACK}">{_e(p)}</p>' for p in op.points)
    n = len(op.supporters)
    support = (f'Signed by {n} other Member{"s" if n != 1 else ""}: '
               + ", ".join(_e(m.name) for m in op.supporters[:12])
               + (" and others" if n > 12 else "") + "."
               if n else "No other Members have signed it yet.")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;width:100%;background:#ffffff;border:1px solid {EDGE}">'
        f'<tr><td style="padding:14px 18px 4px;font-family:{FONT}">{tag}</td></tr>'
        f'<tr><td style="padding:6px 18px 0;font-family:{FONT}">'
        f'<a href="{_e(op.url)}" style="font-size:16px;line-height:1.4;font-weight:700;'
        f'color:{OFF_BLACK};text-decoration:none">{_e(op.title)}</a>'
        f'<div style="font-size:12px;color:{MUTED};padding-top:4px">{meta}</div></td></tr>'
        f'<tr><td style="padding:10px 18px 4px">{points}</td></tr>'
        f'<tr><td style="padding:6px 18px 0;font-family:{FONT};font-size:12.5px;'
        f'color:{MUTED}">{support}</td></tr>'
        f'<tr><td style="padding:10px 18px 14px;font-family:{FONT};font-size:12.5px;'
        f'border-top:1px solid {LINE}"><a href="{_e(op.url)}" style="color:{DARK_BLUE};'
        f'font-weight:600;text-decoration:none">Read it in the Record &rsaquo;</a></td></tr>'
        f'</table><div style="height:16px;line-height:16px;font-size:0">&nbsp;</div>')


def render_opinions(opinions: list, test: bool = False) -> tuple[str, str, int]:
    count = len(opinions)
    if not count:
        return "", "", 0
    first = opinions[0]
    who = f" ({first.tabled_by.name} MS)" if first.tabled_by else ""
    subject = (("TEST — " if test else "")
               + (f"Statement of Opinion: {first.title}{who}" if count == 1 else
                  f"Statements of Opinion: {first.title} (+{count - 1} more)"))
    banner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'border="0" style="border-collapse:collapse;background:#FFF4E5;'
              f'border:1px solid #F3C98B"><tr><td style="padding:12px 16px;font-family:{FONT};'
              f'font-size:13px;line-height:1.5;color:#8A3B06"><b>This is a test.</b> It shows '
              f'the most recent relevant Statement of Opinion, which you may have seen '
              f'already. Real alerts carry no banner and only ever contain something new.'
              f'</td></tr></table><div style="height:16px;line-height:16px;font-size:0">'
              f'&nbsp;</div>') if test else ""
    cards = "".join(_card(op) for op in opinions)
    body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">
  <tr><td bgcolor="{DARK_BLUE}" style="padding:20px 28px 16px;font-family:{FONT};color:#ffffff">
    <div style="font-size:19px;font-weight:700;color:#ffffff">Senedd Statement of Opinion alert</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:4px">
      National Residential Landlords Association</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;font-size:0">&nbsp;</td></tr>
  <tr><td style="padding:20px 28px 0;font-family:{FONT}">{banner}{cards}</td></tr>
  <tr><td style="padding:0 28px 30px;font-family:{FONT}">
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;padding-top:4px">
      Statements of Opinion are the Senedd's equivalent of early day motions:
      tabled by a Member, and signed by others who support them. Only those
      matching the NRLA's relevance rules are alerted. The text is the
      statement's own words, from the Senedd's Record, reproduced under the
      Open Government Licence v3.0.
    </p>
  </td></tr>
</table>
</td></tr></table>"""
    return subject, body, count
