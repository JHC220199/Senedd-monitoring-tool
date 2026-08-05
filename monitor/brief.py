"""The briefing as markdown, for a place a person will actually look.

WHY THIS EXISTS
---------------
The first GitHub Actions workflow ran successfully, collected 257 items, and
committed them — and the operator's reasonable summary of the experience was
"I have received no email and no dashboard has been produced so I'm unsure
exactly what the point of the code I uploaded is."

That was a fair verdict on a real design failure, and it had two halves:

1. The dashboard was written to `out/index.html`, which is gitignored, and then
   uploaded as a build artifact. So the output existed but was a zip file behind
   three clicks at the bottom of a log page. Nobody is going to do that daily.
2. The email is a dry run unless SMTP secrets are set — correctly, so a
   half-configured repo cannot mail a distribution list — but the step still went
   green and said nothing loud. A silent green is the exact failure mode this
   project keeps warning about: it looks like it worked, and it hasn't.

GitHub renders markdown written to `$GITHUB_STEP_SUMMARY` directly on the run
page. So the briefing goes *there*: click the run, read the work. No download, no
email configuration, no server, no Pages, and it works on a private repository.

The zone split is imported from `dashboard.zones` rather than reimplemented, so
this cannot drift from the HTML dashboard.
"""

from __future__ import annotations

from datetime import date, datetime

from .dashboard import zones
from .models import Item
from .pipeline import RunReport
from .relevance import Taxonomy

# How many entries each section shows. The job summary has a 1 MB limit, and
# more importantly a briefing that scrolls forever is the problem this project
# was hired to solve, not a feature.
LIMITS = {"respond": 12, "review": 15, "coming": 10, "undated": 8}


def _link(text: str, url: str) -> str:
    """A markdown link, with pipes escaped so a title cannot break a table."""
    clean = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    return f"[{clean}]({url})" if url else clean


def render_markdown(items: list[Item], tax: Taxonomy,
                    report: RunReport | None = None,
                    upcoming: list[Item] | None = None,
                    today: date | None = None,
                    dashboard_note: str = "",
                    heading: str = "") -> str:
    """The whole briefing as GitHub-flavoured markdown.

    `heading` is set when the output is a standalone page (BRIEFING.md) rather
    than a section of the Actions run summary, which supplies its own title.
    """
    z = zones(items, tax, upcoming)
    today = today or date.today()
    out: list[str] = []

    if heading:
        out.append(f"# {heading}")
        out.append("")
        out.append(f"*As at {today.strftime('%A %d %B %Y')}. Rebuilt "
                   "automatically on every run — there is nothing to refresh.*")
        out.append("")

    # --- health first ----------------------------------------------------
    # If a source failed, that belongs at the top. A briefing that is quietly
    # missing a source reads exactly like a quiet week.
    failed = list(report.sources_failed) if report else []
    substituted = list(getattr(report, "sources_substituted", [])) if report else []
    if failed:
        out.append("> [!WARNING]")
        out.append("> **This view is incomplete.** These sources returned "
                   "nothing, so treat gaps below with suspicion: "
                   + ", ".join(f"`{f}`" for f in failed))
        out.append("")
    if substituted:
        out.append("> [!NOTE]")
        out.append("> Collected another way (nothing missing): "
                   + ", ".join(f"`{s}`" for s in substituted))
        out.append("")

    # --- headline --------------------------------------------------------
    bits = []
    if z["respond"]:
        bits.append(f"**{len(z['respond'])}** open "
                    f"{'consultation' if len(z['respond']) == 1 else 'consultations'}")
    if z["closing_soon"]:
        bits.append(f"**{z['closing_soon']}** closing within three weeks")
    if z["review"]:
        bits.append(f"**{len(z['review'])}** developments to review")
    out.append(" · ".join(bits) if bits
               else "**Nothing above the reporting threshold.** "
                    "A genuinely quiet period, not a failed run.")
    out.append("")

    # --- RESPOND ---------------------------------------------------------
    if z["respond"]:
        out.append("## Respond — things with a deadline")
        out.append("")
        out.append("| Closes | What | Suggested next step |")
        out.append("|---|---|---|")
        for p in z["respond"][:LIMITS["respond"]]:
            urgency = p["urgency"]
            if p["severity"] in ("now", "closed"):
                urgency = f"**{urgency}**"
            out.append(f"| {urgency} | {_link(p['title'], p['url'])}"
                       f"<br><sub>{p['source_name'] or ''}</sub> "
                       f"| {p['action']} |")
        if len(z["respond"]) > LIMITS["respond"]:
            out.append(f"| … | _{len(z['respond']) - LIMITS['respond']} more — "
                       f"see the full dashboard_ | |")
        out.append("")

    if z["undated"]:
        out.append("<details><summary>Open, but no closing date published yet "
                   f"({len(z['undated'])})</summary>")
        out.append("")
        for p in z["undated"][:LIMITS["undated"]]:
            out.append(f"- {_link(p['title'], p['url'])} — "
                       f"{p['source_name'] or 'Senedd'}")
        out.append("")
        out.append("</details>")
        out.append("")

    # --- REVIEW ----------------------------------------------------------
    if z["review"]:
        out.append("## Review — what has happened")
        out.append("")
        for p in z["review"][:LIMITS["review"]]:
            head = _link(p["title"], p["url"])
            out.append(f"**{head}**  ")
            meta = [p["date_display"], p["forum"] or p["source_name"] or ""]
            if p["who"]:
                meta.append(p["who"])
            out.append("<sub>" + " · ".join(m for m in meta if m) + "</sub>  ")
            if p["why"]:
                out.append(f"{p['why']} {p['action']}  ")
            if p["excerpt"]:
                excerpt = p["excerpt"].strip().replace("\n", " ")
                if len(excerpt) > 400:
                    excerpt = excerpt[:400].rsplit(" ", 1)[0] + "…"
                # Verbatim published text. Nothing here is ever summarised by a
                # language model; the quote is the record itself.
                out.append(f"> {excerpt}")
            if p["video"]:
                out.append(f"<sub>[Watch this moment]({p['video']})</sub>")
            out.append("")
        if len(z["review"]) > LIMITS["review"]:
            out.append(f"_{len(z['review']) - LIMITS['review']} further items "
                       f"in the full dashboard._")
            out.append("")

    # --- COMING UP -------------------------------------------------------
    if z["coming"]:
        out.append("## Coming up")
        out.append("")
        for p in z["coming"][:LIMITS["coming"]]:
            when = p["deadline_display"] or p["date_display"]
            out.append(f"- **{when}** — {_link(p['title'], p['url'])}")
        out.append("")

    # --- provenance ------------------------------------------------------
    out.append("---")
    out.append("")
    out.append(f"<sub>{len(z['payload'])} items scored from "
               f"{len(items)} collected records, "
               f"{today.strftime('%d %B %Y')}. "
               "Every quotation is verbatim published text — nothing on this "
               "page is summarised by a language model. "
               "Senedd Cymru and Welsh Government material reproduced under the "
               "[Open Government Licence v3.0]"
               "(https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)."
               "</sub>")
    if dashboard_note:
        out.append("")
        out.append(f"<sub>{dashboard_note}</sub>")

    return "\n".join(out) + "\n"
