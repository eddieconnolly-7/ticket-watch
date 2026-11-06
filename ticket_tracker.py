#!/usr/bin/env python3
"""
Ticket tracker. Run once a day:
  1. Pulls price data for your watchlist from Ticketmaster (default) or SeatGeek
  2. Logs them to prices.db (SQLite)
  3. Scores each game: BUY / WAIT / HOLD
  4. Writes dashboard.html and emails a digest

Env vars:
  TICKETMASTER_API_KEY required for source=ticketmaster (free at developer.ticketmaster.com)
  SEATGEEK_CLIENT_ID   required only for source=seatgeek (free at seatgeek.com/build)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO   optional, enables email
  DASHBOARD_URL        optional, adds a dashboard link to the email
  NTFY_TOPIC           optional, phone push alerts via the free ntfy app (ntfy.sh)
Usage: python ticket_tracker.py [--no-email] [--demo]
  --demo sends a sample digest (fake games) to test email and phone delivery
"""
import json, os, sqlite3, smtplib, ssl, sys
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
DB, CFG, OUT = (os.path.join(HERE, f) for f in ("prices.db", "config.json", "dashboard.html"))


# ---------- 1. collect ----------
def get_json(base, params):
    with urlopen(base + "?" + urlencode(params), timeout=30) as r:
        return json.load(r)


def ticketmaster_events(query, cfg):
    p = {"apikey": os.environ["TICKETMASTER_API_KEY"], "keyword": query,
         "classificationName": "sports", "size": 100, "sort": "date,asc"}
    if cfg.get("venue_state"):
        p["stateCode"] = cfg["venue_state"]
    data = get_json("https://app.ticketmaster.com/discovery/v2/events.json", p)
    for e in data.get("_embedded", {}).get("events", []):
        pr = e.get("priceRanges") or []
        if not pr or "parking" in e["name"].lower():
            continue
        ven = (e.get("_embedded", {}).get("venues") or [{}])[0]
        d = e["dates"]["start"]
        yield dict(id=e["id"], title=e["name"], venue=ven.get("name", ""),
                   state=(ven.get("state") or {}).get("stateCode"),
                   when=d["localDate"] + "T" + (d.get("localTime") or "00:00:00"),
                   url=e["url"], low=min(x["min"] for x in pr),
                   avg=sum(x["min"] + x["max"] for x in pr) / (2 * len(pr)), listings=None)


def seatgeek_events(query, cfg):
    p = {"client_id": os.environ["SEATGEEK_CLIENT_ID"], "q": query, "per_page": 100,
         "sort": "datetime_utc.asc", "taxonomies.name": "sports"}
    for e in get_json("https://api.seatgeek.com/2/events", p)["events"]:
        s = e.get("stats") or {}
        if not s.get("lowest_price"):
            continue
        yield dict(id=e["id"], title=e["title"], venue=e["venue"]["name"],
                   state=e["venue"].get("state"), when=e["datetime_local"], url=e["url"],
                   low=s["lowest_price"], avg=s.get("average_price"),
                   listings=s.get("listing_count"))


SOURCES = {"ticketmaster": ticketmaster_events, "seatgeek": seatgeek_events}


def collect(cfg):
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS snap(
        event_id TEXT, title TEXT, venue TEXT, event_dt TEXT, url TEXT,
        ts TEXT, low REAL, avg REAL, listings INT)""")
    fetch = SOURCES[cfg.get("source", "ticketmaster")]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = set()
    for item in cfg["watchlist"]:
        for e in fetch(item["query"], cfg):
            if e["id"] in seen:
                continue
            if cfg.get("venue_state") and e["state"] != cfg["venue_state"]:
                continue
            seen.add(e["id"])
            db.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,?,?,?)",
                       (str(e["id"]), e["title"], e["venue"], e["when"], e["url"], now,
                        e["low"], e["avg"], e["listings"]))
    db.commit()
    return db


# ---------- 2. predict ----------
def predict(lows, days):
    """Heuristic verdict from this game's own price history. Gets sharper with more data."""
    cur = lows[-1]
    if len(lows) < 4:
        return "COLLECTING", f"{len(lows)} day(s) of history, need 4+"
    window = lows[-7:]
    trend = (cur - window[0]) / window[0]
    floor = min(lows)
    if days <= 2:
        if cur <= sum(lows) / len(lows):
            return "BUY", "Game is near and the price is below its average"
        return "WAIT", "Prices often dip in the last hours, but it's a gamble"
    if trend <= -0.05:
        return "WAIT", f"Falling {abs(trend):.0%} this week"
    if cur <= floor * 1.03:
        return "BUY", f"At its lowest seen (${floor:,.0f})"
    if trend >= 0.05:
        return "BUY", f"Rising {trend:.0%} this week"
    return "HOLD", "No clear signal yet"


