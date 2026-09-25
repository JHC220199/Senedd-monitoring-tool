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

Ordered here so that the week's *new* business comes first and the standing
list comes last: oral questions, committee meetings, Plenary, then open
consultations. Consultations led the first edition, on the reasoning that a
missed deadline cannot be recovered — but they are also the section that
changes least from one Friday to the next, so leading with them buried the
new business under eight unchanged entries and made the email read as a list
rather than a briefing.

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
    # Judged by today's taxonomy, not the one in force when each item was
    # collected. The archive keeps the score from collection time, so a
    # notice collected before a rule was tightened — "Have your say on new
    # powers to tackle roadside rubbish", 22 September 2026 — kept its old
    # verdict and went on appearing after the rule said otherwise.
    from .relevance import Scorer
    scorer = Scorer(tax)
    for item in items:
        scorer.score_item(item)
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


# Every layout decision below is made for Outlook on Windows, because that is
# where this is read. Outlook renders HTML with Word's engine, which ignores
# `max-width` on a <div>, ignores flexbox, and treats margins unpredictably.
# The first version centred a 720px-wide <div>; in Outlook that width was
# discarded, the email stretched to the full window, and every row became a
# line of text with a date stranded on the far right. It looked fine on a
# phone — where Outlook uses a real browser engine — and clunky on a laptop,
# which is the wrong way round for who reads it.
#
# So: tables for structure, fixed pixel widths, padding on <td> rather than
# margins on <div>, and explicit font-family on every cell that carries text.
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,"
        "sans-serif")
WIDTH = 680
RIGHT_COL = 132
LINE = "#EAF0F4"
EDGE = "#E3EAEF"


def _new_tag() -> str:
    return ('<span style="background:#FDEDE0;color:#A8501A;font-size:10px;'
            'font-weight:700;letter-spacing:.6px;padding:2px 6px;'
            'margin-right:8px">NEW</span>')


def _row(primary: str, meta: str, right: str, right_note: str,
         right_colour: str, is_new: bool, url: str = "") -> str:
    """One line of business.

    `primary` is what the reader is actually being asked to look at, and the
    caller decides what that is — which sounds obvious and was not. Every row
    used to print `item.title`, and for an oral question the title is its
    reference number, so the first email said:

        NEW  OQ64467
             Plenary · David Hughes (Pontypridd Cynon Merthyr)

    A reference number is not business. The question was about protecting
    renters and the email never said so.
    """
    body = (f'<a href="{_e(url)}" style="color:{DARK_BLUE};'
            f'text-decoration:none">{_e(primary)}</a>') if url else _e(primary)
    cell = (f'padding:14px 18px;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};vertical-align:top;')
    return (
        f'<tr><td style="{cell}">'
        f'<div style="font-size:15px;line-height:1.5;color:{OFF_BLACK}">'
        f'{_new_tag() if is_new else ""}{body}</div>'
        + (f'<div style="font-size:12px;line-height:1.5;color:{MUTED};'
           f'padding-top:5px">{_e(meta)}</div>' if meta else "")
        + f'</td>'
        f'<td width="{RIGHT_COL}" style="{cell}width:{RIGHT_COL}px;'
        f'text-align:right">'
        f'<div style="font-size:13px;font-weight:700;color:{OFF_BLACK};'
        f'white-space:nowrap">{_e(right)}</div>'
        + (f'<div style="font-size:11.5px;font-weight:600;'
           f'color:{right_colour};padding-top:3px;white-space:nowrap">'
           f'{_e(right_note)}</div>' if right_note else "")
        + '</td></tr>')


def _section(title: str, lede: str, rows: list[str]) -> str:
    if not rows:
        return ""
    # The last row keeps no rule, so the card closes on its own border.
    body = "".join(rows[:-1]) + rows[-1].replace(
        f"border-bottom:1px solid {LINE};", "")
    return (
        f'<tr><td style="padding:28px 28px 0;font-family:{FONT}">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:1.2px;'
        f'text-transform:uppercase;color:{ORANGE}">{_e(title)}'
        f'<span style="color:{MUTED};font-weight:600">&nbsp;&nbsp;{len(rows)}'
        f'</span></div>'
        f'<div style="font-size:12.5px;color:{MUTED};line-height:1.5;'
        f'padding:6px 0 11px">{_e(lede)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" style="border-collapse:collapse;'
        f'width:100%;background:#ffffff;border:1px solid {EDGE}">'
        f'{body}</table></td></tr>')


