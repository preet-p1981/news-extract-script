#!/usr/bin/env python3
"""
India Daily Stock News -> Email
---------------------------------
Fetches today's (IST) Indian finance news, ranks it by stock-market
relevance, and emails you the top 20-30 stories. Built to run unattended
on a server via cron at a fixed time every day.

Setup:
    pip install feedparser

Required environment variables (set these on your server, NOT in this file):
    SMTP_HOST      e.g. smtp.gmail.com
    SMTP_PORT      e.g. 587
    SMTP_USER      the sending email address
    SMTP_PASS      app password (see notes below, NOT your normal password)
    MAIL_TO        where to send the digest (can be same as SMTP_USER)
    MAIL_FROM      optional, defaults to SMTP_USER

Gmail note: Gmail blocks normal passwords for SMTP. Enable 2-Step
Verification on the sending account, then create an "App Password" at
https://myaccount.google.com/apppasswords and use that as SMTP_PASS.
Any other provider (Outlook, Zoho, a transactional service like SendGrid's
SMTP relay) works the same way with their own host/port.

Test manually before scheduling it:
    python india_daily_stock_news_mailer.py --dry-run     # prints, no email
    python india_daily_stock_news_mailer.py                # sends a real email

Deploy with cron (see the deployment notes at the bottom of this file).
"""

import argparse
import datetime
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import feedparser
except ImportError:
    sys.exit("Missing dependency. Install it with:\n    pip install feedparser")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

FEEDS = {
    "Economic Times - Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Economic Times - Business": "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
    "Moneycontrol - Business": "https://www.moneycontrol.com/rss/business.xml",
    "Moneycontrol - Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Business Standard - Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard - Finance": "https://www.business-standard.com/rss/finance-103.rss",
    "LiveMint - Markets": "https://www.livemint.com/rss/markets",
    "LiveMint - Money": "https://www.livemint.com/rss/money",
    "Zerodha Pulse (aggregator)": "https://pulse.zerodha.com/feed.php",
}

CATEGORY_WEIGHTS = {
    "Markets / Indices":       (3, ["sensex", "nifty", "bse", "nse", "fpi", "fii", "dii", "rally", "correction", "index", "bull", "bear"]),
    "RBI / Monetary Policy":   (3, ["rbi", "reserve bank", "repo rate", "mpc", "monetary policy", "inflation", "cpi", "wpi", "rate cut", "rate hike"]),
    "Corporate / Earnings":    (3, ["q1", "q2", "q3", "q4", "profit", "net income", "results", "earnings", "acquisition", "merger", "stake", "guidance"]),
    "Banking / NBFC":          (2, ["bank", "nbfc", "hdfc", "icici", "sbi", "axis bank", "kotak", "loan book", "credit growth", "npa"]),
    "IPO / Listings":          (2, ["ipo", "listing", "offer for sale", "public issue", "debut", "subscription", "gmp"]),
    "Regulatory / SEBI":       (2, ["sebi", "regulation", "compliance", "penalty", "probe", "fraud", "enforcement directorate", "raid", "ban"]),
    "Commodities / Energy":    (2, ["gold", "silver", "crude", "oil price", "opec", "bullion", "commodity"]),
    "Global / Macro":          (2, ["fed", "federal reserve", "tariff", "dollar", "rupee", "global markets", "us stocks", "china", "gdp"]),
    "Personal Finance / Tax":  (0, ["income tax", "advance tax", "sip", "mutual fund", "savings", "itr", "gst calculator", "how much should"]),
}

POSITIVE_WORDS = ["surge", "surges", "rally", "rallies", "gain", "gains", "jump", "jumps", "rise", "rises",
                   "soar", "soars", "record high", "beat estimates", "upgrade", "upgraded", "profit", "growth",
                   "expand", "strong", "boost", "outperform", "recovery", "rebound", "bullish", "approve"]
NEGATIVE_WORDS = ["fall", "falls", "decline", "declines", "drop", "drops", "slump", "plunge", "plunges", "crash",
                   "downgrade", "downgraded", "loss", "losses", "fraud", "probe", "raid", "raids", "penalty",
                   "fine", "sell-off", "selloff", "outflow", "bearish", "weak", "warns", "warning", "default", "scam"]


