# Putting this on GitHub with no command line and no paid account

Two things to clear up first, because both are blocking you unnecessarily.

## 1. You do not need to pay for anything

GitHub **Free** includes **unlimited private repositories**. A private repo is not
a paid feature and has not been for years. Sign up, choose Private, done.

The one thing worth checking is the **Actions allowance** — the free minutes for
running scheduled jobs on private repos. GitHub's own documentation did not state
the per-plan figure on the pages I could read, and the rates changed in January
2026, so check the current number against your account rather than trusting one
written here. What I can tell you is the consumption: **about 3 minutes per run,
so roughly 200 minutes a month** on the weekday-plus-Saturday schedule.

If the allowance turns out to be a problem, the options in order of preference:

- Drop to three runs a week (edit the two `cron:` lines) — roughly 80 minutes.
- Ask IT for an NRLA GitHub organisation seat, if one already exists.
- A **public** repo has unlimited free Actions minutes — but I would not do this.
  The archive is a curated record of which Senedd business the NRLA considers
  important and how highly we rate it. That is a view of our policy priorities,
  and it should not be published by accident.

## 2. Yes, you can upload the code through the website — with one catch

Drag-and-drop upload in the browser works fine for almost everything. But there
are **two folders and files beginning with a dot** that your computer treats as
hidden, and drag-and-drop will silently leave them behind:

| What gets skipped | What it does | What happens if it's missing |
|---|---|---|
| `.github/workflows/monitor.yml` | The daily schedule | **Nothing ever runs.** No error, no warning. |
| `.gitignore` | Keeps the working database and any secrets out of the repo | Clutter, and a real risk of committing something you shouldn't |

This is the trap I flagged earlier: it looks like it worked, and it hasn't. So
those two get typed in by hand afterwards. It takes about four minutes.

## `.git` is the exception — do not upload that one

There is a third dot-folder in the unzipped package, `.git`, and it is one letter
away from `.github`, which is unhelpful of them. They are completely different
things:

- **`.github`** is *your* folder. It holds the workflow — the instructions for
  what to run and when. You need it. This is the one you type in by hand.
- **`.git`** is git's own internal bookkeeping: the compressed history of every
  commit, in a format meant only for the software to read. You do **not** upload
  it. GitHub creates its own `.git` on its side the moment you make the repo.

So on the browser route, drag-and-drop skipping `.git` is **correct behaviour, not
a problem**. Ignore it entirely. If you somehow forced it up there you would get a
few hundred junk files and no working history.

Rule of thumb: `.github` — needed, type it in. `.git` — leave alone, always.

On the **GitHub Desktop** route it is the reverse: `.git` is exactly what makes it
work. Desktop reads that folder to find the existing history and sends it up
through git properly, as commits rather than as files. That is why Desktop needs
no hand-typing — it is talking to git, not uploading a pile of files.

---

# Do this

## Step 1 — Make the account and the repository

1. Go to github.com and sign up (free).
2. Click **+** (top right) → **New repository**.
3. Name: `senedd-monitor`. Select **Private**. Do **not** tick "Add a README".
4. **Create repository**.

You will land on a mostly empty page. Leave it open.

## Step 2 — Unzip the package on your computer

Unzip `senedd-monitor.zip`. You get a folder called `senedd-monitor` containing:

```
config/  deploy/  monitor/  out/  samples/  tests/  tools/  data/
README.md  SPECIFICATION.md  SETUP-GITHUB.md  requirements.txt
```

## Step 3 — Upload all of that

On the repository page, click **uploading an existing file** (in the "quick setup"
text), or go to **Add file → Upload files**.

Open the unzipped `senedd-monitor` folder, select **everything inside it** —
all the folders and all the loose files, but *not* the `senedd-monitor` folder
itself — and drag them onto the browser window.

Wait for the file list to finish populating, then click **Commit changes**.

