"""The Friday future-business email.

WHAT THIS REPLACES
------------------
Camlas send a weekly "Senedd Future Business" email: what is *scheduled*, not
what has happened. Its structure is the specification for this module, because
it is the structure the directorate already reads:

    Plenary Debates          by date, with the slots
    Oral Questions           by date, with the individual tabled questions
    Senedd Committees        meeting dates and agenda items
    Senedd Consultations     with closing dates
    Welsh Government         consultations, grouped by topic

Reordered here so that the part with a clock on it comes first. A missed
consultation deadline cannot be recovered; a missed debate can at least be read
afterwards.

THE DESIGN DECISION THAT LOOKS LIKE A BUG
-----------------------------------------
**Repeats are not suppressed.** The same open consultation appears every Friday
until it closes. That is deliberate and it is what the supplier does: a
forward-business email is a standing list of what is still live, not a change
log. A digest that showed only changes would answer "what is new?" while the
reader is asking "what is still open?", and the second question is the one with
a deadline attached.

What makes it scannable instead is the **NEW** tag: anything first collected
since last Friday is marked, so the week's additions can be found in ten
seconds without losing the standing list underneath them.

The one thing that IS suppressed is an entirely empty week. If all four sections
are empty — recess, most likely — nothing is posted and no email is sent. A
weekly email that says "nothing this week" every week for six weeks teaches
people to delete it unread, and then the seventh one gets deleted too.

DELIVERY
--------
No SMTP credentials anywhere. The script POSTs `{subject, body, count}` to a
Power Automate flow, and the flow sends the mail from the operator's own
account — the same pattern as the Westminster written-questions tool, which is
already in daily use. See `alerts.post_to_flow`.
"""

from __future__ import annotations

import html
from datetime import date, timedelta

from .models import Item
from .relevance import Taxonomy
from .site import _dedupe_by_url


DARK_BLUE = "#113B54"
ORANGE = "#E96C19"
OFF_WHITE = "#FCFCFC"
OFF_BLACK = "#0F2636"
MUTED = "#5A7286"
CRITICAL = "#A32115"

# How far ahead the diary sections look. Three weeks is the window in which a
# committee's papers and any call for written evidence are normally published,
# so it is the window in which there is still something to do about a meeting.
DIARY_WEEKS = 3

# A consultation with no parsed closing date is still live business, but only
# for as long as it is plausibly open. Past this it is more likely to be a
# stale record than an open door.
UNDATED_CONSULTATION_DAYS = 60


def last_friday(today: date) -> date:
    """The Friday before this one — the boundary for the NEW tag.

    On a Friday this returns seven days ago, not today: the email covers the
    week since the last edition, and an item collected this morning is new.
    """
    days_since_friday = (today.weekday() - 4) % 7
    if days_since_friday == 0:
        days_since_friday = 7
    return today - timedelta(days=days_since_friday)


def _is_new(item: Item, since: date) -> bool:
    collected = getattr(item, "collected_at", None)
    if collected is None:
        return False
    return collected.date() >= since


