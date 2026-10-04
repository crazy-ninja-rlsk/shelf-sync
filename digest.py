"""Scheduled chat messages: daily sales digest (message + PDF per location) and an account balance note.

    python digest.py                       # dry run of the sales digest: message to the console, PDFs to reports/
    python digest.py --send                # send it to the default chat from the config
    python digest.py --send --chat main    # another chat key (or several, comma separated)
    python digest.py --date 2026-09-30     # another day
    python digest.py --weekly              # add the weekly summary on any day
    python digest.py --balance [--send]    # the balance note instead of the digest
    python digest.py --schedule NAME       # what a schedule from the config does (kind, chat, time)
    python digest.py --no-restore          # use the database as it is
    python digest.py --chats               # list chats the bot can see (to find chat ids)

Shared settings (storage, database) come from the sync config; the digest's own settings, texts and
queries from digest.local.json (or the DIGEST_JSON env var). Tokens: TELEGRAM_TOKEN, BALANCE_TOKEN.
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from googleapiclient.discovery import build

import sync

HERE = Path(__file__).resolve().parent
FONTS = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]
TX = {}


def load_digest_config():
    raw = os.environ.get("DIGEST_JSON") or (HERE / "digest.local.json").read_text(encoding="utf-8")
    return json.loads(raw)


def T(key, **kw):
    """Text template from the config."""
    return TX[key].format(**kw) if kw else TX[key]


# ---------- formatting ----------

def money(v):
    """12345.5 -> '12 346' (whole units everywhere)."""
    v = round(v or 0)
    return ("-" if v < 0 else "") + f"{abs(v):,.0f}".replace(",", " ")


def qty(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}".replace(".", ",")


def plural(n, forms):
    """forms = [one, few, many]"""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def checks_word(n):
    return plural(n, TX["checks"]).format(n=n)


def wd(d):
    return TX["weekdays"][d.weekday()]


def pct(part, whole):
    return part / whole * 100 if whole else 0.0


def delta(now, before):
    if not before:
        return ""
    p = (now - before) / before * 100
    arrow = "▲" if p > 0.5 else ("▼" if p < -0.5 else "≈")
    return f"{arrow} {p:+.0f}%"


def short(s, n):
    s = sync.norm(s)
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def strip_category(name, categories):
    """Goods names often start with category names: 'Books (x) Books Title' -> 'Title'."""
    name = sync.norm(name)
    again = True
    while again:
        again = False
        for c in categories:
            if name.startswith(c + " "):
                name, again = name[len(c):].lstrip(" -"), True
                break
    return name


def buyer_label(name, cfg):
    name = re.sub(r"^\(\d+\)\s*", "", sync.norm(name))
    return T("retail") if name.casefold() == cfg["retail_buyer"].casefold() else (name or "—")


def hhmm(s):
    h, m = map(int, s.split(":"))
    return dt.time(h, m)


# ---------- data ----------

class Line:
    __slots__ = ("point", "check", "no", "at", "sign", "buyer", "paycat", "qty", "price", "list_price",
                 "disc", "name", "barcode", "type_id", "good", "cost", "art", "category", "title")

    def __init__(self, r):
        (self.point, self.check, self.no, self.at, self.sign, self.buyer, self.paycat, q, p, lp, d,
         self.name, self.barcode, self.type_id, self.good, c, art) = r
        self.art = sync.norm(art)
        self.qty, self.price = float(q or 0), float(p or 0)
        self.list_price = float(lp) if lp is not None else self.price
        self.disc = float(d or 0)
        self.cost = float(c) if c else None
        self.category = self.title = ""

    @property
    def amount(self):
        return self.qty * self.price

    @property
    def list_amount(self):
        return self.qty * self.list_price

    @property
    def discount(self):
        return max(self.list_amount - self.amount, 0.0)


def read_lines(cn, dcfg, day_from, day_to):
    q = dcfg["queries"]["lines"]
    n = q.count("?") // 2
    return [Line(r) for r in cn.execute(q, *([day_from, day_to] * n)).fetchall()]


def read_payments(cn, dcfg, day_from, day_to):
    """-> {check id: {pay form: sum}}"""
    out = {}
    for inv, form, s in cn.execute(dcfg["queries"]["payments"], day_from, day_to).fetchall():
        f = out.setdefault(inv, {})
        f[str(form)] = f.get(str(form), 0.0) + float(s or 0)
    return out


def read_roots(cn, dcfg):
    """-> {category id: top level category name}"""
    rows = {r[0]: (r[1], sync.norm(r[2])) for r in cn.execute(dcfg["queries"]["categories"]).fetchall()}
    roots = {}
    for i in rows:
        cur, seen = i, set()
        while rows[cur][0] in rows and cur not in seen:
            seen.add(cur)
            cur = rows[cur][0]
        roots[i] = rows[cur][1]
    return roots


def read_map(cn, q):
    return {r[0]: r[1] for r in cn.execute(q).fetchall()}


def read_stock(cn, dcfg):
    """-> {good id: {warehouse id: qty >= 0}}"""
    out = {}
    for g, w, q in cn.execute(dcfg["queries"]["stock"]).fetchall():
        if q and q > 0:
            out.setdefault(g, {})[w] = float(q)
    return out


class Data:
    """Everything read from the db for one report day."""

    def __init__(self, cn, dcfg, day):
        self.day = day
        self.d0 = dt.datetime.combine(day, dt.time())
        self.d1 = self.d0 + dt.timedelta(days=1)
        pm0 = (day.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        start = min(dt.datetime.combine(pm0, dt.time()), self.d0 - dt.timedelta(days=35))
        pts = {p["id"] for p in dcfg["points"]}
        roots = read_roots(cn, dcfg)
        self.hist = [l for l in read_lines(cn, dcfg, start, self.d1) if l.point in pts]
        ly = self.d0 - dt.timedelta(days=364)
        self.last_year = [l for l in read_lines(cn, dcfg, ly, ly + dt.timedelta(days=1)) if l.point in pts]
        self.pays = read_payments(cn, dcfg, start, self.d1)
        self.names = read_map(cn, dcfg["queries"]["points"])
        self.stock = read_stock(cn, dcfg)
        # the day's own cost appears after the nightly recalculation, until then the last known one is used
        est = read_map(cn, dcfg["queries"]["incoming_costs"])
        est.update(read_map(cn, dcfg["queries"]["costs"]))
        self.estimated = False
        prefixes = sorted(set(roots.values()), key=len, reverse=True)
        merge = dcfg.get("category_merge", {})  # several top level categories shown as one
        for l in self.hist + self.last_year:
            l.category = merge.get(roots.get(l.type_id, "—"), roots.get(l.type_id, "—"))
            l.title = strip_category(l.name, prefixes)
            if l.art:
                # the article is shown on its own, so it is cut out of the name
                l.title = sync.norm(re.sub(r"(?<!\S)" + re.escape(l.art) + r"(?!\S)", " ", l.title)) or l.title
            if l.cost is None and est.get(l.good):
                l.cost = float(est[l.good])
                if l.at and l.at >= self.d0:
                    self.estimated = True
        self.today = self.between(self.d0, self.d1)

    def between(self, a, b, sales_only=False):
        if isinstance(a, dt.date) and not isinstance(a, dt.datetime):
            a, b = dt.datetime.combine(a, dt.time()), dt.datetime.combine(b, dt.time())
        return [l for l in self.hist if l.at and a <= l.at < b and (not sales_only or l.sign > 0)]


def sales_of(lines, point=None):
    return sum(l.amount for l in lines if l.sign > 0 and (point is None or l.point == point))


def checks_of(lines, point=None):
    return len({l.check for l in lines if l.sign > 0 and (point is None or l.point == point)})


def margin_of(lines):
    """-> (margin, revenue with known cost)"""
    known = [l for l in lines if l.sign > 0 and l.cost is not None]
    rev = sum(l.amount for l in known)
    return rev - sum(l.qty * l.cost for l in known), rev


def peak(lines):
    hours = {}
    for c, h in {(l.check, l.at.hour) for l in lines if l.sign > 0 and l.at}:
        hours[h] = hours.get(h, 0) + 1
    if not hours:
        return None
    return max(hours.items(), key=lambda x: (x[1], -x[0]))


def top_goods(lines, n):
    goods = {}
    for l in lines:
        if l.sign > 0:
            g = goods.setdefault((l.title, l.art), [0.0, 0.0])
            g[0] += l.qty
            g[1] += l.amount
    return sorted(goods.items(), key=lambda x: -x[1][1])[:n]


def art(a):
    return T("art", a=escape(a)) if a else ""


def top_lines(lines):
    return [T("top_line", i=i, art=art(a), name=escape(short(name, 44)), q=qty(q), sum=money(v))
            for i, ((name, a), (q, v)) in enumerate(top_goods(lines, 5), 1)]


def margin_text(lines):
    m, rev = margin_of(lines)
    return T("margin", m=money(m), p=f"{pct(m, rev):.0f}") if rev else ""


# ---------- daily message ----------

def point_block(dcfg, data, p, total):
    pid = p["id"]
    ps = [l for l in data.today if l.point == pid and l.sign > 0]
    pr = [l for l in data.today if l.point == pid and l.sign < 0]
    code = escape(p["code"])
    if not ps and not pr:
        return [T("pt_none", code=code)]
    s, n = sales_of(ps), checks_of(ps)
    week_ago = sales_of(data.between(data.day - dt.timedelta(days=7), data.day - dt.timedelta(days=6)), pid)
    head = T("pt_head", code=code, sum=money(s))
    if total:
        head += f" · {pct(s, total):.0f}%"
    if n:
        head += T("pt_checks", checks=checks_word(n), avg=money(s / n))
    if week_ago and dcfg.get("daily", {}).get("compare", True):
        head += f" · {delta(s, week_ago)}"
    out = [head]

    forms, cod_sum, cod = {}, 0.0, dcfg.get("cod_buyer", "").casefold()
    for c in {l.check for l in ps}:
        paid = data.pays.get(c, {})
        for f, v in paid.items():
            forms[f] = forms.get(f, 0.0) + v
        if not paid:
            cl = [l for l in ps if l.check == c]
            if cod and cod in sync.norm(cl[0].buyer).casefold():
                cod_sum += sales_of(cl)
    pay = [f"{dcfg['pay_forms'].get(f, f)} {money(v)}" for f, v in sorted(forms.items()) if round(v)]
    if cod_sum:
        pay.append(T("cod", sum=money(cod_sum)))
    if pay:
        out.append("    💳 " + " · ".join(pay))

    extra = []
    pk = peak(ps)
    if pk:
        extra.append(T("peak", h0=f"{pk[0]:02d}", h1=f"{pk[0] + 1:02d}", checks=checks_word(pk[1])))
    mt = margin_text(ps)
    if mt:
        extra.append(f"💹 {mt}")
    if extra:
        out.append("    " + " · ".join(extra))
    if pr:
        out.append(T("returns", q=qty(sum(l.qty for l in pr)), sum=money(sum(l.amount for l in pr))))

    disc = [l for l in ps if l.discount >= 0.5]
    if disc:
        out.append(T("discounts", sum=money(sum(l.discount for l in disc))))
        disc.sort(key=lambda l: -l.discount)
        limit = dcfg.get("discount_lines", 5)
        for l in disc[:limit]:
            out.append(T("disc_line", name=escape(short(l.title, 40)), base=money(l.list_price), price=money(l.price),
                         p=f"{pct(l.list_price - l.price, l.list_price):.0f}",
                         each=f" ×{qty(l.qty)}" if l.qty != 1 else ""))
        if len(disc) > limit:
            out.append(T("disc_more", n=len(disc) - limit))
    return out


def signals(dcfg, data, snap_time):
    sg = dcfg.get("signals") or {}
    out = []
    now_t = snap_time.time() if snap_time and snap_time.date() == data.day else dt.time(23, 59)
    weeks = sg.get("drop_weeks", 4)
    past = [data.day - dt.timedelta(days=7 * k) for k in range(1, weeks + 1)]

    for p in dcfg["points"]:
        pid = p["id"]
        ps = [l for l in data.today if l.point == pid and l.sign > 0]
        name = escape(p["code"])

        if pid in sg.get("retail_points", []) and sg.get("first_check_by"):
            limit = hhmm(sg["first_check_by"])
            first = min((l.at for l in ps if l.at), default=None)
            if first is None and now_t >= limit:
                out.append(T("sig_none", name=name))
                if pid in sg.get("print_always", []):
                    out.append(T("sig_print_none", name=name))
                continue
            if first and first.time() > limit:
                out.append(T("sig_first", name=name, t=f"{first:%H:%M}"))

        base = [sales_of(data.between(d, d + dt.timedelta(days=1)), pid) for d in past]
        base = [b for b in base if b]
        if base:
            avg = sum(base) / len(base)
            s = sales_of(ps)
            if s < avg * (1 - sg.get("drop_pct", 30) / 100):
                out.append(T("sig_drop", name=name, sum=money(s), p=f"{100 - pct(s, avg):.0f}", n=len(base),
                             days=plural(len(base), TX["same_days"]), avg=money(avg)))

        if pid in sg.get("print_points", []):
            pat = sg.get("print_pattern", "").casefold()
            n = len({l.check for l in ps if pat and pat in l.name.casefold()})
            if not n and pid in sg.get("print_always", []):
                out.append(T("sig_print_none", name=name))
            elif n <= sg.get("print_min", 3):
                usual = [len({l.check for l in data.between(d, d + dt.timedelta(days=1), True)
                              if l.point == pid and pat in l.name.casefold()}) for d in past]
                usual = [u for u in usual if u]
                tail = T("sig_print_usual", n=round(sum(usual) / len(usual))) if usual else ""
                out.append(T("sig_print", name=name, n=n, ops=plural(n, TX["ops"]), tail=tail))

        for l in ps:
            d = pct(l.list_price - l.price, l.list_price)
            if d >= sg.get("discount_pct", 101):
                out.append(T("sig_disc", name=name, p=f"{d:.0f}", title=escape(short(l.title, 40)),
                             base=money(l.list_price), price=money(l.price)))
    return out


def working_days_between(a, b):
    """Working days after date a up to and including date b."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def order_signals(cfg, dcfg, day, log):
    """Orders sitting in a status too long. The api has no status change time, so the order's last change
    is used: it is never earlier than the status change, so a hit is certain to be at least that old."""
    rules = dcfg.get("sd_signals") or []
    if not rules or "orders_api" not in cfg:
        return []
    try:
        orders, _ = sync.fetch_orders(cfg, sorted({str(r["status"]) for r in rules}))
    except Exception as e:
        log.status(f"orders skipped: {type(e).__name__}")
        return []
    out = []
    for r in rules:
        old = []
        for o in orders:
            if str(o.get("statusId")) != str(r["status"]) or not o.get("updateAt"):
                continue
            changed = dt.datetime.fromisoformat(o["updateAt"]).date()
            if working_days_between(changed, day) > r.get("working_days", 2):
                old.append((changed, o.get("id")))
        if old:
            old.sort()
            ids = ", ".join(f"№{i}" for _, i in old[:r.get("list", 10)]) + (" …" if len(old) > r.get("list", 10) else "")
            out.append(T("sig_sd_stale", name=escape(r["name"]), days=r.get("working_days", 2), n=len(old),
                         orders=plural(len(old), TX["orders"]), ids=ids))
    return out