def analyze(db, cfg):
    rows = db.execute("SELECT event_id,title,venue,event_dt,url,ts,low,listings "
                      "FROM snap ORDER BY ts").fetchall()
    by_event = {}
    for r in rows:
        by_event.setdefault(r[0], []).append(r)
    today, results = datetime.now(), []
    for h in by_event.values():
        last = h[-1]
        days = (datetime.fromisoformat(last[3]) - today).days
        if days < 0 or days > cfg.get("max_days_out", 90):
            continue
        if (datetime.now(timezone.utc) - datetime.fromisoformat(last[5])).days > 3:
            continue  # stale: event dropped off the API
        lows = [x[6] for x in h]
        verdict, why = predict(lows, days)
        results.append(dict(title=last[1], venue=last[2], when=last[3], url=last[4],
                            days=days, price=lows[-1], listings=last[7],
                            lows=lows[-30:], verdict=verdict, why=why))
    return sorted(results, key=lambda r: (r["verdict"] != "BUY", r["days"]))


# ---------- 3. outputs ----------
def spark(v):
    if len(v) < 2:
        return ""
    lo, hi = min(v), max(v)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * 100 / (len(v) - 1):.1f},{22 - (x - lo) / span * 20:.1f}"
                   for i, x in enumerate(v))
    return (f'<svg viewBox="0 0 100 24" width="100" height="24" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')


