# Turning on the morning briefing

About fifteen minutes, once. No IT ticket. Three things to make: a GitHub
token, a small Power Automate flow that uses it at 07.30 each weekday, and a
copy of that flow for Friday afternoons — which also fixes the Friday email,
which has not been going out on its own (Stage 3 explains why).

**Before you start:** the morning briefing sends through the **same** Power
Automate flow as the Friday future-business email, using the same
`MONITOR_FLOW_URL` secret. If the Friday email is already reaching you, that
half is done. If it is not, do `FORWARD-EMAIL-SETUP.md` first — five minutes —
and come back.

---

## What the email is

On every day the Senedd sits, at about 07.35:

1. **On today's agenda** — every broadcast committee meeting and Plenary, with
   its agenda, in time order. Committee housekeeping (apologies, papers to
   note, private sessions) is left out; Plenary is printed in full, as Camlas
   prints it.
2. **Welsh Government** — every announcement since the last briefing would have
   gone out, newest first, with the notice's own first sentence.
3. **Coming up tomorrow** — or on the next sitting day, if that is not tomorrow.

It is the **whole day**, as Camlas sends it. Anything that matches the NRLA's
relevance rules — the same rules as the live page — carries an orange **NRLA**
tag, and the subject line says how many there are. Oral questions tabled for
today on NRLA issues are listed under Plenary.

**No email when the Senedd is not sitting.** Recess, Fridays, bank holidays:
the flow still runs, the briefing sees nothing in the Senedd's diary, and
nothing is sent. Nobody has to maintain a list of sitting dates.

---

## Why a second flow is needed at all

GitHub runs this tool's daily collection on a timer set for 06.30. Over the
whole of September 2026 it actually started between **12.29 and 14.07** — five
to six hours late, every day. GitHub does not promise when a timed run starts.

A run that is started *by request* does not wait in that queue. So Power
Automate, which is punctual, becomes the alarm clock: at 07.30 it asks GitHub
to start the briefing, and the email arrives a few minutes later.

To be allowed to ask, the flow needs a token. That is Stage 1.

---

## Stage 1 — the GitHub token (5 minutes)

This token can do one thing: start workflows in this one repository. It cannot
read your email, change code, or touch any other repository.

1. On <https://github.com>, click your profile picture (top right) →
   **Settings**.
2. Scroll the left-hand menu to the bottom → **Developer settings**.
3. **Personal access tokens → Fine-grained tokens → Generate new token.**
4. **Token name:** `Senedd morning briefing`.
5. **Expiration:** pick a date — up to a year. Put a reminder in your calendar
   for a week before it. (When it expires, the briefing simply stops; see
   "When it stops" below.)
6. **Repository access:** choose **Only select repositories**, then pick
   **Senedd-monitoring-tool**. Not "All repositories".
7. **Permissions → Repository permissions:** find **Actions** and set it to
   **Read and write**. Leave everything else as it is. (GitHub will add
   "Metadata: Read-only" by itself; that is expected.)
8. **Generate token.** Copy the long string starting `github_pat_` and keep the
   page open — GitHub will never show it again.

**Do not paste the token into an email, a chat, a Teams message, or anywhere in
the repository.** It goes in one place only: Stage 2, step 6. If it is ever
exposed, delete it on the same GitHub page and generate a new one.

---

## Stage 2 — the 07.30 flow (10 minutes)

1. Go to <https://make.powerautomate.com> and sign in with your NRLA account.
2. **Create → Scheduled cloud flow.**
3. **Flow name:** `Senedd morning briefing — 07.30 start`.
   **Starting:** tomorrow's date, **07:30**.
   **Repeat every:** `1` **Week**.
   **On these days:** tick **Mon, Tue, Wed, Thu, Fri**.
   Click **Create**.
4. Click the **Recurrence** trigger to open it and check:
   - **Time zone:** **(UTC+00:00) Dublin, Edinburgh, Lisbon, London**. This is
     what makes it 07.30 in both summer and winter — it follows the clocks.
   - **At these hours:** `7` · **At these minutes:** `30`.
5. **New step** (or the **+** under the trigger) → search **HTTP** → choose the
   action called just **HTTP**.
6. Fill it in exactly:

   | Field | Value |
   |---|---|
   | **Method** | `POST` |
   | **URI** | `https://api.github.com/repos/JHC220199/Senedd-monitoring-tool/actions/workflows/morning.yml/dispatches` |
   | **Headers** | three rows — see below |
   | **Body** | `{"ref":"main"}` |

   Headers:

   | Key | Value |
   |---|---|
   | `Accept` | `application/vnd.github+json` |
   | `X-GitHub-Api-Version` | `2022-11-28` |
   | `Authorization` | `Bearer ` then paste the token from Stage 1 (one space after *Bearer*) |