def running_low(dcfg, data):
    rl = dcfg.get("running_low")
    if not rl:
        return [], []
    skip = {s.casefold() for s in rl.get("skip_categories", [])}
    since = data.d1 - dt.timedelta(days=rl.get("days", 30))
    sold, title, arts = {}, {}, {}
    for l in data.hist:
        if l.at and l.at >= since and l.sign > 0 and l.category.casefold() not in skip:
            sold[l.good] = sold.get(l.good, 0.0) + l.qty
            title[l.good] = l.title
            arts[l.good] = l.art
    code = {p["id"]: p["code"] for p in dcfg["points"]}
    low, gone = [], []
    for g, n in sold.items():
        if n < rl.get("min_sold", 3):
            continue
        per = data.stock.get(g, {})
        left = sum(per.values())
        if not left:
            gone.append((-n, title[g], n, arts[g]))
            continue
        days = left / (n / rl.get("days", 30))
        if days < rl.get("cover_days", 14):
            where = [f"{code[w]} {qty(q)}" for w, q in per.items() if w in code]
            other = sum(q for w, q in per.items() if w not in code)
            if other:
                where.append(T("low_other", q=qty(other)))
            low.append((days, -n, title[g], left, n, where, arts[g]))
    period = T("period", n=rl.get("days", 30))
    lines_low = [T("low_line", art=art(a), name=escape(short(t, 38)), left=qty(left),
                   where=f" ({', '.join(where)})" if where else "", n=qty(n), period=period)
                 for _, _, t, left, n, where, a in sorted(low)[:rl.get("limit", 8)]]
    lines_gone = [T("gone_line", art=art(a), name=escape(short(t, 38)), n=qty(n), period=period)
                  for _, t, n, a in sorted(gone)[:rl.get("gone_limit", 5)]]
    return lines_low, lines_gone


