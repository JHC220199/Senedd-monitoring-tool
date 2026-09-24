# Debate summaries

On any weekday morning after the Senedd has sat, one email: every debate or
exchange from the day before that touches the NRLA's interests, a sentence or
two per speaker, in the body of the email. Each line links to the exact
moment in the Senedd's Record.

It replaces the notes Camlas send after relevant debates, with the three
changes the directorate asked for:

| Camlas | This |
|---|---|
| A Word attachment per debate | Everything in the body of one email |
| A paragraph per speaker | A sentence or two per speaker |
| Hours after the debate | The next morning, just after the Bore da briefing |

**Nothing to set up for the basic version.** It runs after the morning
briefing, through the same Power Automate flow, with no new token or flow. The
first email will come on the morning after the next sitting.

---

## What it looks like

For the Building Safety statement on 22 September 2026: the statement's
subject in one line, then the Cabinet Minister, Francesca O'Brien MS, the
Minister's reply, Jayne Bryant MS, and so on. Each has their name, their role
if they have one, what they said, and a **Record ›** link.

For the Business Statement the same day, only the two requests that matched —
Carmelo Colasanto MS on nutrient neutrality and housing delivery, and John
Clark MS on HMOs used for Home Office schemes — each with the Trefnydd's
answer. These are the same two Camlas picked.

No email is sent on a morning when nothing relevant was said.

---

## Two ways of summarising — your choice

### Without anything extra (this is how it starts)

Each speaker's most relevant **one or two sentences, word for word**, taken
from the Record. Nothing is paraphrased, so nothing can be misstated, and
nothing is sent anywhere. The drawback is that it reads as quotations rather
than as a summary, and sometimes the sentence it picks is not the one a person
would have chosen.

### With AI summaries (optional, needs an API key)

Claude, Anthropic's AI model, writes one or two sentences per speaker in
reported speech, as Camlas do. For example: *"The Cabinet Minister gave the
latest figures — 11 of 161 buildings completed or not needing work, work under
way on 71…"*

Safeguards that are built in:

- It is given only that debate's words and told to add nothing.
- **Every figure is checked by the tool itself.** A summary containing a
  number the speaker did not say is thrown away, and the speaker's own
  sentences are shown in its place.
- It is told not to name private individuals such as constituents or
  residents. MSs, ministers and public bodies are named, as Camlas name them.
- If the AI service is down, that morning's email falls back to the
  word-for-word version on its own.
- The email says clearly that the summaries are AI-written, and every line
  links to the Record.

**What it sends where.** The text of the relevant debates goes to Anthropic's
API. That text is the Senedd's public Record (Open Government Licence), and
the speakers are MSs and ministers speaking in public. Nothing from NRLA's own
systems or mailboxes is sent. Under Anthropic's commercial terms, API data is
not used to train models by default. Worth a line to whoever signs off data
processing at NRLA before switching it on, because it is a new supplier.

**Cost.** A typical debate costs about 4p to summarise, using the default
model, Claude Sonnet 5. A busy sitting week costs well under £1.

#### To switch it on (about 10 minutes)

1. Go to <https://console.anthropic.com> and sign in, or create an
   organisation account. Ideally use an NRLA account that finance can see,
   not a personal one.
2. **Billing:** add a payment method and a small prepaid credit (e.g. £10).
   Set a **monthly spend limit** under *Limits*. £5 is plenty.
3. **API keys → Create key.** Name it `Senedd debate summaries`. Copy it. It
   starts `sk-ant-` and is shown once.
4. On GitHub, open the repository → **Settings → Secrets and variables →
   Actions → New repository secret**.
   - **Name:** `ANTHROPIC_API_KEY`
   - **Secret:** paste the key.
   - **Add secret.**
5. That is all. The next morning's email will use AI summaries. The run log
   says which mode it used.

To switch it off, delete the secret. To use a different model, add a
repository **variable** or secret called `DEBATE_SUMMARY_MODEL`. The default
works; you do not need to change it.

Do not paste the key into an email, a chat or the repository. If it is ever
exposed, delete it in the Anthropic console and make a new one.

---

## How it decides what is relevant

It uses the same rules as the live page, applied to each thing said.

- **A whole debate** is summarised when its title is relevant (for example,
  a housing statement), or when at least 40% of what was said is relevant.
- **Otherwise, individual exchanges** are picked. An exchange is a question
  or request and its answer: a Business Statement request and the Trefnydd's
  reply, or a question to the First Minister and the answer.
- **Question sessions** always go exchange by exchange, so a question about
  buses at housing questions is left out.
- **Committees** are summarised as whole evidence sessions when the session
  is relevant. The housing committee's sessions on electoral regulations are
  not.

If it picks something irrelevant, or misses something, the fix is the same as
for the live page: add a term, or an `exclude_if` term, in
`config/taxonomy.yaml`. On 22 September, "train overcrowding", "a licensing
scheme for building companies" and fly-tipping enforcement were excluded this
way, and "houses in multiple occupancy" was added.

---

## Timing

- **Plenary** (Tuesdays and Wednesdays): the Senedd publishes the draft
  Record during the sitting. It is complete by the next morning, so
  Tuesday's debates are in Wednesday's email.
- **Committees**: the Record usually appears the next day, and sometimes two
  or three days later. A committee meeting is summarised on the first morning
  its Record is up. Until then, the email lists it under **Still awaited**.
- The run starts when the morning briefing finishes, at about 07.35, including
  on days the Senedd is not sitting, so Thursday's committees can appear on
  Friday.

The tool remembers which meetings it has done in `data/debates-sent.json`, so
nothing is sent twice. If an email fails to send, the same meetings are tried
again the next morning.

---

## Checking it and running it by hand

- **GitHub → Actions → Senedd debate summaries.** Each run's log lists the
  meetings it read, how many relevant items each had, and which mode it used.
  The email is attached to the run as an artifact.
- **Run workflow** on that page runs it at any time. It sends only what has
  not been sent before.

---

## What it does not do

- It does not cover **written questions**. The team's dedicated tool tracks
  those.
- The draft Record is not final, and the Senedd corrects it over the
  following weeks. Quote from the final Record.
- An AI summary is a summary. For anything going to members or to the press,
  read the Record, which is one click away on every line.
