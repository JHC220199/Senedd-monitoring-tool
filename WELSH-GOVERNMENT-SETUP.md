# Making the Welsh Government half work

Written for someone who has never opened the Azure portal. Nothing here assumes
you can code. Where a step needs someone else, it says so, and there is a
ready-to-send paragraph you can paste into an email.

---

## Read this first: most of it now works on its own

You do not have to do anything to get Welsh Government **announcements**.

The original problem was this. Welsh Government content lives on `www.gov.wales`.
That site sits behind a security service (CloudFront) which rejects requests from
data-centre computers, and GitHub — where this tool runs every morning — is a
data centre. Every attempt came back `403 Forbidden`. Changing how the tool
identified itself made no difference, and pretending to be a web browser to get
around a security rule is not something this tool will do.

On 19 August 2026 a second Welsh Government site turned out to be reachable:

| Address | From GitHub |
|---|---|
| `www.gov.wales/announcements/rss` | blocked (403) |
| `media.service.gov.wales/news` | **works (200)** |

`media.service.gov.wales` is the Welsh Government's own newsroom. It carries the
same press notices, tagged by topic — Housing, Local Government, Finance — and it
is not behind that security rule. The tool now reads it on every run. No account,
no password, no permission, no IT ticket.

So the Welsh Government section of the page is no longer empty, and the amber
"not everything is being monitored" banner should disappear on the next run.

**What still is not covered, and this is the part worth understanding:**

1. **The consultation register** at `gov.wales/consultations`. This is the
   official list, and it holds the *authoritative closing dates*. The newsroom
   usually carries an announcement when a big consultation opens, so you will
   normally hear about it — but the deadline should be confirmed on gov.wales
   before you plan around it.
2. **Written statements** laid before the Senedd, unless they were also issued as
   a press announcement.

Both of those are on the collapsed "What this page does not cover" panel on the
live page, so the limitation is visible to anyone reading it, not just to you.

Everything below is about closing gap 1 and 2. It is worth doing. It is not
urgent, and nothing is broken while you decide.

---

## The two ways to close the remaining gap

**Route A — the email route.** The Welsh Government offers email alerts for
consultations and announcements. You subscribe, and the tool reads those emails.
This is the publisher's own mechanism, so nothing is being scraped or worked
around. It needs one small piece of setup from NRLA IT, once.

**Route B — run it from an NRLA machine.** From inside the NRLA network,
`gov.wales` is reachable normally, so the tool works with no special setup at
all. The cost is that it only runs when that machine is on and someone has set up
a scheduled task on it.

Route A is the recommended one, because it keeps the "runs itself every morning
whether or not anyone remembers" property that makes this tool worth having.

---

# Route A — the email route, step by step

Four stages. You can do stages 1 and 3. Stage 2 needs IT. Stage 4 is you
checking it worked.

Total of your own time: about twenty minutes, spread over however long IT takes.

---

## Stage 1 — subscribe to the Welsh Government alerts (5 minutes, you, today)

1. Open <https://www.gov.wales/subscribe/consultations> in your browser.
2. Enter **joshua.helm-cowley@nrla.org.uk**.
3. Choose the topics that matter — Housing, and Local Government and Planning at
   minimum. Consider Finance too, because council tax and land transaction tax
   consultations land there.
4. Submit, then open the confirmation email and click the link. Nothing arrives
   until you confirm.
5. Do the same at <https://www.gov.wales/subscribe/announcements>.

That is stage 1 finished. From now on Welsh Government consultation alerts arrive
in your inbox. Even if you never complete the remaining stages, you personally
stop missing consultations from today — the rest is about the tool seeing them
too, so they land on the page with everything else.

**Do not set up an Outlook rule that moves these emails out of your Inbox.** The
tool reads your Inbox folder. If a rule files them into a subfolder, the tool
will not see them.

You *can* read them yourself as normal. The tool looks at emails by date
received, not by whether you have opened them, so opening one does not hide it.

---

## Stage 2 — ask IT for one app registration (10 minutes of IT's time)

### What you are asking for, in plain terms

A program that runs without a person sitting at a keyboard cannot type a
password. Microsoft's answer is an "app registration": IT creates an identity for
the tool, gives it permission to read one specific mailbox and nothing else, and
hands you three values to paste into GitHub.

The permission asked for is **read-only** (`Mail.Read`). The tool cannot send,
delete, move or reply to anything.

### Something to decide before you send this

This route means an automated app can read the mailbox it is pointed at. You
asked for your own address to be used, which is fine and is what the tool is
configured for — but two things are worth weighing:

