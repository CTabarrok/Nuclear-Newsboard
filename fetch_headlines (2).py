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
            if when and when < cutoff:
                continue
            pub = source
            if source == "Google News" and e.get("source"):
                pub = clean(e.source.get("title", source))
                pub = re.split(r"\s+[-\u2013\u2014]{1,2}\s+", pub)[0].strip() or pub
            body_text = title + " " + clean(e.get("summary", ""))
            if not is_nuclear(body_text) or is_weapons(body_text):
                continue
            item = {
                "title": strip_publisher(title),
                "url": e.get("link", ""),
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
        s += max(0, 6 - age_h / 8)
    best_bucket, best_hits = None, 0
    for bucket, words in BUCKETS.items():
        hits = sum(1 for w in words if w in text)
        if hits > best_hits:
            best_bucket, best_hits = bucket, hits
    return s + best_hits, best_bucket


N_CANDIDATES = 6


def pick_heuristic(items):
    """Return up to N_CANDIDATES: best story per bucket first (distinctness),
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
    winners, used_buckets, used = [], set(), set()
    for it in scored:  # pass 1: one per bucket
        if it["category"] not in used_buckets:
            winners.append(it)
            used_buckets.add(it["category"])
            used.add(id(it))
        if len(winners) == N_CANDIDATES:
            break
    for it in scored:  # pass 2: fill remaining slots with runners-up
        if len(winners) == N_CANDIDATES:
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
        return m2.group(1) if m2 else None
    except Exception:
        return None


def og_meta(url):
    """Return (image, description, resolved_url) — best effort."""
    img = desc = None
    if "news.google.com" in url:
        real = decode_gnews_url(url)
        if not real:
            return None, None, url
        url = real
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        final, text = r.url, r.text
        soup = BeautifulSoup(text, "html.parser")
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
        return img, desc, final
    except Exception:
        return None, None, url


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
        f'1. <a href="{approve}" style="color:#FFD572;">Open the publish workflow</a> and click <b>Run workflow</b>.<br>'
        f'2. In the <b>picks</b> box, enter your three story numbers, e.g. <span style="font-family:monospace;background:#2D2A2B;padding:1px 6px;border-radius:3px;color:#FFD572;">1,4,6</span>. '
        f'Leave it as 1,2,3 to take the default set.<br>'
        f'3. Run &mdash; the board updates in about a minute.</div></div>'
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
    print("Collecting feeds…")
    items = collect()
    print(f"  {len(items)} candidates after dedupe")
    result = pick_claude(items) or pick_heuristic(items)
    winners, trend_note = result
    if len(winners) < 6:
        print(f"  WARNING: only {len(winners)} candidates today.", file=sys.stderr)
    print("Fetching images + descriptions…")
    for it in winners:
        img, desc, final_url = og_meta(it["url"])
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
        print(f"  [{it['category']}] {it['title'][:70]}")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