def _sitting(value: date | None) -> str:
    """"Tue 15 Sep" — the day matters as much as the date for a sitting."""
    return value.strftime("%a %-d %b") if value else "—"


def render_forward(sections: dict[str, list[Item]], tax: Taxonomy,
                   today: date | None = None,
                   new_since: date | None = None,
                   page_url: str = "",
                   review: tuple[str, int] | None = None) -> tuple[str, str, int]:
    """Return ``(subject, html_body, count)``.

    A count of zero means send nothing at all — see the module docstring.

    ``review`` is the week in review (monitor/weekly_review.py): its email
    rows and its count. When the Senedd sat, it goes first and the email
    becomes the weekly briefing; otherwise this is the future-business email
    as it always was.
    """
    review_block, review_count = review or ("", 0)
    today = today or date.today()
    new_since = new_since or last_friday(today)

    count = sum(len(v) for v in sections.values())
    n_new = sum(1 for v in sections.values() for i in v if _is_new(i, new_since))

    # The question itself is the point. The reference, the Member and the
    # minister answering are how a reader knows whose question it is and which
    # session to watch — all three were missing from the first edition.
    oral_rows = []
    for i in sections["oral"]:
        who = i.speaker + (f" ({i.constituency})" if i.constituency else "")
        oral_rows.append(_row(
            primary=i.body or i.title,
            meta=" · ".join(filter(None, [who, i.title, i.agenda_item])),
            right=_sitting(i.deadline), right_note="for answer",
            right_colour=MUTED,
            is_new=_is_new(i, new_since), url=i.url))

    # The committee title already carries the committee's name and the date, so
    # repeating the forum underneath it said the same thing twice.
    com_rows = [
        _row(primary=i.title or "(untitled)",
             meta=i.agenda_item or "",
             right=_sitting(i.item_date), right_note="",
             right_colour=MUTED, is_new=_is_new(i, new_since), url=i.url)
        for i in sections["committees"]]

    plen_rows = [
        _row(primary=i.title or "(untitled)",
             meta=i.forum or "",
             right=_sitting(i.item_date), right_note="",
             right_colour=MUTED, is_new=_is_new(i, new_since), url=i.url)
        for i in sections["plenary"]]

    cons_rows = []
    for i in sections["consultations"]:
        note, colour = _countdown(i.deadline, today)
        cons_rows.append(_row(
            primary=i.title or "(untitled)",
            meta=i.forum or i.source_name or "",
            right=_display_date(i.deadline) if i.deadline else "date not given",
            right_note=note if i.deadline else "check the source",
            right_colour=colour,
            is_new=_is_new(i, new_since), url=i.url))

    # Order set by the operator, 11 September 2026: what is about to be said
    # first, what is open to respond to last. The consultations were first on
    # the reasoning that a missed deadline cannot be recovered — but they are
    # also the section that changes least from week to week, and burying the
    # week's new business under eight standing entries is what made the email
    # feel like a list rather than a briefing.
    body = "".join([
        _section("Oral questions tabled for forthcoming sittings",
                 "Tabled, not yet asked. The minister answering is named "
                 "against each one.", oral_rows),
        _section(f"Committee meetings in the next {DIARY_WEEKS} weeks",
                 "Papers and any call for written evidence are normally "
                 "published in the two weeks beforehand.", com_rows),
        _section("Plenary business",
                 "Debates and statements scheduled on NRLA issues.", plen_rows),
        _section("Consultations closing soonest",
                 "Senedd and Welsh Government. Soonest first — a missed "
                 "deadline cannot be recovered.", cons_rows),
    ])

    subject = (f"Senedd future business — {today.strftime('%-d %B %Y')} "
               f"({count} item{'s' if count != 1 else ''}"
               + (f", {n_new} new)" if n_new else ")"))
    if review_block:
        subject = (f"Senedd weekly briefing — {today.strftime('%-d %B %Y')} "
                   f"({review_count} this week, {count} coming up)")
        body = (review_block
                + f'<tr><td style="padding:30px 28px 0;font-family:{FONT}">'
                  f'<div style="font-size:13px;font-weight:700;letter-spacing:1.2px;'
                  f'text-transform:uppercase;color:{ORANGE};border-top:2px solid {EDGE};'
                  f'padding-top:22px">Coming up</div></td></tr>'
                + body)
    title = "Senedd weekly briefing" if review_block else "Senedd future business"

    tally = " · ".join(
        f"{len(rows)} {label}{'' if len(rows) == 1 else 's'}"
        for label, rows in (
            ("oral question", sections["oral"]),
            ("committee meeting", sections["committees"]),
            ("Plenary item", sections["plenary"]),
            ("consultation", sections["consultations"]),
        ) if rows)

    if review_block:
        tally = (f"This week: {review_count} item{'s' if review_count != 1 else ''} on "
                 f"NRLA issues" + (f" · Coming up: {tally}" if tally else ""))

    newness = (f'<b style="color:{ORANGE}">{n_new} new since '
               f'{_e(_display_date(new_since))}.</b> ' if n_new
               else f'Nothing new since {_e(_display_date(new_since))}. ')

    footer_link = (
        f'<p style="font-size:12.5px;color:{MUTED};margin:0;padding-top:22px;'
        f'font-family:{FONT}">Everything here is also on the '
        f'<a href="{_e(page_url)}" style="color:{DARK_BLUE};font-weight:600">'
        f'live page</a>, with the full archive behind it.</p>'
    ) if page_url else ""

    html_body = f"""<table role="presentation" width="100%" cellpadding="0" \
cellspacing="0" border="0" style="border-collapse:collapse;background:{OFF_WHITE}">
<tr><td align="center" style="padding:0">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" \
border="0" style="border-collapse:collapse;width:{WIDTH}px;max-width:{WIDTH}px">

  <tr><td bgcolor="{DARK_BLUE}" style="padding:24px 28px 20px;font-family:{FONT};color:#ffffff">
    <div style="font-size:20px;font-weight:700;letter-spacing:-.2px;color:#ffffff">
      {_e(title)}</div>
    <div style="font-size:13px;color:#C3D2DC;padding-top:5px">
      National Residential Landlords Association &nbsp;·&nbsp; week of
      {_e(today.strftime('%-d %B %Y'))}</div>
  </td></tr>
  <tr><td bgcolor="{ORANGE}" height="4" style="height:4px;line-height:4px;
    font-size:0">&nbsp;</td></tr>

  <tr><td style="padding:22px 28px 0;font-family:{FONT}">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      border="0" style="border-collapse:collapse;background:#ffffff;
      border:1px solid {EDGE}">
      <tr><td style="padding:15px 18px;font-family:{FONT}">
        <div style="font-size:14px;font-weight:700;color:{OFF_BLACK};
          line-height:1.5">{_e(tally) if tally else "Nothing scheduled"}</div>
        <div style="font-size:12.5px;color:{MUTED};line-height:1.55;
          padding-top:7px">{newness}Open items stay listed until they close or
          happen, so this is the standing list rather than a change log.</div>
      </td></tr>
    </table>
  </td></tr>

  {body}

  <tr><td style="padding:0 28px 34px;font-family:{FONT}">
    {footer_link}
    <p style="font-size:11.5px;color:{MUTED};line-height:1.55;margin:0;
      padding-top:16px;font-family:{FONT}">
      Senedd Cymru and Welsh Government material is reproduced under the Open
      Government Licence v3.0. Nothing in this email is summarised by a
      language model — every question, title and date is the published record.
      Written questions are deliberately excluded; the team's dedicated tool
      tracks those.
    </p>
  </td></tr>

</table>
</td></tr></table>"""

    return subject, html_body, count + review_count