- A **shared mailbox** (for example one called `policy-alerts@`) would mean the
  app reads a mailbox that contains only machine-generated alerts, rather than
  one that also contains your personal correspondence. IT can create one in a
  couple of minutes, and it is the lower-exposure option.
- Either way, ask IT to apply an **application access policy**, which restricts
  the app to that single mailbox. Without it, the permission technically covers
  every mailbox in the NRLA tenant. IT will very likely insist on this anyway.

It is also worth a short note to whoever holds the data protection brief, because
you are pointing an automated process at a mailbox. The emails themselves are
machine-generated bulletins with no personal data in them, and the tool
deliberately stores only the subject, the body and the date received — never
recipients, headers or attachments — and ignores any email that did not come from
a gov.wales-family sender. That is a short, easy conversation to have in advance
and an awkward one to have afterwards.

### Paste this into an email to IT

> Could you set up a read-only Microsoft Graph app registration for a policy
> monitoring tool the directorate runs?
>
> What it does: reads Welsh Government consultation-alert emails from one mailbox
> each morning, so consultation deadlines appear on our policy monitoring page.
> It never sends, moves or deletes anything.
>
> What I think it needs:
>
> - An app registration in Entra ID — single tenant, no redirect URI needed.
> - The **application** permission `Mail.Read` on Microsoft Graph (not delegated),
>   with admin consent granted.
> - An Exchange application access policy restricting that app to the one mailbox
>   only — `joshua.helm-cowley@nrla.org.uk`, or a dedicated shared mailbox if you
>   would prefer to create one, which I am happy with.
> - A client secret.
>
> Could you send me the Directory (tenant) ID, the Application (client) ID and
> the client secret value, and let me know the secret's expiry date so I can
> diarise renewing it?
>
> The secret goes into a GitHub Actions encrypted secret, which is only readable
> by the workflow. Happy to talk through the tool if that helps.

### If IT asks you what the clicks are