# ---------- News fetching & ranking (same logic as the standalone version) ----------

def fetch_all():
    items = []
    for source, url in FEEDS.items():
        try:
            parsed = feedparser.parse(url)
            for e in parsed.entries:
                items.append({
                    "source": source,
                    "title": e.get("title", "").strip(),
                    "link": e.get("link", ""),
                    "summary": e.get("summary", "").strip(),
                    "published_struct": e.get("published_parsed") or e.get("updated_parsed"),
                })
        except Exception as err:
            print(f"[warn] Could not fetch {source}: {err}", file=sys.stderr)
    return items


def to_ist_date(struct_time):
    if not struct_time:
        return None
    utc_dt = datetime.datetime(*struct_time[:6], tzinfo=datetime.timezone.utc)
    return utc_dt.astimezone(IST)


def dedupe(items):
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower()[:60]
        if key in seen or not it["title"]:
            continue
        seen.add(key)
        unique.append(it)
    return unique


def stock_relevance_score(text):
    text_l = text.lower()
    best_weight, best_cat = 0, "General / Other"
    for cat, (weight, keywords) in CATEGORY_WEIGHTS.items():
        if any(kw in text_l for kw in keywords) and weight > best_weight:
            best_weight, best_cat = weight, cat
    pos = sum(1 for w in POSITIVE_WORDS if w in text_l)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text_l)
    strength = abs(pos - neg)
    score = best_weight * 10 + strength
    sentiment = "Positive" if pos > neg else "Negative" if neg > pos else "Neutral"
    return score, best_cat, sentiment


def build_digest(target_date, n):
    raw = dedupe(fetch_all())
    todays = []
    for it in raw:
        ist_dt = to_ist_date(it["published_struct"])
        if ist_dt is None or ist_dt.date() != target_date:
            continue
        it["ist_time"] = ist_dt.strftime("%H:%M")
        text = f"{it['title']} {it['summary']}"
        it["score"], it["category"], it["sentiment"] = stock_relevance_score(text)
        todays.append(it)
    todays.sort(key=lambda x: x["score"], reverse=True)
    n = max(20, min(n, 30))
    return todays[:n]


# ---------- Formatting ----------

def to_markdown(items, target_date):
    if not items:
        return f"# India Stock-Moving News — {target_date.isoformat()}\n\nNo stories found for today yet."
    lines = [f"# India Stock-Moving News — {target_date.isoformat()}", f"_{len(items)} stories, ranked by market relevance_\n"]
    tag = {"Positive": "UP-leaning", "Negative": "DOWN-leaning", "Neutral": "Neutral"}
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. **{it['title']}**  \n"
            f"   {tag.get(it['sentiment'])} | {it['category']} | {it['ist_time']} IST | _{it['source']}_  \n"
            f"   {it['link']}"
        )
    return "\n\n".join(lines)


def to_html(items, target_date):
    if not items:
        return f"<h2>India Stock-Moving News — {target_date.isoformat()}</h2><p>No stories found for today yet.</p>"
    color = {"Positive": "#1a7f37", "Negative": "#c0392b", "Neutral": "#666666"}
    rows = []
    for i, it in enumerate(items, 1):
        c = color.get(it["sentiment"], "#666666")
        rows.append(
            f"<tr><td style='padding:8px;border-bottom:1px solid #eee;'>"
            f"<div style='font-weight:600;'>{i}. {it['title']}</div>"
            f"<div style='font-size:13px;color:{c};margin-top:2px;'>{it['sentiment']}</div>"
            f"<div style='font-size:12px;color:#888;margin-top:2px;'>{it['category']} &middot; {it['ist_time']} IST &middot; {it['source']}</div>"
            f"<div style='margin-top:4px;'><a href='{it['link']}'>Read more</a></div>"
            f"</td></tr>"
        )
    body = "".join(rows)
    return (
        f"<h2>India Stock-Moving News — {target_date.isoformat()}</h2>"
        f"<p style='color:#888;'>{len(items)} stories, ranked by market relevance</p>"
        f"<table style='width:100%;border-collapse:collapse;'>{body}</table>"
    )