def daily_message(dcfg, data, snap_time, stale, extra_signals=()):
    day = data.day
    out = [T("title", date=f"{day:%d.%m.%Y}", wd=wd(day))]
    if snap_time:
        t = f"{snap_time:%H:%M}" + (f" {snap_time:%d.%m}" if snap_time.date() != day else "")
        out.append(T("asof", t=t) + (T("stale") if stale else ""))
    out.append("")

    today = data.today
    total, n = sales_of(today), checks_of(today)
    items = sum(l.qty for l in today if l.sign > 0)
    if n:
        out.append(T("total", sum=money(total), checks=checks_word(n), avg=money(total / n)))
        out.append(T("items", q=qty(items), per=f"{items / n:.1f}".replace(".", ",")))
        mt = margin_text(today)
        if mt:
            out.append(f"💹 {mt}" + (T("margin_est") if data.estimated else ""))
    else:
        out.append(T("none"))
    show = dict({"compare": True, "categories": True, "channels": True}, **dcfg.get("daily", {}))
    wa = day - dt.timedelta(days=7)
    wa_s = sales_of(data.between(wa, wa + dt.timedelta(days=1)))
    if wa_s and show["compare"]:
        out.append(T("week_ago", wd=wd(wa), date=f"{wa:%d.%m}", sum=money(wa_s), delta=delta(total, wa_s)))
    ly = day - dt.timedelta(days=364)
    ly_s = sales_of(data.last_year)
    if ly_s and show["compare"]:
        out.append(T("last_year", wd=wd(ly), date=f"{ly:%d.%m.%Y}", sum=money(ly_s), delta=delta(total, ly_s)))
    m0 = day.replace(day=1)
    mtd = sales_of(data.between(m0, day + dt.timedelta(days=1)))
    pm0 = (m0 - dt.timedelta(days=1)).replace(day=1)
    pm1 = pm0.replace(day=min(day.day, (m0 - dt.timedelta(days=1)).day)) + dt.timedelta(days=1)
    prev = sales_of(data.between(pm0, pm1))
    line = T("mtd", month=TX["months"][day.month - 1].capitalize(), d=day.day, sum=money(mtd))
    if prev:
        line += T("mtd_delta", delta=delta(mtd, prev))
    if show["compare"]:
        out.append(line)
    if day.weekday() == 0:
        we = data.between(day - dt.timedelta(days=2), day)
        if sales_of(we):
            out.append(T("weekend", sum=money(sales_of(we)), checks=checks_word(checks_of(we))))

    out += ["", T("h_points")]
    for p in dcfg["points"]:
        out += point_block(dcfg, data, p, total)

    sig = signals(dcfg, data, snap_time) + list(extra_signals)
    if sig:
        out += ["", T("h_signals")] + sig

    if show["categories"]:
        out += category_lines(today, total)

    channels = {}
    for l in today:
        if l.sign > 0:
            k = buyer_label(l.buyer, dcfg)
            channels[k] = channels.get(k, 0.0) + l.amount
    if len(channels) > 1 and show["channels"]:
        out += ["", T("h_channels"),
                " · ".join(f"{escape(k)} {money(v)}" for k, v in sorted(channels.items(), key=lambda x: -x[1])[:6])]

    top = top_lines(today)
    if top:
        out += ["", T("h_top")] + top

    low, gone = running_low(dcfg, data)
    if low:
        out += ["", T("h_low", n=dcfg["running_low"].get("cover_days", 14))] + low
    if gone:
        out += ["", T("h_gone")] + gone
    return "\n".join(out)