For reference, in the Azure portal (<https://portal.azure.com>):

1. **Microsoft Entra ID → App registrations → New registration.** Name it
   something like `NRLA Senedd Monitor`. "Accounts in this organizational
   directory only". No redirect URI. **Register**.
2. On the **Overview** page, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. **API permissions → Add a permission → Microsoft Graph → Application
   permissions →** tick **Mail.Read → Add permissions**. Then **Grant admin
   consent**. The Status column must show a green tick; if it does not, the app
   is authenticated but allowed to read nothing.
4. **Certificates & secrets → Client secrets → New client secret.** Give it a
   description and an expiry. Copy the **Value** immediately — Azure never shows
   it again, only the Secret ID, which is not the thing needed.
5. Scope it to one mailbox, in Exchange Online PowerShell:

   ```powershell
   New-ApplicationAccessPolicy -AppId <application-client-id> `
     -PolicyScopeGroupId joshua.helm-cowley@nrla.org.uk `
     -AccessRight RestrictAccess `
     -Description "NRLA Senedd policy monitor - read-only, one mailbox"

   Test-ApplicationAccessPolicy -Identity joshua.helm-cowley@nrla.org.uk `
     -AppId <application-client-id>
   ```

   The test should return `AccessCheckResult : Granted`.

---

## Stage 3 — put the four values into GitHub (10 minutes, you)

You will have four things: the mailbox address, the tenant ID, the client ID and
the client secret.

1. Go to <https://github.com/JHC220199/Senedd-monitoring-tool>.
2. Click **Settings** (the tab along the top of the repository, not your own
   account settings).
3. In the left sidebar: **Secrets and variables → Actions**.
4. Click the green **New repository secret** button, once for each row below.
   The **Name** must be typed exactly as shown — capitals, underscores and all.

   | Name | Secret (value to paste) |
   |---|---|
   | `MONITOR_MAILBOX` | `joshua.helm-cowley@nrla.org.uk` |
   | `MONITOR_GRAPH_TENANT` | the Directory (tenant) ID |
   | `MONITOR_GRAPH_CLIENT_ID` | the Application (client) ID |
   | `MONITOR_GRAPH_CLIENT_SECRET` | the client secret **value** |

5. Once saved, GitHub will never show you a secret again — you can only replace
   it. That is normal and correct. Keep the secret in NRLA's password manager,
   not in an email folder.

**Do not create a secret called `MONITOR_GRAPH_TOKEN`.** An older version of
these instructions told you to, and following it would have quietly broken the
feed. A Microsoft access token lasts about an hour, so it works for one run and
then fails every morning afterwards with a `401` buried in a log nobody reads.
The four secrets above are the arrangement that survives unattended, because the
tool uses them to fetch itself a fresh hour-long token on every run. If a
`MONITOR_GRAPH_TOKEN` secret already exists, delete it — it takes precedence over
the others.

---

## Stage 4 — prove it worked (5 minutes, you)

1. Go to the **Actions** tab of the repository.
2. Click **Senedd policy monitor** in the left sidebar.
3. Click **Run workflow → Run workflow**. Wait two or three minutes and refresh.
4. Click into the run, then the **monitor** job, then expand **Collect, score and
   store**. You are looking for a line naming the mailbox source with a count
   next to it. If something is wrong, this is where it says so in plain English —
   the tool translates Microsoft's error codes rather than printing them raw.
5. Open <https://jhc220199.github.io/Senedd-monitoring-tool/> and check that the
   "What this page does not cover" panel no longer lists the consultation
   register.

---

## When it does not work

The tool prints a plain-English reason in the run log. The five that actually
happen:

| What the log says | What it means | What to do |
|---|---|---|
| "the client secret is wrong — check you pasted the secret VALUE, not the Secret ID" | Azure shows both; the Secret ID is not a password | Get the value from IT again, or have them generate a new secret |
| "the client secret has expired" | Secrets expire; 24 months is Microsoft's maximum | Ask IT for a new one and update `MONITOR_GRAPH_CLIENT_SECRET` |
| "the tenant is not recognised" | `MONITOR_GRAPH_TENANT` is wrong | Check for a stray space or a truncated paste |
| "403 (forbidden) ... check the Exchange application access policy" | The app is real but not allowed at that mailbox | Stage 2 step 5 was skipped, or names a different mailbox |
| "401 ... admin consent never having been granted" | The permission was added but not consented | Stage 2 step 3 — the green tick |

If Welsh Government consultations simply do not appear and there is no error at
all, check in this order: did you click the link in the subscription confirmation
email (stage 1 step 4); is an Outlook rule moving the alerts out of your Inbox;
and is the sender address in the gov.wales family — the tool ignores anything
else on purpose, so that a colleague emailing that mailbox never ends up in the
archive.

---

## Diarise this

Put one recurring reminder in your calendar: **renew the client secret**, a month
before whatever expiry date IT gives you. An expired secret does not announce
itself. The Welsh Government section just goes quiet, which looks exactly like a
quiet fortnight in Cardiff Bay.

---

# Route B — run it from an NRLA machine instead

No Azure, no IT, no secrets. The trade-off is that it only runs when that machine
is switched on.

From inside the NRLA network, `gov.wales` is reachable, so:

1. Install Python 3.11 or later.
2. Download the repository (green **Code** button → **Download ZIP**) and unzip
   it.
3. In that folder: `pip install -r requirements.txt`
4. Run:

   ```
   python -m monitor.cli --govwales-route rss collect --days 21
   python -m monitor.cli site --out docs/index.html
   ```

5. If it collects Welsh Government items, the network is not blocked and this
   route works. To make it run by itself, use Windows Task Scheduler, and have it
   commit `data/archive.sql` and `docs/index.html` back to the repository — which
   needs git set up on that machine.

Honestly: this is more fiddly to keep alive than Route A, and it dies the day
that machine is replaced. It is listed because it is a legitimate answer, and
because running it once by hand is the quickest way to prove what the office
network can see.

---

## What each secret is for, for whoever maintains this next

| Secret | Read by | Purpose |
|---|---|---|
| `MONITOR_MAILBOX` | `monitor/cli.py` | which mailbox to read; its absence disables the route entirely |
| `MONITOR_GRAPH_TENANT` | `monitor/graph_auth.py` | which Microsoft tenant to authenticate against |
| `MONITOR_GRAPH_CLIENT_ID` | `monitor/graph_auth.py` | which app registration |
| `MONITOR_GRAPH_CLIENT_SECRET` | `monitor/graph_auth.py` | proves the app is itself |
| `MONITOR_GRAPH_TOKEN` | `monitor/graph_auth.py` | a pasted hour-long token; for one manual test only, and it overrides the four above |

The mail query is in `GovWalesMailboxCollector.first_page_url`. It filters on
`receivedDateTime`, deliberately not on the unread flag: an earlier version
filtered on unread, which meant that opening a Welsh Government email in Outlook
hid it from the monitor forever. Reading by date also keeps the permission ask at
`Mail.Read`, because nothing has to be marked as read.