> If the browser refuses a drop of folders, upload the loose files first, then
> repeat the drop once per folder. Tedious, not a problem.

## Step 4 — Type in the workflow file (the step that cannot be skipped)

1. **Add file → Create new file**.
2. In the filename box, type exactly:

   ```
   .github/workflows/monitor.yml
   ```

   Typing the `/` characters creates the folders as you go — you will see the box
   split into breadcrumbs. That is correct.
3. In your unzipped folder, open `deploy/github-actions-workflow.yml` in Notepad
   or TextEdit. **This is the same file, put there deliberately as a readable
   copy for exactly this moment.** Select all, copy.
4. Paste it into the GitHub editor.
5. **Commit changes**.

## Step 5 — Type in the .gitignore

1. **Add file → Create new file**.
2. Filename: `.gitignore` (just that, dot included).
3. Paste the contents of the `.gitignore` from `deploy/gitignore.txt` in the
   unzipped folder — same trick, a readable copy of a hidden file.
4. **Commit changes**.

## Step 6 — Prove it works

Click the **Actions** tab.

- You should see **"Senedd policy monitor"** in the left sidebar. If it is there,
  Step 4 worked. If the tab says "Get started with GitHub Actions" instead, the
  workflow file is in the wrong place — check the filename has `.github` with the
  dot and `workflows` with the s.
- Click it, then **Run workflow → Run workflow**.
- It turns yellow, then green after about three minutes. Click into the run to
  see what it found, at the bottom under the summary.

From this point it runs itself: 07.30 weekday mornings and 09.30 Saturday, with
nobody prompting anything.

## Step 7 — Turn the email on

Until you do this the run works but emails nothing, which is deliberate — a
half-configured repo must not be able to mail a distribution list.

**Settings → Secrets and variables → Actions → New repository secret**, once per
row:

| Name | Value |
|---|---|
| `MONITOR_TO` | `first.last@nrla.org.uk` (comma-separate several) |
| `MONITOR_FROM` | the mailbox the alerts come from |
| `MONITOR_SMTP_HOST` | `smtp.office365.com` |
| `MONITOR_SMTP_USER` | that mailbox again |
| `MONITOR_SMTP_PASS` | an app password from IT |

Secrets are write-only once saved — GitHub will not show them back to you, and
they never appear in the repository or in a log. Ask IT for a dedicated service
mailbox rather than using your own credentials.

Start with `MONITOR_TO` set to just yourself. Add the rest of the directorate
after you have seen a week of it.

---

# The easier alternative, if the dot-file typing bothers you

**GitHub Desktop** (desktop.github.com, free, no command line) does not have the
hidden-file problem at all:

1. Install it, sign in.
2. **File → Add local repository**, point it at your unzipped `senedd-monitor`
   folder. It already contains a git repository with two commits, so Desktop will
   recognise it immediately.
3. **Publish repository**, tick **Keep this code private**.

Everything goes up, dot-files included, and you can skip Steps 3–5 entirely.
This is the route I would take.

---

# What this does not give you

The GitHub runners are datacentre machines, and **gov.wales returns 403 to
datacentre machines**. So on GitHub you get full Senedd coverage — Plenary,
committees, consultations, legislation, the forward look — but not Welsh
Government press releases and written statements directly.

Two ways to close that:

- **The mailbox route.** Subscribe a shared mailbox to the Welsh Government
  updates you already get by email, and set the `MONITOR_MAILBOX` and
  `MONITOR_GRAPH_TOKEN` secrets. The collector then reads that mailbox instead of
  the website. Needs a Graph app registration from IT, scoped by Exchange
  application access policy to that one mailbox — see `README.md`.
- **Run it on an NRLA server instead**, which reaches gov.wales directly.
  `deploy/AUTOMATION.md` covers that comparison.

Neither is a reason to delay Step 6. Get the Senedd half running daily first;
that is the large majority of what the supplier briefing covered.