def by_category(lines):
    cats = {}
    for l in lines:
        if l.sign > 0:
            cats[l.category] = cats.get(l.category, 0.0) + l.amount
    return cats


def category_lines(lines, total, prev=None):
    cats = by_category(lines)
    before = by_category(prev) if prev is not None else {}
    if not cats:
        return []
    out = ["", T("h_cats")]
    cats = sorted(cats.items(), key=lambda x: -x[1])
    for name, v in cats[:7]:
        out.append(T("cat_line", name=escape(name), sum=money(v), p=f"{pct(v, total):.0f}")
                   + (f" · {delta(v, before[name])}" if before.get(name) else ""))
    rest = sum(v for _, v in cats[7:])
    if rest:
        out.append(T("cat_rest", sum=money(rest)))
    return out


# ---------- weekly message ----------

def weekly_message(dcfg, data):
    day = data.day
    mon = day - dt.timedelta(days=day.weekday())
    end = day + dt.timedelta(days=1)
    span = (end - mon).days
    week = data.between(mon, end)
    prev = data.between(mon - dt.timedelta(days=7), mon - dt.timedelta(days=7 - span))
    s, n = sales_of(week), checks_of(week)
    out = [T("w_title", d0=f"{mon:%d.%m}", d1=f"{day:%d.%m}"), ""]
    if not n:
        return "\n".join(out + [T("w_none")])
    line = T("total", sum=money(s), checks=checks_word(n), avg=money(s / n))
    if sales_of(prev):
        line += T("w_delta", delta=delta(s, sales_of(prev)), sum=money(sales_of(prev)))
    out.append(line)
    mt = margin_text(week)
    if mt:
        pm, prev_rev = margin_of(prev)
        out.append(f"💹 {mt}" + (T("w_margin_prev", p=f"{pct(pm, prev_rev):.0f}") if prev_rev else ""))

    out += ["", T("h_points")]
    for p in dcfg["points"]:
        ps, pp = sales_of(week, p["id"]), sales_of(prev, p["id"])
        code = escape(p["code"])
        if not ps:
            out.append(T("pt_none", code=code))
            continue
        pn = checks_of(week, p["id"])
        out.append(T("pt_head", code=code, sum=money(ps)) + f" · {pct(ps, s):.0f}%"
                   + T("pt_checks", checks=checks_word(pn), avg=money(ps / pn)) + (f" · {delta(ps, pp)}" if pp else ""))

    out += ["", T("h_days")]
    days = [(mon + dt.timedelta(days=i)) for i in range(span)]
    per_day = [(d, sales_of(data.between(d, d + dt.timedelta(days=1)))) for d in days]
    best = max(per_day, key=lambda x: x[1])
    for d, v in per_day:
        was = sales_of(data.between(d - dt.timedelta(days=7), d - dt.timedelta(days=6)))
        out.append(T("day_line", wd=wd(d), date=f"{d:%d.%m}", sum=money(v)) + (" 🏆" if d == best[0] and v else "")
                   + (T("day_prev", sum=money(was), delta=delta(v, was)) if was else ""))

    out += category_lines(week, s, prev)

    top = top_lines(week)
    if top:
        out += ["", T("h_top_week")] + top
    return "\n".join(out)