def dashboard(results):
    rows = ""
    for r in results:
        d = datetime.fromisoformat(r["when"])
        rows += (f'<tr><td><a href="{escape(r["url"])}">{escape(r["title"])}</a>'
                 f'<small>{escape(r["venue"])}</small></td>'
                 f'<td>{d:%a %b %-d, %-I:%M %p}<small>in {r["days"]} days</small></td>'
                 f'<td class="num">${r["price"]:,.0f}<small>{f"{r['listings']} listings" if r["listings"] else ""}</small></td>'
                 f'<td class="trend">{spark(r["lows"])}</td>'
                 f'<td><span class="v {r["verdict"].lower()}">{r["verdict"].title()}</span>'
                 f'<small>{escape(r["why"])}</small></td></tr>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ticket watch</title>
<style>
:root{{--bg:#f6f4ef;--fg:#1b1f1d;--mute:#6a706c;--line:#d9d6cc;--buy:#1c7a4b;--wait:#b3541e;--hold:#6a706c}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14181a;--fg:#e8e6df;--mute:#9aa09b;--line:#2b3235;--buy:#4cc38a;--wait:#f08c5a;--hold:#9aa09b}}}}
body{{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);font:16px/1.45 Georgia,'Times New Roman',serif}}
main{{max-width:60rem;margin:0 auto}} h1{{font-size:2rem;margin:0}} p.sub{{color:var(--mute);margin:.25rem 0 1.5rem}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:42rem}}
th{{text-align:left;font-weight:normal;color:var(--mute);border-bottom:2px solid var(--fg);padding:.4rem .6rem}}
td{{padding:.7rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}}
small{{display:block;color:var(--mute);font-size:.82rem}} a{{color:inherit;text-decoration-thickness:1px}}
.num{{font-size:1.25rem}} .trend{{color:var(--mute)}}
.v{{font-weight:bold;font-size:1.05rem}} .buy{{color:var(--buy)}} .wait{{color:var(--wait)}} .hold,.collecting{{color:var(--hold)}}
</style></head><body><main>
<h1>Ticket watch</h1>
<p class="sub">Updated {datetime.now():%b %-d, %-I:%M %p}. Lowest resale price per game, best buys first.</p>
<div class="wrap"><table><thead><tr><th>Game</th><th>Date</th><th>Lowest price</th><th>Last 30 days</th><th>Verdict</th></tr></thead>
<tbody>{rows or '<tr><td colspan="5">No games found yet.</td></tr>'}</tbody></table></div>
</main></body></html>"""
    with open(OUT, "w") as f:
        f.write(page)


def build_digest(results):
    """Returns (subject, plain_text, html) for the daily email."""
    buys = [r for r in results if r["verdict"] == "BUY"]
    waits = [r for r in results if r["verdict"] == "WAIT"][:5]
    link = os.environ.get("DASHBOARD_URL", "")
    today = f"{datetime.now():%A, %b %-d}"
    collecting = sum(r["verdict"] == "COLLECTING" for r in results)
    if buys:
        headline = f"{len(buys)} game(s) look like a good time to buy."
    elif collecting and not waits:
        headline = f"Still collecting price history on {collecting} game(s). Verdicts start in a few days."
    else:
        headline = "No clear buys today."

    def row(r, color):
        d = datetime.fromisoformat(r["when"])
        return (f'<tr><td style="padding:12px 0;border-bottom:1px solid #d9d6cc">'
                f'<a href="{escape(r["url"])}" style="color:#1b1f1d;font-size:17px">{escape(r["title"])}</a><br>'
                f'<span style="color:#6a706c;font-size:13px">{d:%a %b %-d}, {escape(r["venue"])}</span><br>'
                f'<span style="color:{color};font-weight:bold;font-size:16px">from ${r["price"]:,.0f}</span> '
                f'<span style="color:#6a706c;font-size:13px">{escape(r["why"])}</span></td></tr>')

    def section(title, items, color):
        if not items:
            return ""
        return (f'<h2 style="font-size:18px;margin:24px 0 0">{title}</h2>'
                f'<table width="100%" cellpadding="0" cellspacing="0">'
                + "".join(row(r, color) for r in items) + "</table>")

    html = ('<div style="max-width:560px;margin:0 auto;padding:24px;background:#f6f4ef;'
            'font-family:Georgia,\'Times New Roman\',serif;color:#1b1f1d">'
            f'<h1 style="font-size:26px;margin:0">Ticket watch</h1>'
            f'<p style="color:#6a706c;margin:4px 0 0">{today}. {headline}</p>'
            + section("Good time to buy", buys, "#1c7a4b")
            + section("Worth waiting on", waits, "#b3541e")
            + (f'<p style="margin-top:24px"><a href="{escape(link)}" style="color:#1b1f1d">Open the full dashboard</a></p>'
               if link else "") + "</div>")

    text = [f"Ticket watch, {today}", headline, ""]
    for title, items in (("Good time to buy:", buys), ("Worth waiting on:", waits)):
        if items:
            text.append(title)
            text += [f"- {r['title']} ({r['when'][:10]}): from ${r['price']:,.0f}. {r['why']}\n  {r['url']}"
                     for r in items]
            text.append("")
    if link:
        text.append(f"Dashboard: {link}")
    return f"Ticket watch: {len(buys)} buy signal(s)", "\n".join(text), html


def email_digest(results):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        print("Email skipped: set SMTP_HOST and EMAIL_TO to enable.")
        return
    subject, text, html = build_digest(results)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_USER", to), to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or 587)) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print("Digest emailed.")


def push_digest(results):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("Phone push skipped: set NTFY_TOPIC to enable.")
        return
    buys = [r for r in results if r["verdict"] == "BUY"]
    body = "\n".join(f"{r['title']}: ${r['price']:,.0f} ({r['why']})" for r in buys[:4]) \
        or "No clear buys today."
    req = Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                  headers={"Title": f"Ticket watch: {len(buys)} buy signal(s)"})
    if os.environ.get("DASHBOARD_URL"):
        req.add_header("Click", os.environ["DASHBOARD_URL"])
    urlopen(req, timeout=30).read()
    print("Phone push sent.")


def demo_results():
    def g(title, venue, days, price, verdict, why):
        d = datetime.now().replace(hour=19, minute=10, second=0, microsecond=0)
        return dict(title=title + " (sample)", venue=venue, when=d.replace(day=d.day).isoformat(),
                    url="https://seatgeek.com", days=days, price=price, listings=1000,
                    lows=[price], verdict=verdict, why=why)
    return [g("Patriots vs. Bills", "Gillette Stadium", 23, 207, "BUY", "Rising 9% this week"),
            g("Celtics vs. Knicks", "TD Garden", 31, 123, "WAIT", "Falling 8% this week")]


if __name__ == "__main__":
    cfg = json.load(open(CFG))
    if "--demo" in sys.argv:
        demo = demo_results()
        email_digest(demo)
        push_digest(demo)
        sys.exit(0)
    db = collect(cfg)
    results = analyze(db, cfg)
    dashboard(results)
    print(f"Dashboard written: {OUT} ({len(results)} games)")
    if "--no-email" not in sys.argv:
        email_digest(results)
        push_digest(results)
