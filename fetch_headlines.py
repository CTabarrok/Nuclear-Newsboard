#!/usr/bin/env python3
"""
Nuclear Newsboard — morning fetch.

Pulls nuclear-energy headlines from a set of RSS feeds, picks the 3 most
interesting *distinct* stories (one per category bucket, so together they map
a trend rather than repeat one story three ways), grabs an og:image for each,
writes pending.json, and emails the picks for approval.

Curation modes:
  - Heuristic (default): keyword/source/recency scoring across category buckets.
  - Claude-assisted (optional): if ANTHROPIC_API_KEY is set, Claude picks the 3
    and writes the blurbs + a one-line trend note.

Env (all optional locally; set as GitHub Actions secrets for the real thing):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO, MAIL_FROM
  ANTHROPIC_API_KEY
  REPO_SLUG   e.g. "CTabarrok/Nuclear-Newsboard" (for the approve link)
"""

import hashlib
import hmac as hmac_mod
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; NuclearNewsboard/1.0)"}
TIMEOUT = 12

FEEDS = [
    # (url, source label, weight) — direct feeds outrank Google News so cards
    # get real URLs, real images, and real summaries.
    ("https://www.world-nuclear-news.org/rss", "World Nuclear News", 3),
    ("https://www.powermag.com/category/nuclear/feed/", "POWER Magazine", 3),
    ("https://neutronbytes.com/feed/", "Neutron Bytes", 2),
    ("https://www.utilitydive.com/feeds/news/", "Utility Dive", 2),
    ("https://news.google.com/rss/search?q=%22nuclear%20power%22%20OR%20%22nuclear%20energy%22%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
    ("https://news.google.com/rss/search?q=%22small%20modular%20reactor%22%20OR%20SMR%20nuclear%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
    ("https://news.google.com/rss/search?q=NRC%20license%20OR%20permit%20nuclear%20reactor%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
    ("https://news.google.com/rss/search?q=%22advanced%20reactor%22%20OR%20microreactor%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
    ("https://news.google.com/rss/search?q=uranium%20OR%20enrichment%20OR%20HALEU%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
    ("https://news.google.com/rss/search?q=%22data%20center%22%20nuclear%20power%20when:2d&hl=en-US&gl=US&ceid=US:en",
     "Google News", 1),
]

# Category buckets — the "distinctness" mechanism. One winner per bucket,
# top three buckets by best score make the board.
BUCKETS = {
    "Policy & Licensing": [
        "nrc", "license", "licence", "permit", "rule", "executive order",
        "doe ", "department of energy", "congress", "legislation", "tax credit",
        "moratorium", "ban", "approval", "environmental review", "part 53",
    ],
    "Deployment & Construction": [
        "construction", "groundbreaking", "break ground", "restart", "uprate",
        "grid", "online", "commission", "deploy", "site work", "concrete",
        "operational", "startup", "criticality", "first power",
    ],
    "Corporate & Capital": [
        "funding", "raise", "investment", "billion", "million", "deal",
        "agreement", "contract", "order", "acquisition", "partnership",
        "offtake", "ipo", "stock", "backing", "loan",
    ],
    "Fuel Cycle": [
        "haleu", "enrichment", "uranium", "fuel", "centrus", "urenco",
        "conversion", "mining", "mill", "fabrication", "triso",
    ],
    "Global Fleet": [
        "iaea", "uk ", "france", "china", "japan", "korea", "poland",
        "canada", "india", "czech", "sweden", "netherlands", "world's",
        "europe", "export",
    ],
}

INTEREST = [  # generic "this is a story, not a listicle" boosters
    ("first", 3), ("record", 3), ("historic", 2), ("largest", 2),
    ("approve", 2), ("announce", 2), ("select", 2), ("award", 2),
    ("begin", 2), ("complete", 2), ("sign", 2), ("launch", 1),
]

NOISE = [  # de-boost opinion/aggregation/markets churn
    "opinion", "editorial", "letter", "podcast", "webinar", "stocks to",
    "should you buy", "price target", "analyst", "here's why", "explained",
    "what to know", "newsletter", "week in review", "roundup", "stock price",
    "shares", "nyse:", "nasdaq:", "tumbl", "soar", "surge", "rally",
    "buy now", "etf", "top 5", "top 10", "best stocks",
]


def normalize_url(u):
    """Collapse duplicate slashes in the path (feed bugs like host//articles/...),
    leaving the scheme's // intact."""
    return re.sub(r"(?<!:)/{2,}", "/", u or "")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


WEAPONS_TERMS = [
    "weapon", "warhead", "nuclear bomb", "atomic bomb", "missile", "icbm",
    "ballistic", "deterren", "arsenal", "disarmament", "nonproliferation",
    "non-proliferation", "hiroshima", "nagasaki", "test ban", "nuclear war",
    "nuclear strike", "nuclear threat", "doomsday", "mushroom cloud",
]


def is_weapons(text):
    t = text.lower()
    return any(w in t for w in WEAPONS_TERMS)


NUCLEAR_TERMS = [
    "nuclear", "reactor", "smr", "uranium", "haleu", "fission", "nrc",
    "atomic", "nuscale", "oklo", "westinghouse", "terrapower", "x-energy",
    "kairos", "holtec", "microreactor", "enrichment", "fusion", "vogtle",
    "palisades", "radioisotope", "spent fuel",
]


def is_nuclear(text):
    t = text.lower()
    return any(w in t for w in NUCLEAR_TERMS)


def strip_publisher(title):
    """Peel trailing ' - Publisher' suffixes; Google News titles can stack two."""
    pat = r"\s+[-\u2013\u2014]{1,2}\s+[^-\u2013\u2014]{2,40}$"
    prev = None
    while title != prev and len(title) > 30:
        prev, title = title, re.sub(pat, "", title).strip()
    return title


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", strip_publisher(t).lower()).strip()


HISTORY_FILE = "shown_history.json"
HISTORY_DAYS = 30


def load_shown(today_str, include_today=False):
    """Titles/URLs already shown. Normally only previous days count (so a plain
    same-day re-run regenerates freely); a reshuffle also counts today's set so
    the rejected six can't come back."""
    try:
        hist = json.load(open(HISTORY_FILE))
    except Exception:
        return set(), set()
    titles, urls = set(), set()
    for day, entries in hist.get("days", {}).items():
        if day > today_str or (day == today_str and not include_today):
            continue
        for e in entries:
            if e.get("t"):
                titles.add(e["t"])
            if e.get("u"):
                urls.add(e["u"])
    return titles, urls


def save_shown(today_str, winners, append=False):
    """Record today's shown set; append on reshuffle so rejected sets stack."""
    try:
        hist = json.load(open(HISTORY_FILE))
    except Exception:
        hist = {"days": {}}
    entries = hist["days"].get(today_str, []) if append else []
    entries += [{"t": norm_title(w["title"]), "u": w["url"]} for w in winners]
    hist["days"][today_str] = entries
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    hist["days"] = {d: v for d, v in hist["days"].items() if d >= cutoff}
    with open(HISTORY_FILE, "w") as f:
        json.dump(hist, f, indent=2)


def collect():
    seen, items = {}, []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    for url, source, weight in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=UA)
        except Exception as e:
            print(f"  feed failed: {source}: {e}", file=sys.stderr)
            continue
        for e in feed.entries[:40]:
            title = clean(e.get("title", ""))
            if not title:
                continue
            key = norm_title(title)[:80]
            when = None
            for f in ("published_parsed", "updated_parsed"):
                if e.get(f):
                    when = datetime(*e[f][:6], tzinfo=timezone.utc)
                    break
            if not when or when < cutoff:
                continue  # undated entries are exactly how stale stories sneak in
            pub = source
            if source == "Google News" and e.get("source"):
                pub = clean(e.source.get("title", source))
                pub = re.split(r"\s+[-\u2013\u2014]{1,2}\s+", pub)[0].strip() or pub
            body_text = title + " " + clean(e.get("summary", ""))
            if not is_nuclear(body_text) or is_weapons(body_text):
                continue
            item = {
                "title": strip_publisher(title),
                "url": normalize_url(e.get("link", "")),
                "summary": clean(e.get("summary", ""))[:400],
                "source": pub,
                "published": when.isoformat() if when else None,
                "_weight": weight,
            }
            if key in seen:  # keep higher-weight duplicate
                if weight > seen[key]["_weight"]:
                    items[items.index(seen[key])] = item
                    seen[key] = item
                continue
            seen[key] = item
            items.append(item)
    return items


def score(item):
    text = (item["title"] + " " + item["summary"]).lower()
    s = item["_weight"] * 2
    for word, pts in INTEREST:
        if word in text:
            s += pts
    for word in NOISE:
        if word in text:
            s -= 4
    if item["published"]:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(item["published"])).total_seconds() / 3600
        s += 10 - age_h / 6  # 10 pts at 0h, 0 at 60h, mildly negative to 72h
    best_bucket, best_hits = None, 0
    for bucket, words in BUCKETS.items():
        hits = sum(1 for w in words if w in text)
        if hits > best_hits:
            best_bucket, best_hits = bucket, hits
    return s + best_hits, best_bucket


N_CANDIDATES = 6


def pick_heuristic(items, n=None):
    """Return up to n (default N_CANDIDATES): best story per bucket first (distinctness),
    then fill with runners-up across buckets, ordered so the top 3 remain a
    distinct default set."""
    scored = []
    for it in items:
        s, bucket = score(it)
        if not bucket:
            continue
        it["_score"], it["category"] = round(s, 1), bucket
        scored.append(it)
    scored.sort(key=lambda x: -x["_score"])
    n = n or N_CANDIDATES
    winners, used_buckets, used = [], set(), set()
    for it in scored:  # pass 1: one per bucket
        if it["category"] not in used_buckets:
            winners.append(it)
            used_buckets.add(it["category"])
            used.add(id(it))
        if len(winners) == n:
            break
    for it in scored:  # pass 2: fill remaining slots with runners-up
        if len(winners) == n:
            break
        if id(it) not in used:
            winners.append(it)
            used.add(id(it))
    for w in winners:
        w["blurb"] = (w["summary"][:220].rsplit(" ", 1)[0] + "…") if len(w["summary"]) > 220 else w["summary"]
    return winners, None


def pick_claude(items):
    """Optional: let Claude curate. Falls back to heuristic on any failure."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    pool = sorted(items, key=lambda i: -score(i)[0])[:30]
    listing = "\n".join(f"{n}. [{i['source']}] {i['title']} — {i['summary'][:150]}"
                        for n, i in enumerate(pool))
    prompt = (
        "You are curating a daily nuclear-energy newsboard for a civil engineering "
        "consulting audience. From the numbered candidates below, pick the SIX most "
        "consequential, mutually DISTINCT stories mapping the nuclear renaissance, "
        "ordered best-first (mix policy, deployment, capital, fuel, global — never two "
        "versions of one story, never opinion/stock-picking content, and NOTHING about "
        "nuclear weapons, warheads, or military strike capability — civilian energy only). "
        "Respond ONLY with JSON: {\"trend_note\": \"<one sentence tying the three together>\", "
        "\"picks\": [{\"index\": <n>, \"category\": \"<2-3 word label>\", "
        "\"blurb\": \"<25-40 word neutral blurb>\"}]}\n\n" + listing
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        data = json.loads(re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M))
        winners = []
        for p in data["picks"][:6]:
            it = pool[p["index"]]
            it["category"], it["blurb"] = p["category"], p["blurb"]
            winners.append(it)
        return winners, data.get("trend_note")
    except Exception as e:
        print(f"  Claude curation failed, using heuristic: {e}", file=sys.stderr)
        return None


def decode_gnews_url(gn_url):
    """Resolve a news.google.com/rss/articles link to the real article URL via
    Google's internal batchexecute endpoint. Best effort — returns None on any
    change to the (undocumented) protocol."""
    m = re.search(r"articles/([^?/]+)", gn_url)
    if not m:
        return None
    art_id = m.group(1)
    try:
        s = requests.Session()
        s.headers.update(UA)
        r = s.get(f"https://news.google.com/articles/{art_id}", timeout=TIMEOUT)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sg and ts):
            return None
        payload = (
            '[[["Fbv4je","[\\"garturlreq\\",[[\\"X\\",\\"X\\",[\\"en-US\\",\\"US\\"],'
            'null,null,1,1,\\"US:en\\",null,180,null,null,null,null,null,0,null,null,[1,8]],'
            '\\"en-US\\",\\"US\\",1,[2,3,4,8],1,0,\\"655000234\\",0,0,null,0],'
            f'\\"{art_id}\\",{ts.group(1)},\\"{sg.group(1)}\\"]",null,"generic"]]]'
        )
        r2 = s.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                    data={"f.req": payload},
                    headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
                    timeout=TIMEOUT)
        m2 = re.search(r'(https?://(?!news\.google|www\.google)[^\\"\s]+)', r2.text)
        return normalize_url(m2.group(1)) if m2 else None
    except Exception:
        return None


DEAD_STATUSES = {404, 410}
STALE_DAYS = 5  # article's own date older than this → treat as dead


def og_meta(url):
    """Return (image, description, resolved_url, dead). dead=True only for
    definitive misses (404/410, DNS/connection failure) — bot-walls (403/429)
    and timeouts keep the link, since it usually works for a human."""
    img = desc = None
    if "news.google.com" in url:
        real = decode_gnews_url(url)
        if not real:
            return None, None, url, False  # unverifiable, assume alive
        url = real
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in DEAD_STATUSES:
            return None, None, url, True
        final, text = normalize_url(r.url), r.text
        soup = BeautifulSoup(text, "html.parser")
        page_title = (soup.title.get_text() if soup.title else "").lower()
        if "404" in page_title or "page not found" in page_title:
            return None, None, final, True   # soft 404: server said 200, page says no
        # stale check: trust the article's own date over the feed's entry date
        # (Google News re-indexes old articles with fresh timestamps)
        page_date = None
        tag = (soup.find("meta", {"property": "article:published_time"})
               or soup.find("meta", {"name": "article:published_time"})
               or soup.find("meta", {"itemprop": "datePublished"}))
        if tag and tag.get("content"):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", tag["content"])
            if m:
                page_date = m.group(1)
        if not page_date:  # JSON-LD
            m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', text)
            if m:
                page_date = m.group(1)
        if not page_date:  # <time datetime="...">
            t_el = soup.find("time", attrs={"datetime": True})
            if t_el:
                m = re.match(r"(\d{4}-\d{2}-\d{2})", t_el["datetime"])
                if m:
                    page_date = m.group(1)
        if not page_date:  # date baked into the URL path, e.g. /2026/03/04/
            m = re.search(r"/(20\d\d)/(\d{1,2})/(?:(\d{1,2})/)?", final)
            if m:
                page_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3) or 15):02d}"
        if page_date:
            try:
                pd = datetime.strptime(page_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - pd).days > STALE_DAYS:
                    return None, None, final, True   # page says it's old news
            except ValueError:
                pass
        # redirect-to-home soft 404: a deep article URL bounced to a shallow index
        from urllib.parse import urlparse
        req_depth = len([s for s in urlparse(url).path.split("/") if s])
        fin_depth = len([s for s in urlparse(final).path.split("/") if s])
        if req_depth >= 2 and fin_depth <= 1 and normalize_url(final.rstrip("/")) != normalize_url(url.rstrip("/")):
            return None, None, final, True
        for sel, attr in ((("meta", {"property": "og:image"}), "content"),
                          (("meta", {"name": "twitter:image"}), "content")):
            tag = soup.find(*sel)
            v = tag.get(attr, "") if tag else ""
            if v.startswith("http") and "googleusercontent" not in v and "gstatic" not in v:
                img = v
                break
        tag = soup.find("meta", {"property": "og:description"}) or soup.find("meta", {"name": "description"})
        if tag and tag.get("content"):
            desc = clean(tag["content"])[:300]
        if not desc or len(desc) < 60:
            title_words = {w for w in re.findall(r"[a-z]{4,}", (soup.title.string or "").lower())} if soup.title and soup.title.string else set()
            best, best_hits = None, 1  # require >=2 title-word hits
            for p in soup.find_all("p")[:25]:
                text = clean(p.get_text())
                if len(text) < 100 or text.lower().startswith(("cookie", "subscribe", "sign up")):
                    continue
                hits = sum(1 for w in title_words if w in text.lower())
                if hits > best_hits:
                    best, best_hits = text, hits
            if best:
                desc = best[:300].rsplit(" ", 1)[0]
        return img, desc, final, False
    except requests.exceptions.ConnectionError:
        return None, None, url, True   # DNS failure / refused — dead
    except Exception:
        return None, None, url, False  # timeout etc. — keep


def send_email(payload, date_str):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if not (host and to):
        print("  SMTP not configured — skipping email.")
        return
    repo = os.environ.get("REPO_SLUG", "")
    gh_link = f"https://github.com/{repo}/actions/workflows/publish.yml" if repo else ""
    # No-GitHub approval: HMAC-tokenized link to approve.html, verified by the
    # Cloudflare Worker. Falls back to the GitHub workflow UI if no secret set.
    secret = os.environ.get("APPROVAL_SECRET")
    approve_page = ""
    if secret and repo:
        owner, name = repo.split("/", 1)
        token = hmac_mod.new(secret.encode(), date_str.encode(), hashlib.sha256).hexdigest()
        approve_page = f"https://{owner.lower()}.github.io/{name}/approve.html?t={token}"

    def card(n, it):
        img = (f'<img src="{it["image"]}" width="100%" style="border-radius:6px;display:block;margin-bottom:8px;">'
               if it.get("image") else
               '<div style="background:#232021;border-radius:6px;height:110px;text-align:center;'
               'line-height:110px;color:#C4A046;font-size:34px;margin-bottom:8px;">&#9883;</div>')
        return (
            f'<td valign="top" style="padding:10px;width:33%;">'
            f'<div style="display:inline-block;background:#C4A046;color:#2D2A2B;font-family:monospace;'
            f'font-weight:bold;font-size:13px;padding:2px 10px;border-radius:3px;margin-bottom:8px;">STORY {n}</div>'
            f'{img}'
            f'<div style="font-family:monospace;font-size:11px;color:#C4A046;">{html.escape(it["category"].upper())} &middot; {html.escape(it["source"])}</div>'
            f'<div style="font-family:Georgia,serif;font-size:16px;color:#F0EBE3;margin:6px 0;">'
            f'<a href="{it["url"]}" style="color:#FFD572;text-decoration:none;">{html.escape(it["title"])}</a></div>'
            f'<div style="font-family:Arial;font-size:13px;color:#BDB6AE;">{html.escape(it["blurb"])}</div></td>'
        )

    items = payload["items"]
    rows = ""
    for start in range(0, len(items), 3):
        rows += "<tr>" + "".join(card(n + 1, it) for n, it in enumerate(items[start:start + 3], start)) + "</tr>"
    trend = f'<p style="font-family:Georgia,serif;color:#F0EBE3;font-style:italic;">{html.escape(payload["trend_note"])}</p>' if payload.get("trend_note") else ""
    body = (
        f'<div style="background:#2D2A2B;padding:24px;">'
        f'<h2 style="font-family:monospace;color:#FFD572;letter-spacing:2px;">NUCLEAR NEWSBOARD &middot; {date_str}</h2>'
        f'<p style="font-family:Arial;font-size:13px;color:#BDB6AE;">Six candidates below &mdash; pick the three that make the board.</p>{trend}'
        f'<table style="border-collapse:collapse;max-width:860px;">{rows}</table>'
        f'<div style="margin-top:22px;padding:16px;background:#232021;border-radius:6px;max-width:820px;">'
        f'<div style="font-family:monospace;font-size:12px;color:#FFD572;letter-spacing:1px;margin-bottom:8px;">TO PUBLISH</div>'
        f'<div style="font-family:Arial;font-size:13px;color:#F0EBE3;line-height:1.6;">'
        + (
            f'<a href="{approve_page}" style="display:inline-block;background:#C4A046;color:#2D2A2B;'
            f'padding:11px 22px;font-family:monospace;font-weight:bold;text-decoration:none;'
            f'border-radius:5px;">Review &amp; pick 3 &rarr;</a>'
            f'<div style="margin-top:8px;color:#BDB6AE;">No GitHub account needed &mdash; tick three stories, hit publish.</div>'
            if approve_page else
            f'1. <a href="{gh_link}" style="color:#FFD572;">Open the publish workflow</a> and click <b>Run workflow</b>.<br>'
            f'2. In the <b>picks</b> box, enter your three story numbers, e.g. <span style="font-family:monospace;background:#2D2A2B;padding:1px 6px;border-radius:3px;color:#FFD572;">1,4,6</span>. '
            f'Leave it as 1,2,3 to take the default set.<br>'
            f'3. Run &mdash; the board updates in about a minute.'
        )
        + f'</div></div>'
        f'<p style="font-family:monospace;font-size:11px;color:#8A8280;margin-top:14px;">Do nothing and nothing publishes. Blurbs can be edited in pending.json before running.</p></div>'
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Nuclear Newsboard — {date_str} — pick 3 of 6"
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", "newsboard"))
    msg["To"] = to
    msg.attach(MIMEText(body, "html"))
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.sendmail(msg["From"], [to], msg.as_string())
    print(f"  emailed picks to {to}")


def main():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reshuffle = os.environ.get("RESHUFFLE", "").lower() in ("1", "true", "yes")
    if reshuffle:
        print("RESHUFFLE — excluding everything already shown today")
    print("Collecting feeds…")
    items = collect()
    print(f"  {len(items)} candidates after dedupe")
    shown_t, shown_u = load_shown(today_str, include_today=reshuffle)
    if shown_t or shown_u:
        before = len(items)
        items = [i for i in items
                 if norm_title(i["title"]) not in shown_t and i["url"] not in shown_u]
        print(f"  {before - len(items)} excluded as already shown on previous days")
    # Ordered candidate queue: Claude's picks first (if any), then the
    # heuristic ranking as backfill for any that turn out to be dead links.
    claude_result = pick_claude(items)
    trend_note = claude_result[1] if claude_result else None
    queue, seen_keys = [], set()
    for it in (claude_result[0] if claude_result else []):
        queue.append(it)
        seen_keys.add(norm_title(it["title"]))
    for it in pick_heuristic(items, n=N_CANDIDATES + 8)[0]:
        if norm_title(it["title"]) not in seen_keys:
            queue.append(it)
            seen_keys.add(norm_title(it["title"]))

    def too_similar(a, b):
        """Same-event guard: heavy word overlap between titles."""
        wa = {w for w in re.findall(r"[a-z0-9]{4,}", norm_title(a))}
        wb = {w for w in re.findall(r"[a-z0-9]{4,}", norm_title(b))}
        if not wa or not wb:
            return False
        return len(wa & wb) / min(len(wa), len(wb)) >= 0.5

    print("Validating links + fetching images…")
    winners = []
    for it in queue:
        if len(winners) == N_CANDIDATES:
            break
        if any(too_similar(it["title"], w["title"]) for w in winners):
            print(f"  SIMILAR, skipping: {it['title'][:60]}")
            continue
        img, desc, final_url, dead = og_meta(it["url"])
        if dead:
            print(f"  DEAD LINK, skipping: {it['title'][:60]} ({it['url'][:60]})")
            continue
        it["image"] = img
        if "news.google.com" not in final_url:
            it["url"] = final_url
        blurb = it.get("blurb", "")
        junk = (not blurb or norm_title(blurb).startswith(norm_title(it["title"])[:40])
                or len(blurb) < 40)
        if junk and desc:
            it["blurb"] = (desc[:220].rsplit(" ", 1)[0] + "\u2026") if len(desc) > 220 else desc
        elif junk:
            it["blurb"] = f"Full story at {it['source']}."
        for k in ("_weight", "_score", "summary"):
            it.pop(k, None)
        winners.append(it)
        print(f"  [{it['category']}] {it['title'][:70]}")
    if len(winners) < N_CANDIDATES:
        print(f"  WARNING: only {len(winners)} live candidates today.", file=sys.stderr)
    date_str = today_str
    save_shown(date_str, winners, append=reshuffle)
    payload = {
        "date": date_str,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trend_note": trend_note,
        "items": winners,
    }
    with open("pending.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("Wrote pending.json")
    send_email(payload, date_str)


if __name__ == "__main__":
    main()