# ---------- pdf ----------

def fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in FONTS:
        if Path(reg).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("R", reg))
            pdfmetrics.registerFont(TTFont("B", bold))
            return "R", "B"
    raise RuntimeError("no suitable font found")


def point_pdf(path, dcfg, day, title, lines, pays, made_at):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    R, B = fonts()
    st = ParagraphStyle("n", fontName=R, fontSize=8, leading=10)
    stb = ParagraphStyle("b", parent=st, fontName=B)
    h1 = ParagraphStyle("h1", fontName=B, fontSize=14, leading=17)
    h2 = ParagraphStyle("h2", fontName=B, fontSize=11, leading=14, spaceBefore=8, spaceAfter=4)
    P = lambda t, s=st: Paragraph(escape(str(t)), s)
    widths = [8 * mm, 86 * mm, 27 * mm, 11 * mm, 20 * mm, 13 * mm, 20 * mm]
    grid = [("FONT", (0, 0), (-1, -1), R, 8), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8"))]
    form = lambda f: dcfg["pay_forms"].get(f, f)

    story = [P(T("pdf_title"), h1), P(f"{day:%d.%m.%Y}", st), P(title, stb),
             P(T("pdf_made", t=f"{made_at:%d.%m.%Y %H:%M:%S}"), st), Spacer(1, 4 * mm)]

    def section(kind, caption):
        items = [l for l in lines if l.sign == kind]
        if not items:
            return None
        story.append(P(caption, h2))
        data = [[P(h, stb) for h in TX["pdf_cols"]]]
        style = list(grid)
        checks = sorted({(l.at or dt.datetime.min, l.no, l.check) for l in items})
        for at, no, cid in checks:
            cl = [l for l in items if l.check == cid]
            first = cl[0]
            mark = "/".join(dcfg["pay_marks"].get(f, f) for f in sorted(pays.get(cid, {})) if pays[cid][f])
            vip = "VIP " if first.paycat == dcfg.get("vip_category") else ""
            head = (T("pdf_head", vip=vip, kind=T("pdf_check") if kind > 0 else T("pdf_return"), no=no,
                      t=f"{at:%d.%m.%Y %H:%M}") + (f"  ({mark})" if mark else "") + f"  {sync.norm(first.buyer)}")
            r = len(data)
            data.append([P(head, stb), "", "", "", "", "", ""])
            style += [("SPAN", (0, r), (-1, r)), ("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f4f4f4"))]
            for i, l in enumerate(cl, 1):
                d = pct(l.list_price - l.price, l.list_price)
                data.append([str(i), P(sync.norm(l.name)), l.barcode or "", qty(l.qty), money(l.list_price),
                             f"{d:.0f}" if d >= 0.5 else "", money(l.amount)])
            r = len(data)
            data.append(["", "", "", "", "", T("pdf_total"), money(sum(l.amount for l in cl))])
            style += [("FONT", (0, r), (-1, r), B, 8), ("SPAN", (0, r), (4, r))]
        r = len(data)
        total = sum(l.amount for l in items)
        data.append(["", T("pdf_section_total", caption=caption, n=len(checks), q=qty(sum(l.qty for l in items))),
                     "", "", "", "", money(total)])
        style += [("FONT", (0, r), (-1, r), B, 8), ("SPAN", (1, r), (5, r))]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle(style))
        story.append(t)
        forms = {}
        for cid in {l.check for l in items}:
            for f, v in pays.get(cid, {}).items():
                forms[f] = forms.get(f, 0.0) + v
        return total, forms

    sold = section(1, T("pdf_sales"))
    back = section(-1, T("pdf_returns"))
    if not sold and not back:
        story.append(P(T("pdf_empty"), st))

    story.append(P(T("pdf_summary"), h2))
    rows = []
    s_forms = sold[1] if sold else {}
    r_forms = back[1] if back else {}
    if sold:
        rows.append([T("pdf_sold"), money(sold[0])])
        for f, v in sorted(s_forms.items()):
            rows.append([T("pdf_paid", form=form(f)), money(v)])
        unpaid = sold[0] - sum(s_forms.values())
        if round(unpaid):
            rows.append([T("pdf_unpaid"), money(unpaid)])
        disc = sum(l.discount for l in lines if l.sign > 0)
        if round(disc):
            rows.append([T("pdf_disc"), money(disc)])
    if back:
        rows.append([T("pdf_returned"), money(back[0])])
        for f, v in sorted(r_forms.items()):
            rows.append([T("pdf_refund", form=form(f)), money(v)])
    for f in sorted(set(s_forms) | set(r_forms)):
        rows.append([T("pdf_cash", form=form(f)), money(s_forms.get(f, 0) - r_forms.get(f, 0))])
    rows.append([T("pdf_all"), money(sum(s_forms.values()) - sum(r_forms.values()))])
    t = Table(rows, colWidths=[120 * mm, 30 * mm])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), R, 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                           ("FONT", (0, -1), (-1, -1), B, 9), ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black)]))
    story.append(t)

    def footer(canvas, doc):
        canvas.setFont(R, 7)
        canvas.drawRightString(A4[0] - 12 * mm, 8 * mm, T("pdf_page", n=doc.page))

    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=10 * mm, rightMargin=10 * mm,
                      topMargin=10 * mm, bottomMargin=14 * mm).build(story, onFirstPage=footer, onLaterPages=footer)


# ---------- balance ----------

