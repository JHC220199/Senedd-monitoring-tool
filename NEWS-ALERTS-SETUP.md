# Political news alerts

An email when something big happens in Welsh politics: a defection, a leader
standing down, a new leader or deputy, a shadow cabinet, a reshuffle, a
suspension, a by-election or a vote of no confidence. These replace Camlas's
"breaking news" emails, such as Sarah Cooper-Lesadd MS's defection and Dan
Thomas MS standing down (15 September 2026), and Reform UK's shadow cabinet
(24 September).

They will be rare, and most weeks there will be none.

**The same hourly check also sends press release alerts.** Whenever the
Welsh Government publishes a press release or written statement that matches
the NRLA's relevance rules (the same rules as the live page), you get a
separate email. It has the notice's title, its own summary points word for
word, and a link, like Camlas's "Alert - Press Release". Camlas sent their
waking watch alert the afternoon after the notice was published. This one
arrives within the hour. There is nothing extra to set up.

---

## What an alert looks like

The subject line is the news: *Senedd news: Sarah Cooper-Lesadd MS defects to
Plaid Cymru from Reform UK*. The body has the headline as each outlet
published it, linked to the story, with the outlet and the time. When the
BBC and WalesOnline both report it, you get one alert with both headlines,
not two alerts.

Anything touching housing, such as a change of housing minister or shadow
housing spokesperson, carries the orange **NRLA** tag.

**Nothing is copied or summarised.** The alert gives the headline and the
link, and you read the story at the outlet. Where a story is about an
allegation, someone is named only if the outlet's own headline names them.

---

## Where it looks

| Source | What it catches |
|---|---|
| BBC News Wales, politics feed | Most Senedd news, usually first |
| Nation.Cymru | Welsh politics, often with party statements |
| WalesOnline, politics feed | A third outlet, for what the others miss |
| The Welsh Government's list of ministers, gov.wales | **Any** change to who holds which ministerial job. This comes from the Welsh Government itself, not from the news. |

Party websites refuse automated requests from cloud servers, and so does the
Senedd's own members list, so the tool relies on the news for the opposition
side. A shadow cabinet announcement arrives as a headline with a link, not as
a list of names.

## What counts as news

The tool only alerts on political changes: leadership changes, defections,
resignations, appointments, reshuffles, shadow cabinets, suspensions and the
whip, by-elections, votes of no confidence, and agreements between parties.

These are left out:

- council politics (a council leader resigning);
- Westminster (an MP resigning);
- features and profiles ("Who is…", "What we learned…", "Q&A in full");
- MSs' private lives, such as a health diagnosis, unless it leads to one of
  the changes above;
- repeats. Once an event has been alerted, other outlets' versions of it and
  follow-ups are not alerted again for three days, unless the kind of change
  is new. For example, "elected leader" and later "names shadow cabinet" are
  two alerts.

---

## What you need to do: one more Power Automate flow (5 minutes)

News is no use hours late, and GitHub's own timer runs five to six hours
late. So, like the morning briefing, this is started by Power Automate: every
hour from 08.00 to 18.00 on weekdays. News that breaks overnight or at the
weekend is caught by the first run after it, at 08.00.

It uses the **same GitHub token** as the morning briefing flow. You do not need
a new one.

1. In Power Automate, open **Senedd morning briefing - 07.30 start** →
   **… (More) → Save As** → name it `Senedd news alerts - hourly`.
2. Open the copy. In **Recurrence**:
   - **Time zone:** (UTC+00:00) Dublin, Edinburgh, Lisbon, London
   - **On these days:** Monday to Friday
   - **At these hours:** 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18
   - **At these minutes:** 0
3. Keep **one** HTTP action and delete the other, the "Refresh live page"
   step. In the one you keep, change the end of the URI to `news.yml`:

   `https://api.github.com/repos/JHC220199/Senedd-monitoring-tool/actions/workflows/news.yml/dispatches`

4. **Save**, then **Turn on** (Save As leaves copies off).

**The very first run sends nothing.** It notes the headlines already out
there, so you are not sent last week's news. Alerts start from the second
run.

---

## Checking it

**GitHub → Actions → Senedd news alerts.** Each run's log lists how many
headlines it read and any political changes it found. A run that finds
nothing is green and short. That is normal.

### Press release alerts: what is included

- Press notices from the Welsh Government newsroom, and written statements
  and announcements from gov.wales. The newsroom does not publish written
  statements.
- Only those that match the relevance rules. On 22–24 September 2026 that
  was one notice out of eleven: the waking watch alarm grant, the same one
  Camlas alerted.
- Not "Oral Statement:" items. These are the text of statements already made
  in the Chamber, which the debate summaries cover.
- They also still appear, tagged NRLA, in the next morning's Bore da
  briefing.

The summary points are the Welsh Government's own words, reproduced under
the Open Government Licence. Nothing is paraphrased.

### Sending yourself a test

To check that alerts reach your inbox, without waiting for real news:
**GitHub → Actions → Senedd news alerts → Run workflow**, tick **Send TEST
alerts**, then **Run workflow**. About a minute later you get two emails whose
subjects start **TEST —**: one with the most recent political changes in the
news, and one with the most recent relevant Welsh Government notice. Either may
already be old news, and each has a "This is a test" banner. A test remembers
nothing, so it can never stop a real alert from being sent.

## When it goes wrong

| What you see | What it means |
|---|---|
| The run is red: "none of the news feeds could be read" | All three outlets refused or changed their feed address. The run page says which. |
| "The ministers page … layout has probably changed" | gov.wales redesigned the page. The tool compares nothing rather than reporting that the whole Cabinet resigned. |
| An alert you did not need | Tell Claude which one. The words that trigger an alert are in `monitor/news.py` (`TRIGGERS`, `EXCLUDE`). |
| No alert for something big | The same: send the story. Some news, such as a shadow cabinet in a party press release that no outlet headlines, will not be caught. |

The run keeps its memory in GitHub's Actions cache, not in the repository,
so it adds nothing to the history. If that memory is ever cleared, the next
run re-learns what is out there and sends nothing, and alerts resume from the
run after.