def select_business(items: list[Item], tax: Taxonomy,
                    today: date | None = None,
                    weeks_ahead: int = DIARY_WEEKS) -> dict[str, list[Item]]:
    """Split the archive into the four forward-business sections.

    Filtered by exactly the same strict rule as the live page
    (`Taxonomy.qualifies_for_site`), because the operator's standing
    instruction is that the tool must show what is relevant to the NRLA rather
    than everything it can see: *"otherwise you're just overloaded with
    information"*. Two different definitions of "relevant" in one system would
    also mean the email and the page could disagree, which is worse than either
    being wrong on its own.
    """
    today = today or date.today()
    horizon = today + timedelta(weeks=weeks_ahead)
    # Same strict rule AND the same de-duplication as the page. A consultation
    # whose wording changes between runs is stored again under a new uid — by
    # design, so the archive keeps the history — and without this the committee
    # priorities consultation appeared twice in the email, identically, one
    # above the other. Sharing the page's helper rather than writing a second
    # one means the two can never drift apart.
    shown = _dedupe_by_url([i for i in items if tax.qualifies_for_site(i)])

    consultations = [
        i for i in shown
        if i.source_kind == "consultation"
        and i.deadline and i.deadline >= today
    ]
    undated = [
        i for i in shown
        if i.source_kind == "consultation" and not i.deadline
        and i.item_date
        and i.item_date >= today - timedelta(days=UNDATED_CONSULTATION_DAYS)
    ]
    consultations.sort(key=lambda i: i.deadline)
    undated.sort(key=lambda i: i.item_date, reverse=True)

    diary = [i for i in shown
             if i.source_kind == "calendar"
             and i.item_date and today <= i.item_date <= horizon]
    # Plenary comes from the same ModernGov forward look as the committees —
    # it is one of the forums that service returns — so it needs no collector
    # of its own. It is empty during recess, which is correct and is why this
    # section can look broken in August and is not.
    committees = sorted((i for i in diary if "plenary" not in (i.forum or "").lower()),
                        key=lambda i: i.item_date)
    plenary = sorted((i for i in diary if "plenary" in (i.forum or "").lower()),
                     key=lambda i: i.item_date)

    # Oral questions tabled but not yet asked. The Record's search results carry
    # both a tabled date and the date an answer is due — for an oral question
    # that second date IS the sitting it is down for — and the collector puts it
    # in `deadline` and leaves it unset once the question has been answered. So
    # a future deadline on an oral question means exactly "tabled for a sitting
    # that has not happened yet", which is what Camlas list and what this
    # section needs. No new collector was required; if that ever changes, this
    # section will empty out while the others stay full, which is visible.
    oral = sorted((i for i in shown
                   if i.source_kind == "oral_question"
                   and i.deadline and i.deadline >= today),
                  key=lambda i: i.deadline)

    return {
        "consultations": consultations + undated,
        "committees": committees,
        "plenary": plenary,
        "oral": oral,
    }


