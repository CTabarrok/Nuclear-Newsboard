# Nuclear Newsboard

Three distinct headlines every weekday morning, mapping the nuclear renaissance. Auto-fetched, emailed for approval, published to GitHub Pages, embedded in SharePoint.

## How it runs

1. **Morning fetch** (`fetch.yml`, 11:00 UTC weekdays): `fetch_headlines.py` pulls World Nuclear News, POWER Magazine, Neutron Bytes, Utility Dive, and three Google News queries; filters to nuclear-relevant stories from the last 72 hours; scores them across five category buckets (Policy & Licensing, Deployment & Construction, Corporate & Capital, Fuel Cycle, Global Fleet) and picks the top story from the three strongest buckets — that bucket separation is what keeps the three headlines distinct and trend-mapping rather than three takes on one story. It resolves Google News redirect links to real article URLs, scrapes each winner's og:image and description, writes `pending.json`, and emails you the picks.
2. **Approval**: the email has a button linking to the **Approve & publish** workflow. Click *Run workflow* — that's the approval. Want to swap a story first? Edit `pending.json` in the repo, then run the workflow.
3. **Publish** (`publish.yml`): promotes `pending.json` to `news-YYYY-MM-DD.json` and updates `archive_index.json`. The page picks it up immediately.
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
| `ANTHROPIC_API_KEY` | Optional — Claude curates the 3 picks and writes the blurbs + a one-line trend note; without it the keyword heuristic runs |

**SMTP note:** corporate Office 365 tenants usually have SMTP AUTH disabled, so `smtp.office365.com` with your KH login will likely bounce. Two paths that work: a personal Gmail with an [app password](https://myaccount.google.com/apppasswords) (`smtp.gmail.com` / 587), or a free transactional sender like Resend or Brevo (they hand you SMTP credentials in one screen). Either can deliver *to* your KH inbox — the sender account doesn't have to be KH.

No secrets set? The fetch still runs and commits `pending.json`; it just skips the email. You can watch for the commit and run publish manually.

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