def balance_message(dcfg, now, log):
    """Account balance from a json api, whole units rounded down (never show more than there is)."""
    b = dcfg["balance"]
    token = (os.environ.get("BALANCE_TOKEN") or b.get("token") or "").strip()
    stamp = dict(date=f"{now:%d.%m.%Y}", t=f"{now.hour}:{now:%M}")
    try:
        r = requests.get(b["url"], headers={"Authorization": f"Bearer {token}"}, timeout=15)
        j = r.json()
        if j.get(b["ok_field"]) != b["ok_value"]:
            raise RuntimeError(str(j.get(b["error_field"])))
        v = j
        for k in b["path"]:
            v = v[k]
        return T("balance", sum=money(math.floor(float(v))), **stamp)
    except Exception as e:
        log.status(f"balance failed: {type(e).__name__}")
        log(str(e))
        return T("balance_na", **stamp)


# ---------- parcels waiting at the carrier's branch ----------

def order_meta(cfg):
    """Option lists of the order fields (status, manager, ...) from the orders api."""
    api = cfg["orders_api"]
    key = (os.environ.get("ORDERS_API_KEY") or api.get("key") or "").strip()
    for attempt in range(5):
        # the api limits the request rate, so a refusal is waited out
        time.sleep(api.get("pause", 1.5) * (attempt + 1) * 2)
        r = requests.get(api["url"].format(domain=api["domain"]), params={"page": 1, "limit": 1},
                         headers={api["key_header"]: key}, timeout=60)
        if r.status_code == 200 and "meta" in r.json():
            return r.json()["meta"]["fields"]
    return {}


def deliveries(o):
    v = o.get("ord_delivery_data") or []
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, list):
            out += [y for y in x if isinstance(y, dict)]
        elif isinstance(x, dict):
            out.append(x)
    return out


def track(dcfg, numbers):
    """Carrier tracking for many waybills -> {number: record}. Read only."""
    np_cfg = dcfg["np"]
    key = (os.environ.get("NP_KEY") or np_cfg.get("key") or "").strip()
    out = {}
    for i in range(0, len(numbers), 100):
        r = requests.post(np_cfg["url"], timeout=60, json={
            "apiKey": key, "modelName": "TrackingDocument", "calledMethod": "getStatusDocuments",
            "methodProperties": {"Documents": [{"DocumentNumber": n, "Phone": ""} for n in numbers[i:i + 100]]}})
        for x in r.json().get("data") or []:
            out[str(x.get("Number"))] = x
    return out


def branch_message(cfg, dcfg, now, log):
    """Parcels lying at the branch: short count for the first days, then order numbers by manager."""
    b = dcfg["branch"]
    orders, _ = sync.fetch_orders(cfg, [str(b["status"])])
    managers = {o["value"]: o["text"] for o in (order_meta(cfg).get("userId") or {}).get("options") or []}
    by_ttn = {}
    for o in orders:
        changed = o.get("updateAt")
        if changed and (now.replace(tzinfo=None) - dt.datetime.fromisoformat(changed)).days > b.get("stale_days", 30):
            continue  # long forgotten orders are reviewed separately
        for x in deliveries(o):
            if x.get("trackingNumber"):
                by_ttn[str(x["trackingNumber"])] = o
    info = track(dcfg, list(by_ttn))
    today = now.date()
    waiting = []
    for n, x in info.items():
        if str(x.get("StatusCode")) not in b["np_branch_codes"] or not x.get("ActualDeliveryDate"):
            continue
        days = (today - dt.datetime.fromisoformat(x["ActualDeliveryDate"]).date()).days
        cod = float(x.get("AfterpaymentOnGoodsCost") or 0)
        back = dt.date.fromisoformat(x["DateReturnCargo"]) if x.get("DateReturnCargo") else None
        o = by_ttn[n]
        name = managers.get(o.get("userId")) or T("no_manager")
        name = re.sub(r"\s*\(.*$", "", name)
        waiting.append((days, dcfg.get("manager_names", {}).get(name, name), o["id"], cod, back))

    out = [T("br_title", date=f"{today:%d.%m}", t=f"{now:%H:%M}"), ""]
    first = [w for w in waiting if 1 <= w[0] <= b["group_max"]]
    if first:
        out.append(T("br_group", d=b["group_max"], n=len(first), cod=sum(1 for w in first if w[3]),
                     paid=sum(1 for w in first if not w[3])))
    bounds = b["buckets"]
    shown = False
    for i, lo in enumerate(bounds):
        hi = bounds[i + 1] if i + 1 < len(bounds) else None
        group = [w for w in waiting if w[0] >= lo and (hi is None or w[0] < hi)]
        if not group:
            continue
        shown = True
        out += ["", T("br_bucket", label=TX["br_labels"][i], n=len(group))]
        per = {}
        for w in sorted(group, key=lambda w: (-w[0], w[2])):
            per.setdefault(w[1], []).append(w)
        for name, items in per.items():
            out.append(T("br_manager", name=escape(name)))
            for days, _, oid, cod, back in items:
                if back == today:
                    ret = T("br_ret_today")
                elif back == today + dt.timedelta(days=1):
                    ret = T("br_ret_tomorrow")
                else:
                    ret = T("br_ret", date=f"{back:%d.%m}") if back else ""
                out.append(T("br_line", id=oid, days=T("br_line_days", d=days) if hi is None else "",
                             pay=T("br_cod", sum=money(cod)) if cod else T("br_paid"), ret=ret).rstrip(" ·"))
    if not shown:
        out += ["", T("br_none")]
    log(f"Parcels at branch: {len(waiting)}")
    return "\n".join(out)


# ---------- telegram ----------

