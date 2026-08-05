"""Email alerts and digests.

Two channels, deliberately different in character:

IMMEDIATE ALERT
    Fires within the hour for Critical items only. Short, one item per alert
    where possible, and it always says what the item is and why it escalated.
    An alert nobody can act on immediately is just a notification, so these are
    rationed hard — if everything is urgent, nothing is.

DAILY / WEEKLY DIGEST
    Everything at High and Medium, grouped by policy area, with the closing-soon
    list at the top because that is the part with a clock on it.

Both are plain HTML with inlined styles — Outlook's rendering engine does not
support external stylesheets or most modern CSS, so the markup here is
deliberately old-fashioned (tables for layout would be even safer, but divs with
inline styles render correctly in current Outlook and read far better on a
phone).

Nothing in either email is generated text. Every quoted word is verbatim from
the published Senedd or Welsh Government source, with a link back. This is a
deliberate design constraint: a policy team briefing a chief executive or a
committee cannot afford a paraphrase that drifts, and an automated system should
never put words in a minister's mouth.
"""

from __future__ import annotations

import html
import smtplib
from collections import defaultdict
from datetime import date, datetime
from email.message import EmailMessage

from .models import Item
from .relevance import Taxonomy


DARK_BLUE = "#113B54"
ORANGE = "#E96C19"
OFF_WHITE = "#FCFCFC"
OFF_BLACK = "#0F2636"
MUTED = "#5A7286"
CRITICAL = "#A32115"


def _band_colour(band: str, tax: Taxonomy) -> str:
    for b in tax.bands:
        if b["name"] == band:
            return b.get("colour", MUTED)
    return MUTED


# ---------------------------------------------------------------------------
# Immediate alert
# ---------------------------------------------------------------------------

def render_alert(items: list[Item], tax: Taxonomy) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for a Critical alert."""
    if not items:
        return "", "", ""

    lead = items[0]
    if len(items) == 1:
        subject = f"Senedd alert: {lead.title[:90]}"
    else:
        subject = f"Senedd alert: {len(items)} items need attention"

    # If the NRLA has been named, say so in the subject. That is the single
    # thing most likely to need a same-day response.
    if any(i.force_alert for i in items):
        subject = f"Senedd alert: the NRLA has been mentioned — {len(items)} item(s)"

    blocks = []
    text_lines = []
    for item in items:
        why = ", ".join(filter(None, [
            "; ".join(tax.theme_label(t) for t in item.themes[:3]),
            "; ".join(item.signals[:2]),
            "; ".join(item.entities[:2]),
        ]))
        links = []
        if item.url:
            links.append(f'<a href="{html.escape(item.url)}" '
                         f'style="color:{DARK_BLUE};">Open the source</a>')
        if item.video_url:
            links.append(f'<a href="{html.escape(item.video_url)}" '
                         f'style="color:{DARK_BLUE};">Watch this moment</a>')
        deadline = ""
        if item.deadline:
            days = (item.deadline - date.today()).days
            deadline = (f'<p style="margin:6px 0;color:{CRITICAL};font-weight:600;">'
                        f'Closes {item.deadline.strftime("%d %B %Y")} — '
                        f'{days} day(s) left.</p>')

        blocks.append(f"""
<div style="border-left:4px solid {_band_colour(item.band, tax)};
            background:{OFF_WHITE};padding:14px 16px;margin:0 0 14px;
            border-radius:8px;">
  <p style="margin:0 0 4px;font-size:12px;text-transform:uppercase;
            letter-spacing:.05em;color:{MUTED};font-weight:700;">
     {html.escape(item.band)} · {html.escape(item.source_name)}</p>
  <p style="margin:0 0 6px;font-size:16px;font-weight:650;color:{OFF_BLACK};">
     {html.escape(item.title)}</p>
  <p style="margin:0 0 8px;font-size:13px;color:{MUTED};">
     {html.escape(' · '.join(filter(None, [item.speaker, item.speaker_role,
                                           item.forum,
                                           item.item_date.strftime('%d %B %Y')
                                           if item.item_date else ''])))}</p>
  {deadline}
  <p style="margin:0 0 10px;font-size:14px;color:{OFF_BLACK};">
     {html.escape(item.excerpt)}</p>
  <p style="margin:0 0 6px;font-size:12px;color:{MUTED};">
     Flagged because: {html.escape(why)}</p>
  <p style="margin:0;font-size:14px;">{' &nbsp;·&nbsp; '.join(links)}</p>