def _e(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _display_date(value: date | None) -> str:
    return value.strftime("%-d %b %Y") if value else "—"


def _countdown(deadline: date | None, today: date) -> tuple[str, str]:
    if not deadline:
        return "closing date not published", MUTED
    days = (deadline - today).days
    if days <= 0:
        return "closes today", CRITICAL
    if days == 1:
        return "1 day left", CRITICAL
    if days <= 14:
        return f"{days} days left", ORANGE
    return f"{days} days left", MUTED


def _new_tag() -> str:
    return (f'<span style="display:inline-block;background:{ORANGE};color:#fff;'
            f'font-size:10.5px;font-weight:700;letter-spacing:.5px;'
            f'border-radius:3px;padding:1px 6px;margin-right:7px;'
            f'vertical-align:middle">NEW</span>')


def _row(item: Item, right: str, right_colour: str, is_new: bool,
         sub: str = "") -> str:
    title = _e(item.title or "(untitled)")
    link = (f'<a href="{_e(item.url)}" style="color:{DARK_BLUE};'
            f'text-decoration:none">{title}</a>') if item.url else title
    meta = " · ".join(filter(None, [_e(item.forum or item.source_name or ""),
                                    _e(sub)]))
    return (
        f'<tr><td style="padding:11px 14px;border-bottom:1px solid #E7EDF2;'
        f'vertical-align:top">'
        f'<div style="font-size:14.5px;line-height:1.45;color:{OFF_BLACK}">'
        f'{_new_tag() if is_new else ""}{link}</div>'
        f'<div style="font-size:12px;color:{MUTED};margin-top:3px">{meta}</div>'
        f'</td>'
        f'<td style="padding:11px 14px;border-bottom:1px solid #E7EDF2;'
        f'text-align:right;white-space:nowrap;vertical-align:top;'
        f'font-size:12.5px;font-weight:700;color:{right_colour}">{_e(right)}</td>'
        f'</tr>')


def _section(title: str, lede: str, rows: list[str]) -> str:
    if not rows:
        return ""
    return (
        f'<h2 style="font-size:15px;color:{OFF_BLACK};margin:26px 0 2px">'
        f'{_e(title)} <span style="color:{MUTED};font-weight:400">'
        f'({len(rows)})</span></h2>'
        f'<div style="font-size:12.5px;color:{MUTED};margin-bottom:9px">'
        f'{_e(lede)}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'width="100%" style="border-collapse:collapse;background:#fff;'
        f'border:1px solid #E7EDF2;border-radius:8px">'
        + "".join(rows) + '</table>')


def render_forward(sections: dict[str, list[Item]], tax: Taxonomy,
                   today: date | None = None,
                   new_since: date | None = None,
                   page_url: str = "") -> tuple[str, str, int]:
    """Return ``(subject, html_body, count)``.

    A count of zero means send nothing at all — see the module docstring.
    """
    today = today or date.today()
    new_since = new_since or last_friday(today)

    count = sum(len(v) for v in sections.values())
    n_new = sum(1 for v in sections.values() for i in v if _is_new(i, new_since))

    cons_rows = [
        _row(i, *_countdown(i.deadline, today), _is_new(i, new_since),
             sub=(f"closes {_display_date(i.deadline)}" if i.deadline
                  else "check the source for the closing date"))
        for i in sections["consultations"]]

    com_rows = [
        _row(i, _display_date(i.item_date), MUTED, _is_new(i, new_since))
        for i in sections["committees"]]

    plen_rows = [
        _row(i, _display_date(i.item_date), MUTED, _is_new(i, new_since))
        for i in sections["plenary"]]

    oral_rows = [
        _row(i, _display_date(i.deadline), MUTED, _is_new(i, new_since),
             sub=" ".join(filter(None, [i.speaker, f"({i.constituency})"
                                        if i.constituency else ""])))
        for i in sections["oral"]]

    body = "".join([
        _section("Consultations closing soonest",
                 "Senedd and Welsh Government. Soonest first — a missed "
                 "deadline cannot be recovered.", cons_rows),
        _section(f"Committee meetings in the next {DIARY_WEEKS} weeks",
                 "Papers and any call for written evidence are normally "
                 "published in the two weeks beforehand.", com_rows),
        _section("Plenary business",
                 "Chamber business scheduled on NRLA issues.", plen_rows),
        _section("Oral questions tabled for forthcoming sittings",
                 "Tabled but not yet asked.", oral_rows),
    ])

    subject = (f"Senedd future business — {today.strftime('%-d %B %Y')} "
               f"({count} item{'s' if count != 1 else ''}"
               + (f", {n_new} new)" if n_new else ")"))

    footer_link = (
        f'<p style="font-size:12.5px;color:{MUTED};margin:22px 0 0">'
        f'Everything here is also on the '
        f'<a href="{_e(page_url)}" style="color:{DARK_BLUE}">live page</a>, '
        f'with the full archive behind it.</p>') if page_url else ""

    html_body = f"""<div style="margin:0;padding:0;background:{OFF_WHITE}">
<div style="max-width:720px;margin:0 auto;padding:0 0 34px;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">

  <div style="background:{DARK_BLUE};color:#fff;padding:18px 22px">
    <div style="font-size:18px;font-weight:700;letter-spacing:-.2px">
      Senedd future business</div>
    <div style="font-size:13px;opacity:.85;margin-top:3px">
      National Residential Landlords Association · week of
      {_e(today.strftime('%-d %B %Y'))}</div>
  </div>
  <div style="height:4px;background:{ORANGE}"></div>

  <div style="padding:4px 22px 0">
    <p style="font-size:13.5px;color:{OFF_BLACK};line-height:1.55;margin:18px 0 0">
      What is scheduled or still open, filtered to NRLA relevance.
      {'<b>' + str(n_new) + ' item' + ('s' if n_new != 1 else '') + ' new since '
       + _e(_display_date(new_since)) + '.</b>' if n_new else
       'Nothing new since ' + _e(_display_date(new_since)) + '.'}
      Items stay listed until they close or happen, so this is the standing
      list rather than a change log.
    </p>
    {body}
    {footer_link}
    <p style="font-size:11.5px;color:{MUTED};margin:20px 0 0;line-height:1.5">
      Senedd Cymru and Welsh Government material is reproduced under the Open
      Government Licence v3.0. Nothing in this email is summarised by a
      language model — every title and date is the published record.
      Written questions are deliberately excluded; the team's dedicated tool
      tracks those.
    </p>
  </div>
</div>
</div>"""

    return subject, html_body, count