def tg(token, method, **kw):
    for attempt in range(5):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=120, **kw)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 429:
            time.sleep(int((j.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        if not j.get("ok"):
            raise RuntimeError(f"telegram {method}: {r.status_code} {j.get('description', '')}")
        return j["result"]
    raise RuntimeError(f"telegram {method}: rate limited")


def split_text(text, limit=4000):
    """Telegram takes up to 4096 chars per message: split on blank lines."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    return parts + [cur] if cur else parts


def send(token, chat, texts, files):
    for text in texts:
        for part in split_text(text):
            tg(token, "sendMessage", data={"chat_id": chat, "text": part, "parse_mode": "HTML",
                                           "disable_web_page_preview": True})
    if len(files) == 1:
        with open(files[0], "rb") as fh:
            tg(token, "sendDocument", data={"chat_id": chat}, files={"document": (files[0].name, fh)})
    elif files:
        handles = [open(f, "rb") for f in files]
        try:
            media = [{"type": "document", "media": f"attach://f{i}"} for i in range(len(files))]
            tg(token, "sendMediaGroup", data={"chat_id": chat, "media": json.dumps(media)},
               files={f"f{i}": (f.name, h) for i, (f, h) in enumerate(zip(files, handles))})
        finally:
            for h in handles:
                h.close()


def list_chats(token):
    """-> {chat id: (type, title, username)} from the updates the bot still holds (about a day)"""
    seen = {}
    for u in tg(token, "getUpdates", data={"allowed_updates": json.dumps(["message", "my_chat_member"])}):
        for k in ("message", "my_chat_member", "channel_post"):
            c = (u.get(k) or {}).get("chat")
            if c:
                seen[c["id"]] = (c.get("type"), c.get("title") or c.get("first_name") or "", c.get("username") or "")
    return seen


def resolve_chats(token, dcfg, keys):
    """Chat keys -> ids. A value may be an id, an @username of someone who wrote to the bot, or a list."""
    chats = dcfg["telegram"]["chats"]
    ids, known = [], None
    for key in keys.split(","):
        v = chats[key.strip()]
        for c in v if isinstance(v, list) else [v]:
            c = str(c).strip()
            if c.startswith("@"):
                if known is None:
                    known = {u.casefold(): i for i, (_, _, u) in list_chats(token).items() if u}
                if c[1:].casefold() not in known:
                    raise RuntimeError("recipient has not started the bot yet")
                c = str(known[c[1:].casefold()])
            ids.append(c)
    return list(dict.fromkeys(ids))


def send_all(token, ids, texts, files, log, exit_on_fail=True):
    failed = 0
    for chat in ids:
        try:
            send(token, chat, texts, files)
        except RuntimeError as e:
            # one recipient who blocked the bot must not stop the others
            failed += 1
            log(str(e))
    log.status(f"sent to {len(ids) - failed} of {len(ids)}")
    if failed == len(ids) and exit_on_fail:
        sys.exit(1)


# ---------- chat requests ----------

def poll_commands(cfg, log):
    """Requests to the bot in allowed chats ("@bot ... <keyword>") -> fresh report sent back there.

    Called every minute by the long sync job; returns the number of requests answered."""
    dcfg = load_digest_config()
    cm = dcfg.get("commands")
    if not cm:
        return 0
    TX.update(dcfg["texts"])
    token = (os.environ.get("TELEGRAM_TOKEN") or dcfg["telegram"].get("token") or "").strip()
    ups = tg(token, "getUpdates", data={"timeout": 0, "allowed_updates": json.dumps(["message"])})
    if not ups:
        return 0
    # confirm first, so a request that breaks the report is not repeated every minute
    tg(token, "getUpdates", data={"offset": ups[-1]["update_id"] + 1, "timeout": 0,
                                 "allowed_updates": json.dumps(["message"])})
    me = "@" + tg(token, "getMe")["username"].casefold()
    allowed = {}
    for k in cm["chats"]:
        v = dcfg["telegram"]["chats"][k]
        for c in v if isinstance(v, list) else [v]:
            allowed[str(c)] = k
    oldest = time.time() - cm.get("max_age_minutes", 15) * 60
    wanted = {}
    for u in ups:
        m = u.get("message") or {}
        text = (m.get("text") or "").casefold()
        chat = str((m.get("chat") or {}).get("id"))
        if chat not in allowed or me not in text or m.get("date", 0) < oldest:
            continue
        log(f"chat request: {text}")
        # a message may ask for both
        if any(w.casefold() in text for w in cm.get("daily", [])):
            wanted[(chat, "daily")] = True
        if any(w.casefold() in text for w in cm.get("weekly", [])):
            wanted[(chat, "weekly")] = True
    if not wanted:
        return 0
    tz = ZoneInfo(dcfg["tz"])
    snap_time = prepare_db(cfg, log, True)
    day = dt.datetime.now(tz).date()
    texts, files = build_report(cfg, dcfg, day, log, snap_time, HERE / "reports" / "digest", True)
    for chat, kind in wanted:
        if kind == "weekly":
            send_all(token, [chat], texts[1:], [], log, exit_on_fail=False)
        else:
            send_all(token, [chat], texts[:1], files, log, exit_on_fail=False)
        log.status(f"chat request answered: {kind}")
    return len(wanted)


# ---------- main ----------

def prepare_db(cfg, log, restore_db):
    """Newest snapshot restored into the working db -> snapshot time (naive local)."""
    have = sync.restored_time(cfg)
    if not restore_db:
        return have
    drive = build("drive", "v3", credentials=sync.credentials(cfg), cache_discovery=False)
    files = sync.list_snapshots(drive, cfg)
    if not files:
        return have
    latest = files[0]
    if have and abs((have - latest["taken"]).total_seconds()) < 60:
        log("Database already restored from the newest snapshot")
        return have
    bak = Path(cfg["sql"]["backup_dir"]) / "snap.bak"
    t0 = time.time()
    sync.download(drive, latest["id"], bak)
    sync.restore(cfg, cfg["sql"].get("server_backup_path") or str(bak))
    log(f"Snapshot {latest['taken']:%d.%m.%Y %H:%M} restored in {time.time() - t0:.0f}s")
    return sync.restored_time(cfg) or latest["taken"]


def build_report(cfg, dcfg, day, log, snap_time, out_dir, weekly):
    tz = ZoneInfo(dcfg["tz"])
    with sync.sql(cfg, cfg["sql"]["db"]) as cn:
        data = Data(cn, dcfg, day)
    now = dt.datetime.now(tz).replace(tzinfo=None)
    stale = bool(snap_time) and day == now.date() and \
        (now - snap_time).total_seconds() > dcfg.get("stale_minutes", 60) * 60
    texts = [daily_message(dcfg, data, snap_time, stale, order_signals(cfg, dcfg, day, log))]
    if weekly:
        texts.append(weekly_message(dcfg, data))

    files = []
    for p in dcfg["points"]:
        pl = [l for l in data.today if l.point == p["id"]]
        if not pl:
            continue
        f = out_dir / f"{day:%Y.%m.%d}_{p['code']}.pdf"
        point_pdf(f, dcfg, day, data.names.get(p["id"], p["code"]), pl, data.pays, snap_time or now)
        files.append(f)
    log(f"Lines today: {len(data.today)}, files: {len(files)}")
    return texts, files


def due_schedule(dcfg, now):
    """Name of the schedule whose window (send time minus/plus a margin) contains now, or None."""
    for name, s in dcfg["schedules"].items():
        if now.weekday() not in s["weekdays"] or (s.get("from") and now.date().isoformat() < s["from"]):
            continue
        at = dt.datetime.combine(now.date(), hhmm(s["at"]), tzinfo=now.tzinfo)
        if at - dt.timedelta(minutes=dcfg.get("window_before", 60)) <= now <= \
                at + dt.timedelta(minutes=s.get("window_after", dcfg.get("window_after", 180))):
            return name
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--chat", help="chat key(s) from the config, comma separated")
    ap.add_argument("--date", help="YYYY-MM-DD, default today")
    ap.add_argument("--weekly", action="store_true", help="add the weekly summary on any day")
    ap.add_argument("--balance", action="store_true", help="the balance note instead of the digest")
    ap.add_argument("--branch", action="store_true", help="the parcels-at-branch note instead of the digest")
    ap.add_argument("--schedule", help="run a schedule from the config (its kind, chat and time)")
    ap.add_argument("--no-restore", action="store_true")
    ap.add_argument("--wait", action="store_true", help="wait until the schedule's send time")
    ap.add_argument("--window", action="store_true", help="only tell which schedule is due now")
    ap.add_argument("--chats", action="store_true", help="list chats the bot can see")
    ap.add_argument("--commands", action="store_true", help="answer pending chat requests once")
    args = ap.parse_args()

    dcfg = load_digest_config()
    TX.update(dcfg["texts"])
    tz = ZoneInfo(dcfg["tz"])
    log = sync.Log(dcfg.get("quiet", False))
    token = (os.environ.get("TELEGRAM_TOKEN") or dcfg["telegram"].get("token") or "").strip()

    if args.chats:
        for cid, (kind, title, user) in list_chats(token).items():
            print(cid, kind, title, f"@{user}" if user else "")
        return

    if args.commands:
        cfg = sync.load_config()
        log.status(f"answered: {poll_commands(cfg, log)}")
        return

    now = dt.datetime.now(tz)
    if args.window:
        # with --schedule the named one is taken as it is (manual runs)
        name = args.schedule or due_schedule(dcfg, now)
        sync.set_output("day", now.date().isoformat())
        sync.set_output("schedule", name or "")
        sync.set_output("kind", dcfg["schedules"][name]["kind"] if name else "")
        log.status(f"due: {name}" if name else "nothing due")
        return

    sch = dcfg["schedules"][args.schedule] if args.schedule else {}
    kind = "balance" if args.balance else ("branch" if args.branch else sch.get("kind", "digest"))
    chat_keys = args.chat or sch.get("chat") or dcfg["telegram"]["chat"]

    def wait_until(target):
        left = (target - dt.datetime.now(tz)).total_seconds()
        if left > 0:
            log.status(f"waiting {int(left // 60)} min")
            time.sleep(left)

    try:
        send_at = dt.datetime.combine(now.date(), hhmm(sch["at"]), tzinfo=tz) if sch else now
        if kind == "branch":
            if args.wait:
                wait_until(send_at)
            texts, files = [branch_message(sync.load_config(), dcfg, dt.datetime.now(tz), log)], []
        elif kind == "balance":
            if args.wait:
                wait_until(send_at)
            texts, files = [balance_message(dcfg, dt.datetime.now(tz), log)], []
        else:
            if args.wait:
                # restore a few minutes early so the message goes out on time
                wait_until(send_at - dt.timedelta(minutes=dcfg.get("prepare_minutes", 6)))
            cfg = sync.load_config()
            if os.environ.get("BACKUP_DIR"):
                cfg["sql"]["backup_dir"] = os.environ["BACKUP_DIR"]
            day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz).date()
            snap_time = prepare_db(cfg, log, not args.no_restore)
            if args.wait:
                wait_until(send_at)
            out_dir = HERE / "reports" / "digest"
            weekly = args.weekly or day.weekday() == dcfg.get("weekly_on", 4)
            texts, files = build_report(cfg, dcfg, day, log, snap_time, out_dir, weekly)
            (out_dir / f"{day:%Y.%m.%d}_message.html").write_text("\n\n----\n\n".join(texts), encoding="utf-8")
        if args.send:
            # someone who has not started the bot yet may still do it: keep asking for a while
            give_up = time.time() + dcfg.get("resolve_wait_minutes", 30) * 60
            while True:
                try:
                    ids = resolve_chats(token, dcfg, chat_keys)
                    break
                except RuntimeError:
                    if time.time() > give_up:
                        raise
                    log.status("recipient not reachable yet, retrying")
                    time.sleep(120)
            send_all(token, ids, texts, files, log)
        else:
            if not log.quiet:
                print("\n\n----\n\n".join(texts))
            log.status("dry run")
    except Exception as e:
        if not log.quiet:
            raise
        log.status(f"failed: {type(e).__name__}")
        sys.exit(1)


if __name__ == "__main__":
    main()