</div>""")

        text_lines.append(
            f"[{item.band}] {item.title}\n"
            f"  {' · '.join(filter(None, [item.speaker, item.forum]))}\n"
            f"  {item.excerpt}\n"
            f"  Why: {why}\n"
            f"  {item.url}\n"
        )

    html_body = f"""<html><body style="margin:0;padding:0;background:#F6F7F8;">
<div style="max-width:660px;margin:0 auto;padding:22px;
            font-family:-apple-system,'Segoe UI',Arial,sans-serif;">
  <div style="background:{DARK_BLUE};color:{OFF_WHITE};padding:18px 20px;
              border-radius:8px 8px 0 0;">
    <p style="margin:0;font-size:18px;font-weight:650;">Senedd policy alert</p>
    <p style="margin:4px 0 0;font-size:13px;opacity:.85;">
       {len(items)} item(s) crossed the Critical threshold ·
       {datetime.now().strftime('%d %B %Y at %H.%M')}</p>
  </div>
  <div style="background:#F6F7F8;padding:18px 4px;">
    {''.join(blocks)}
  </div>
  <p style="font-size:12px;color:{MUTED};margin:8px 0 0;">
     Sent by the NRLA Senedd monitor. Every quotation above is the verbatim
     published text from the Senedd or the Welsh Government. Adjust what
     triggers an alert in <code>config/taxonomy.yaml</code>.</p>
