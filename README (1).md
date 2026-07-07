# Nuclear Newsboard

Three distinct headlines every weekday morning, mapping the nuclear renaissance. Auto-fetched, emailed for approval, published to GitHub Pages, embedded in SharePoint.

## How it runs

1. **Morning fetch** (`fetch.yml`, 11:00 UTC weekdays): `fetch_headlines.py` pulls World Nuclear News, POWER Magazine, Neutron Bytes, Utility Dive, and three Google News queries; filters to nuclear-relevant stories from the last 72 hours; scores them across five category buckets (Policy & Licensing, Deployment & Construction, Corporate & Capital, Fuel Cycle, Global Fleet) and picks the top story from the three strongest buckets — that bucket separation is what keeps the three headlines distinct and trend-mapping rather than three takes on one story. It resolves Google News redirect links to real article URLs, scrapes each winner's og:image and description, writes `pending.json` with **six numbered candidates** (weapons/military stories are hard-excluded), and emails them to you.
2. **Approval — no GitHub account needed**: the email shows all six stories with STORY 1–6 chips and a **Review & pick 3** button. It opens `approve.html` on the Pages site: tick three stories, hit *Publish board*, done. The page posts to a tiny Cloudflare Worker that verifies the emailed link's token and fires the publish workflow — the GitHub credential lives only in the Worker, so approvers never touch GitHub. (Without the Worker configured, the email falls back to the GitHub *Run workflow* + picks-box flow.) Blurbs or titles can still be edited in `pending.json` beforehand if needed.
3. **Publish** (`publish.yml`): pulls your three picks out of `pending.json`, writes `news-YYYY-MM-DD.json`, and updates `archive_index.json`. Bad input (wrong count, duplicates, out-of-range numbers) fails loudly and publishes nothing. The page picks up the new edition immediately.
4. **The board** (`index.html`): three-card layout with photo, category/source eyebrow, headline, blurb. Previous editions live on the decay-chain strip at the bottom — click a node or use ←/→ arrow keys. Cards without a scrapeable photo render an orbiting-atom placeholder.

## Repo setup (flat layout)

All data and page files sit at the repo root — `index.html`, `archive_index.json`, `news-*.json`, the two Python scripts, `requirements.txt`. The only subdirectory is `.github/workflows/`, which GitHub requires. **The web-upload UI flattens dragged folder contents**, so create the workflows with *Add file → Create new file* and type the full path (`.github/workflows/fetch.yml`) into the filename box — that preserves the path.

Enable Pages: Settings → Pages → Deploy from branch → `main` / root.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Purpose |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` | Mail server (port 587 STARTTLS assumed) |
| `SMTP_USER` / `SMTP_PASS` | Credentials |
| `MAIL_TO` | Your work email |
| `MAIL_FROM` | From address (optional; defaults to SMTP_USER) |
| `ANTHROPIC_API_KEY` | Optional — Claude curates the 6 candidates and writes the blurbs + a one-line trend note; without it the keyword heuristic runs |
| `APPROVAL_SECRET` | Optional — any long random string; enables the no-GitHub approval page. Must match the Worker secret of the same name |

**SMTP note:** corporate Office 365 tenants usually have SMTP AUTH disabled, so `smtp.office365.com` with your KH login will likely bounce. Two paths that work: a personal Gmail with an [app password](https://myaccount.google.com/apppasswords) (`smtp.gmail.com` / 587), or a free transactional sender like Resend or Brevo (they hand you SMTP credentials in one screen). Either can deliver *to* your KH inbox — the sender account doesn't have to be KH.

No secrets set? The fetch still runs and commits `pending.json`; it just skips the email. You can watch for the commit and run publish manually.

## No-GitHub approval (Cloudflare Worker, ~5 min, free)

Lets anyone with the emailed link approve — practice builders, marketing, whoever — with zero GitHub access.

1. Make a fine-grained GitHub PAT: github.com → Settings → Developer settings → Fine-grained tokens → New. Repository access: **only this repo**. Permissions: **Actions → Read and write**. Nothing else.
2. dash.cloudflare.com (free account) → **Workers & Pages → Create Worker** → replace the starter code with `worker.js` from this repo → Deploy.
3. Worker → Settings → **Variables and Secrets**, add three secrets: `GH_PAT` (the token), `APPROVAL_SECRET` (any long random string), `REPO` (e.g. `CTabarrok/Nuclear-Newsboard`).
4. Copy the worker URL (`https://….workers.dev`) into `WORKER_URL` at the top of `approve.html`.
5. Add `APPROVAL_SECRET` (same string as step 3) to the repo's Actions secrets.

From then on the morning email's button carries a token that's only valid for that day's edition — the link can be forwarded to whoever should approve, and it can't be forged or reused for a different date. Each edition publishes on the first valid approval; a re-approval just overwrites the same day's file with a different trio, so a stray second click is harmless.

## SharePoint embed

Standard iframe via the Embed web part (domain already whitelisted):

```html
<iframe src="https://ctabarrok.github.io/Nuclear-Newsboard/" width="100%" height="760" frameborder="0"></iframe>
```

The layout stacks to a single column below 820 px, so it also holds up in narrow web-part zones.

## Local test

```
pip install -r requirements.txt
python fetch_headlines.py   # writes pending.json (email skipped without SMTP vars)
python publish.py           # promotes it to the archive
python -m http.server       # view at localhost:8000 (fetch() needs http, not file://)
```

## Seed data

Ships with two real editions (2026-07-06 and 2026-07-07) built from live feeds at build time, so the board and the decay chain render on first deploy.