7. Optional but sensible: open the HTTP action's **Settings** and turn on
   **Secure inputs**. That hides the token from the flow's run history.
8. **Save.**

### Test it

1. In the flow, click **Test → Manually → Test → Run flow**.
2. The HTTP step should go green with status **204**. (204 means "done,
   nothing to say" — it is the success code.)
3. On GitHub, open the **Actions** tab. Within a minute there should be a new
   run called **Senedd morning briefing**. Click it to watch.
4. If the Senedd is sitting today, the email arrives a few minutes later. If it
   is not, the run's log says *"the Senedd is not sitting, so nothing is
   sent"* — that is a pass.

---

## Stage 3 — the same fix for the Friday email (3 minutes)

**The Friday email has not been going out on its own.** It is timed for 15.00
on GitHub's timer, and it checks the London clock before sending so it never
arrives at the wrong hour. But GitHub started it at 18.22 and 19.16 on
18 September (and 18.26 and 19.21 on 11 September), so both times the clock
check said "not 15.00" and it stopped itself. Runs #7 to #10 on the Actions
page, each about ten seconds long, are those.

A run started by request is always let through, so the fix is the same as
this morning's:

1. In Power Automate, open the flow from Stage 2 → **… (More) → Save As** →
   name it `Senedd future business — Friday 15.00 start`.
2. Open the copy. In **Recurrence**: **On these days** → **Friday** only;
   **At these hours** → `15`; **At these minutes** → `0`.
3. In the **HTTP** action, change the end of the URI from `morning.yml` to
   `forward.yml`:

   `https://api.github.com/repos/JHC220199/Senedd-monitoring-tool/actions/workflows/forward.yml/dispatches`

4. **Save**, then **Turn on** the copy (Save As leaves copies switched off).

The timed GitHub runs carry on and keep stopping themselves, so there is no
risk of two emails.

---

## Optional — make the live page fresh in the morning too

The same trick fixes the page updating after lunch. In the same flow, add a
second **HTTP** action next to the first (use **Add a parallel branch**), with
everything identical except the URI ends `monitor.yml/dispatches` instead of
`morning.yml/dispatches`:

`https://api.github.com/repos/JHC220199/Senedd-monitoring-tool/actions/workflows/monitor.yml/dispatches`

The page will then be rebuilt from about 07.40. The late-running timed run
still happens after lunch as a backstop; running twice does no harm.

---

## When it goes wrong

| What you see | What it means | What to do |
|---|---|---|
| HTTP step fails with **401** | The token is wrong or has expired | Generate a new one (Stage 1) and replace it in the Authorization header |
| HTTP step fails with **403** | The token lacks permission | Check Stage 1 step 7: **Actions → Read and write**, on this repository |
| HTTP step fails with **404** | The URI is mistyped, or the token was not given this repository | Copy the URI from this page again; check Stage 1 step 6 |
| HTTP step fails with **422** | The body is wrong | It must be exactly `{"ref":"main"}` |
| The run on GitHub is red | Something in the tool failed | The run page says why in plain English; GitHub also emails you when a run you started fails |
| The run says "not switched on yet" | `MONITOR_FLOW_URL` is missing | Do `FORWARD-EMAIL-SETUP.md` |
| No email on a sitting day, run is green | Check the run log | Most often: the flow URL was regenerated — see `FORWARD-EMAIL-SETUP.md` |

### When it stops

If you stop getting the briefing on sitting days, the first thing to check is
the token's expiry date. Power Automate also emails a flow's owner when a flow
keeps failing.

---

## What it does not cover

- **Meetings that are not broadcast.** The diary comes from senedd.tv, which
  lists broadcast meetings only. A committee meeting held wholly in private
  will not appear. (On 24 September, the Finance Committee's introductory
  briefing from the Senedd Commission was in Camlas's email and not here.)
- **The Business Statement.** Camlas links to it as "Senedd Cymru"; this does
  not, yet.
- **Written questions**, deliberately — the team's dedicated tool tracks those.

## Your licence

Power Automate's **HTTP** action is a *premium* action. So is the **When an
HTTP request is received** trigger the Friday flow uses. If the Friday email
works, your account already has what this needs.