</div></body></html>"""

    text_body = ("Senedd policy alert\n"
                 f"{len(items)} item(s) crossed the Critical threshold\n"
                 f"{datetime.now().strftime('%d %B %Y at %H.%M')}\n\n"
                 + "\n".join(text_lines))

    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def render_digest(items: list[Item], deadlines: list[Item], tax: Taxonomy,
                  period_label: str = "the last seven days",
                  dashboard_url: str = "") -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for the periodic digest."""
    critical = [i for i in items if i.band == "Critical"]
    high = [i for i in items if i.band == "High"]

    if critical:
        subject = (f"Senedd digest: {len(critical)} critical, "
                   f"{len(high)} high priority")
    elif high:
        subject = f"Senedd digest: {len(high)} high priority items"
    elif items:
        subject = f"Senedd digest: {len(items)} items to note"
    else:
        subject = "Senedd digest: nothing to report"

    # Closing soon comes first. It is the only section with a hard clock.
    deadline_rows = ""
    if deadlines:
        rows = []
        for item in deadlines[:12]:
            days = (item.deadline - date.today()).days if item.deadline else None
            urgent = days is not None and days <= 7
            rows.append(f"""
<tr>
  <td style="padding:7px 10px 7px 0;font-weight:650;white-space:nowrap;
             color:{CRITICAL if urgent else OFF_BLACK};font-size:14px;">
     {item.deadline.strftime('%d %b %Y') if item.deadline else ''}</td>
  <td style="padding:7px 0;font-size:14px;">
     {f'<a href="{html.escape(item.url)}" style="color:{DARK_BLUE};">' if item.url else ''}
     {html.escape(item.title)}{'</a>' if item.url else ''}
     <span style="color:{MUTED};font-size:12px;">
       — {html.escape(item.source_name)}{f', {days} days left' if days is not None else ''}</span></td>
</tr>""")
        deadline_rows = f"""
<h2 style="font-size:16px;color:{DARK_BLUE};margin:22px 0 6px;">Closing soon</h2>
<p style="font-size:13px;color:{MUTED};margin:0 0 8px;">
   Windows that are still open. Act on these first.</p>
<table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>"""

    # Group the rest by policy tier so the digest reads like a briefing rather
    # than a list.
    grouped: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        for tier in (item.tiers or ["Other"]):
            grouped[tier].append(item)

    tier_order = ["Private rented sector", "Property & energy", "Tax & finance",
                  "Planning & place", "Housing system", "Context", "Other"]
    ordered = sorted(grouped.items(),
                     key=lambda kv: (tier_order.index(kv[0])
                                     if kv[0] in tier_order else 99))

    sections = []
    text_sections = []
    for tier, tier_items in ordered:
        tier_items = sorted(tier_items, key=lambda i: -i.score)[:10]
        cards = []
        for item in tier_items:
            links = []
            if item.url:
                links.append(f'<a href="{html.escape(item.url)}" '
                             f'style="color:{DARK_BLUE};">Source</a>')
            if item.video_url:
                links.append(f'<a href="{html.escape(item.video_url)}" '
                             f'style="color:{DARK_BLUE};">Watch</a>')
            cards.append(f"""
<div style="border-left:3px solid {_band_colour(item.band, tax)};
            background:{OFF_WHITE};padding:11px 14px;margin:0 0 10px;
            border-radius:6px;">
  <p style="margin:0 0 3px;font-size:11px;text-transform:uppercase;
            letter-spacing:.05em;color:{MUTED};font-weight:700;">
     {html.escape(item.band)} · {html.escape(item.source_name)}</p>
  <p style="margin:0 0 4px;font-size:15px;font-weight:620;color:{OFF_BLACK};">
     {html.escape(item.title)}</p>
  <p style="margin:0 0 6px;font-size:12px;color:{MUTED};">
     {html.escape(' · '.join(filter(None, [item.speaker, item.speaker_role,
        item.item_date.strftime('%d %B %Y') if item.item_date else ''])))}</p>
  <p style="margin:0 0 7px;font-size:13.5px;color:{OFF_BLACK};">
     {html.escape(item.excerpt)}</p>
  <p style="margin:0;font-size:13px;">{' &nbsp;·&nbsp; '.join(links)}</p>
</div>""")
        sections.append(
            f'<h2 style="font-size:16px;color:{DARK_BLUE};margin:24px 0 8px;">'
            f'{html.escape(tier)} '
            f'<span style="font-weight:400;font-size:13px;color:{MUTED};">'
            f'({len(grouped[tier])})</span></h2>' + "".join(cards))

        text_sections.append(
            f"\n== {tier} ==\n" + "\n".join(
                f"[{i.band}] {i.title}\n    {i.excerpt}\n    {i.url}"
                for i in tier_items))

    body_content = (deadline_rows + "".join(sections)) if items or deadlines else f"""
<div style="background:{OFF_WHITE};padding:26px;border-radius:8px;
            text-align:center;color:{MUTED};">
  <p style="margin:0;font-size:15px;">No relevant Senedd or Welsh Government
     business in {html.escape(period_label)}.</p>
  <p style="margin:8px 0 0;font-size:13px;">All sources ran successfully — this
     is a genuinely quiet period, not a broken feed.</p>
</div>"""

    dash_link = ""
    if dashboard_url:
        dash_link = (f'<p style="margin:16px 0 0;font-size:14px;">'
                     f'<a href="{html.escape(dashboard_url)}" '
                     f'style="background:{ORANGE};color:{OFF_WHITE};'
                     f'padding:10px 18px;border-radius:6px;'
                     f'text-decoration:none;font-weight:650;">'
                     f'Open the full dashboard</a></p>')

    html_body = f"""<html><body style="margin:0;padding:0;background:#F6F7F8;">
<div style="max-width:680px;margin:0 auto;padding:22px;
            font-family:-apple-system,'Segoe UI',Arial,sans-serif;">
  <div style="background:{DARK_BLUE};color:{OFF_WHITE};padding:20px;
              border-radius:8px 8px 0 0;">
    <p style="margin:0;font-size:19px;font-weight:650;">Senedd policy digest</p>
    <p style="margin:5px 0 0;font-size:13px;opacity:.85;">
       Covering {html.escape(period_label)} ·
       {datetime.now().strftime('%d %B %Y')}</p>
  </div>
  <div style="padding:4px 0;">
    {body_content}
    {dash_link}
  </div>
  <p style="font-size:12px;color:{MUTED};margin:20px 0 0;
            border-top:1px solid #DCE2E7;padding-top:12px;">
     Sent by the NRLA Senedd monitor. Quotations are verbatim published text
     from Senedd Cymru and the Welsh Government, reproduced under the Open
     Government Licence v3.0. Tune what appears here in
     <code>config/taxonomy.yaml</code>.</p>
</div></body></html>"""

    text_body = (f"Senedd policy digest — covering {period_label}\n"
                 f"{datetime.now().strftime('%d %B %Y')}\n"
                 + ("\n-- Closing soon --\n" + "\n".join(
                     f"{i.deadline.strftime('%d %b %Y') if i.deadline else ''}  "
                     f"{i.title} ({i.source_name})\n    {i.url}"
                     for i in deadlines[:12]) if deadlines else "")
                 + "".join(text_sections))

    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send(subject: str, html_body: str, text_body: str,
         sender: str, recipients: list[str],
         smtp_host: str = "", smtp_port: int = 587,
         username: str = "", password: str = "",
         use_tls: bool = True, dry_run: bool = True) -> bool:
    """Send one message. `dry_run=True` by default, on purpose.

    Nothing should ever be emailed to a distribution list because a script was
    run with the wrong argument. Sending requires an explicit `--send`.
    """
    if not (subject and recipients):
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text_body or "See the HTML version of this message.")
    message.add_alternative(html_body, subtype="html")

    if dry_run or not smtp_host:
        return False

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
    return True