# ---------- Email ----------

def send_email(subject, text_body, html_body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")
    mail_from = os.environ.get("MAIL_FROM", user)

    missing = [k for k, v in {
        "SMTP_HOST": host, "SMTP_USER": user, "SMTP_PASS": password, "MAIL_TO": mail_to
    }.items() if not v]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}. "
                  f"Set these on your server before running (see script header).")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(mail_from, [mail_to], msg.as_string())


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(description="Daily India stock news -> email")
    parser.add_argument("--n", type=int, default=25, help="Number of stories (20-30)")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD (default: today, IST)")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending an email (use this to test)")
    parser.add_argument("--save", action="store_true", help="Also save a local .md copy")
    args = parser.parse_args()

    target_date = (
        datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date else datetime.datetime.now(IST).date()
    )

    try:
        items = build_digest(target_date, args.n)
    except Exception as e:
        # Even on failure, try to notify by email so a silent cron failure doesn't go unnoticed.
        error_msg = f"India stock news digest failed to run on {target_date.isoformat()}: {e}"
        print(error_msg, file=sys.stderr)
        if not args.dry_run:
            try:
                send_email(f"[FAILED] India Stock News {target_date.isoformat()}", error_msg, f"<p>{error_msg}</p>")
            except Exception:
                pass
        sys.exit(1)

    md = to_markdown(items, target_date)
    html = to_html(items, target_date)
    subject = f"India Stock-Moving News — {target_date.isoformat()} ({len(items)} stories)"

    if args.save:
        fname = f"india_stock_news_{target_date.isoformat()}.md"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Saved to {fname}")

    if args.dry_run:
        print(md)
        print("\n[dry-run] Email not sent.")
    else:
        send_email(subject, md, html)
        print(f"Email sent to {os.environ.get('MAIL_TO')} with {len(items)} stories.")


if __name__ == "__main__":
    main()


# =====================================================================
# DEPLOYMENT NOTES (server + cron, 9 AM IST daily)
# =====================================================================
#
# 1. On your server:
#      sudo apt update && sudo apt install -y python3 python3-venv
#      mkdir -p ~/india-news && cd ~/india-news
#      python3 -m venv venv
#      source venv/bin/activate
#      pip install feedparser
#
# 2. Upload this script into ~/india-news/
#
# 3. Set environment variables. The cleanest way for cron is a small env
#    file that's NOT committed to any repo, e.g. ~/india-news/.env :
#
#      SMTP_HOST=smtp.gmail.com
#      SMTP_PORT=587
#      SMTP_USER=youraddress@gmail.com
#      SMTP_PASS=your16digitapppassword
#      MAIL_TO=youraddress@gmail.com
#
#    chmod 600 ~/india-news/.env    (restrict read access to you only)
#
# 4. Test it manually first:
#      source venv/bin/activate
#      set -a; source .env; set +a
#      python india_daily_stock_news_mailer.py --dry-run
#      python india_daily_stock_news_mailer.py          # sends a real test email
#
# 5. Check your server's timezone (cron uses the SERVER's local time):
#      timedatectl
#    If it's not IST, either set it (simplest, fine for a single-purpose box):
#      sudo timedatectl set-timezone Asia/Kolkata
#    ...or work out the UTC-equivalent time for 9 AM IST (03:30 UTC) and use
#    that in the cron schedule instead.
#
# 6. Add the cron job:
#      crontab -e
#    Add this line (adjust paths; runs 9:00 AM server-local time daily):
#
#      0 9 * * * cd /home/youruser/india-news && set -a && . ./.env && set +a && ./venv/bin/python india_daily_stock_news_mailer.py --n 30 >> cron.log 2>&1
#
# 7. Confirm it's scheduled:
#      crontab -l
#    Check cron.log the next morning to confirm it ran and the email sent.
#
# Alternative (more robust): a systemd service + timer instead of cron,
# which gives you `journalctl` logs and automatic retry-on-boot if the
# server was off at 9 AM. Ask if you want that version instead.
# =====================================================================