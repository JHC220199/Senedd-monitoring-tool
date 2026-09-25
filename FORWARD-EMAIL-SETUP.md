# Turning on the Friday future-business email

Five minutes, once. No Azure, no IT ticket, no password stored anywhere.

This is the same arrangement the Westminster written-questions tool already
runs on, so if you set that one up, this will look familiar — and if you have a
flow for it, **do not reuse that one**: it would send Senedd business to
whatever the Westminster flow is wired to. Make a second one.

---

## What the email is

**In a week the Senedd sat, it opens with the week in review.** This is
Camlas's weekly briefing, cut down to the NRLA's areas. First come the week's
five most important items as **Headlines**. Then everything said and
published on NRLA issues this week, filed under the NRLA's own themes:

- private renting and renting reform;
- Rent Smart Wales, licensing and enforcement;
- building safety and leasehold;
- energy efficiency and retrofit;
- property tax and second homes;
- housing supply, planning and social housing;
- homelessness and tenants' finances.

Each entry is a line and a link: what it was, where and who, and the most
relevant sentence the speaker actually said. The entries come from the
Senedd's draft Record and the Welsh Government's own notices. They are chosen
by the same rules as the live page, the debate summaries and the press
release alerts. Themes with nothing in them are left out. Political changes
reported by the week's news alerts are listed at the end. Committee meetings
whose Record is not yet published are named, so you know what is not
covered. In recess the review is left out, and the email is future business
alone.

The email then has what is coming up, every Friday afternoon:

1. **Consultations closing soonest** — Senedd and Welsh Government, soonest
   first. A missed deadline cannot be recovered, so it goes first.
2. **Committee meetings in the next three weeks** — papers and any call for
   written evidence are normally published in the fortnight beforehand.
3. **Plenary business** scheduled on NRLA issues.
4. **Oral questions tabled for forthcoming sittings** — tabled, not yet asked.

Filtered by the same strict relevance rule as the live page, so it is not a
firehose.

**Things repeat, on purpose.** An open consultation appears every Friday until
it closes. That is what a forward-business list is: what is still live, not
what changed. Anything first seen since last Friday is tagged **NEW**, so the
week's additions are findable in ten seconds.

**An empty week sends nothing at all.** During recess you will hear nothing,
rather than receiving six identical "nothing this week" emails and learning to
delete the seventh unread.

### The Word document attached

In a week the Senedd sat, the email also carries a Word document, *NRLA
Senedd weekly briefing 25 September 2026.docx*. It has the same items under
the same themes, with **who said what**, as Camlas's weekly briefing does:

- **Questions and requests** (oral questions, the Business Statement, the
  questions after a statement): every contribution, the question and the
  reply, in full.
- **Statements and debates**: each speaker in turn. A short contribution is in
  full. A long one is cut to its passages on NRLA issues, and its opening is
  always kept. **[…]** marks each cut, and every heading links to the full
  Record.
- **Welsh Government press releases and written statements**: the notice's
  own summary points and opening paragraphs.
- Then the week's political changes and everything coming up, so the
  document can be forwarded on its own.

Nothing is summarised or paraphrased. Every word is the speaker's own, from
the Senedd's Record of Proceedings, in English as spoken or in the official
interpretation where the words were spoken in Welsh. The run also keeps a
copy of the document with the email (see "Keep a copy of what was sent").

**The flow change that makes the attachment work (done 25 September 2026).**
The tool sends the document as an extra `attachments` field. In the flow's
**Send an email (V2)** step:

1. Open **Advanced parameters** and tick **Attachments**.
2. Click the **switch to input entire array** icon next to Attachments.
3. Put this in as an **expression** (the *fx* button), then **Save**:

   ```
   coalesce(triggerBody()?['attachments'], json('[]'))
   ```

The morning, debate and news emails send no `attachments` field, so for them
this expression gives an empty list and nothing changes. If the attachment
ever stops arriving while the email still comes, this step is the one to
check.

---

## Stage 1 — make the flow (5 minutes)

1. Go to <https://make.powerautomate.com> and sign in with your NRLA account.
2. **Create → Instant cloud flow.**
3. Name it something you will recognise in a year — `Senedd future business`.
4. Choose the trigger **When an HTTP request is received**, then **Create**.
5. Open the trigger and click **Use sample payload to generate schema**. Paste
   this in, then **Done**:

   ```json
   {"subject": "text", "body": "text", "count": 1}
   ```

   If your screen shows a schema box instead, paste this:

   ```json
   {"type":"object","properties":{"subject":{"type":"string"},"body":{"type":"string"},"count":{"type":"integer"}}}
   ```

6. **New step → Send an email (V2)** (the Office 365 Outlook action).
   - **To:** `joshua.helm-cowley@nrla.org.uk`
   - **Subject:** click the field, then pick the dynamic content **subject**
   - **Body:** click the **`</>`** button in the corner of the body box first —
     that switches it to HTML mode — then pick the dynamic content **body**.

   The `</>` step is the one people miss. Without it the email arrives as a
   page of visible HTML tags.

7. **Save.** Then reopen the trigger: it now shows an **HTTP POST URL**. Copy
   it.

---

## Stage 2 — give the URL to the tool (2 minutes)

1. Go to <https://github.com/JHC220199/Senedd-monitoring-tool>.
2. **Settings** (the repository's tab, not your account settings) →
   **Secrets and variables → Actions → New repository secret.**
3. Name it exactly `MONITOR_FLOW_URL`, paste the URL in, and save.

That URL is the only secret involved. It is write-only: anyone holding it can
make the flow send *you* an email, and nothing else. It is not a password and
it gives no access to your mailbox. Keep it out of documents and chats all the
same, and if it ever needs changing, regenerate the flow's URL in Power
Automate and update the secret.

---

## Stage 3 — prove it (3 minutes)

1. **Actions → Senedd future business (Friday) → Run workflow → Run workflow.**
2. Wait two or three minutes, then refresh and click into the run.
3. The **Build and send the future-business email** step says in plain English
   what happened. Either it posted to the flow, or it says why not.
4. Check your inbox.

A manual run always goes ahead. Scheduled runs only proceed at 15:00 London
time — see the note about the two cron lines in the workflow file.

---

## When it does not work

| What the log says | What it means | What to do |
|---|---|---|
| "Nothing scheduled or open that is relevant — no email sent, deliberately." | An empty week. Usually recess. | Nothing. This is correct behaviour. |
| "No flow URL configured (MONITOR_FLOW_URL)." | Stage 2 was not done, or the name is misspelt. | Check the secret's name letter for letter. |
| "The flow rejected the request (401/403)." | The URL has been regenerated. | Copy the HTTP POST URL from the flow again and update the secret. |
| "The flow URL returned 404." | The flow was deleted, renamed or turned off. | Turn it back on in Power Automate, or rebuild it and update the secret. |
| "Could not reach the Power Automate flow." | A network problem at GitHub's end. | Re-run the workflow. If it persists, it is not a configuration fault. |

The email arrives but shows raw HTML tags → step 6, the `</>` button. Switch
the body field to HTML mode and save the flow again.

---

## Changing the day or the time

Both live in `.github/workflows/forward.yml`. There are two cron lines and a
`TARGET_LONDON_HOUR`, and they work together: GitHub's cron is always UTC and
does not follow British Summer Time, so both possible UTC hours are scheduled
and the run stops itself unless the London clock says the intended hour. If you
change the hour, change all three.
