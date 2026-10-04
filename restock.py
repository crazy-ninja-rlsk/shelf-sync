"""Restock proposals written into the supplier tabs of an order spreadsheet.

Run by the long sync job after each stock update (python restock.py runs it once by hand, --dry writes nothing).
Everything specific — spreadsheet ids, column and status names, texts, queries, the price feed, the list of
replaced items — comes from RESTOCK_JSON (or restock.local.json).

Per tab: cell A1 = item code families handled there (blank = tab skipped), M1 = delivery days.
Heavy data is read once per run: the order spreadsheet (one request), its status lists (one request), the stock
sheet (one request), the price feed (once a day), sales (once a day, cached).
"""
import collections
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET

import requests

import sync

HERE = os.path.dirname(os.path.abspath(__file__))
FAST_DAYS, FAST_K, SLOW_K = 10, 2.0, 1.5   # delivery up to 10 days → shelf days ×2, longer → ×1.5
TREND_CAP, MIN_SOLD, DEFAULT_DAYS, GAP = 1.5, 3, 10, 5
W = 14   # columns A..N

R = {}          # restock config
TX = {}         # texts
_C = {}         # what one run reads once
TAB = None; PFXS = (); DAYS = DEFAULT_DAYS; COVER = 14


def load_config():
    raw = os.environ.get("RESTOCK_JSON") or open(os.path.join(HERE, "restock.local.json"), encoding="utf-8").read()
    R.clear(); R.update(json.loads(raw)); TX.clear(); TX.update(R["texts"])
    return R


def T(key, **kw):
    return TX[key].format(**kw) if kw else TX[key]


def st(key):
    return R["statuses"][key].casefold()


def prefixes_of(row1):
    return [x for x in re.split(r"[\s,;]+", str(row1[0] if row1 else "").strip().lower()) if re.fullmatch(r"[a-z]{1,6}", x)]


def days_of(row1):
    try:
        return int(float(str((list(row1) + [""] * 13)[12]).replace(",", ".")))
    except ValueError:
        return DEFAULT_DAYS


def owns(a, pfxs):
    return bool(pfxs) and bool(re.match(r"^(" + "|".join(map(re.escape, pfxs)) + r")-?\d", str(a).strip().lower()))


def date_pattern(key):
    """Regex for a dated text template like "proposed {d}" → captures day and month."""
    return re.compile(re.escape(TX[key]).replace(re.escape("{d}"), r"(\d{2})\.(\d{2})"))


# ---------- reading ----------

def load(cfg, refresh=False):
    if _C and not refresh:
        return _C
    _C.clear()
    creds = sync.credentials(cfg); gc = sync.gspread.authorize(creds)
    from googleapiclient.discovery import build as _build
    svc = _build("sheets", "v4", credentials=creds, cache_discovery=False)
    book = gc.open_by_key(R["file"])
    sheets = {w.title: w for w in book.worksheets()}
    titles = [t for t in sheets if t not in set(R.get("skip_tabs", []))]
    got = book.values_batch_get([f"'{t}'" for t in titles])["valueRanges"]
    tabs = {t: [list(r) for r in g.get("values", [])] for t, g in zip(titles, got)}

    def last_status(rows):
        return max([i for i, r in enumerate(rows, 1) if i > 2 and len(r) > 9 and r[9].strip()] + [3])

    def last_filled(rows):
        return max([i for i, r in enumerate(rows, 1) if any(str(x).strip() for x in r)] + [2])

    rngs = [f"'{t}'!J{last_status(tabs[t])}:J{max(last_status(tabs[t]), last_filled(tabs[t]) + 1)}" for t in titles]
    meta = svc.spreadsheets().get(spreadsheetId=R["file"], ranges=rngs, includeGridData=True,
                                  fields="sheets(properties(sheetId,title),data(rowData(values(dataValidation(condition(values))))))").execute()
    lists, cells = {}, {}
    bot_value = R["statuses"]["bot_value"]
    for sh, rng in zip(meta["sheets"], rngs):
        t = sh["properties"]["title"]; row0 = int(re.search(r"J(\d+):", rng).group(1))
        for k, row in enumerate((sh.get("data") or [{}])[0].get("rowData") or []):
            v = (row.get("values") or [{}])[0]
            opts = [x.get("userEnteredValue", "") for x in (v.get("dataValidation") or {}).get("condition", {}).get("values", [])]
            if k == 0: lists[t] = opts
            if bot_value in opts: cells[t] = (sh["properties"]["sheetId"], row0 + k)   # a cell whose list knows the bot statuses
    ws = gc.open_by_key(cfg["sheet"]["id"]).get_worksheet_by_id(cfg["sheet"]["gid"])
    stock_rows = ws.get(f"A6:P{ws.row_count}", value_render_option="UNFORMATTED_VALUE")
    _C.update(cfg=cfg, book=book, sheets=sheets, tabs=tabs, lists=lists, cells=cells,
              template=next(iter(cells.values()), None), stock_rows=stock_rows, prices=feed_prices(), sales=None)
    return _C


def cache_dir():
    d = os.environ.get("RESTOCK_CACHE") or os.path.join(HERE, "restock_cache")
    os.makedirs(d, exist_ok=True)
    return d


def feed_prices():
    """Prices by item code from the shop feed, downloaded at most once a day."""
    f = os.path.join(cache_dir(), f"feed_{dt.date.today():%Y-%m-%d}.xml")
    if not os.path.exists(f):
        r = requests.get(R["feed"], timeout=180); r.raise_for_status()
        open(f, "wb").write(r.content)
    out = {}
    for o in ET.parse(f).getroot().iter("offer"):
        m = (o.findtext("model") or "").strip()
        if m: out[m.lower()] = (o.findtext("price") or "").strip()
    return out


def sales_cache(cfg, pfxs):
    """Daily sales of the handled families for 400 days: from the database once a day (the morning run)."""
    f = os.path.join(cache_dir(), f"sales_{dt.date.today():%Y-%m-%d}_{'_'.join(sorted(pfxs))}.json")
    if os.path.exists(f):
        return json.load(open(f, encoding="utf-8"))
    like = " OR ".join([R["queries"]["item_like"]] * len(pfxs))
    with sync.sql(cfg, cfg["sql"]["db"]) as cn:
        rows = cn.execute(R["queries"]["sales"].format(like=like), *[p + "%" for p in pfxs],
                          dt.date.today() - dt.timedelta(days=400)).fetchall()
    data = [[a, d.isoformat(), float(q), nm] for a, d, q, nm in rows]
    json.dump(data, open(f, "w", encoding="utf-8"), ensure_ascii=False)
    return data


def db_stock(cfg, pfxs):
    """Current stock in the database (fallback for items missing in the stock sheet), live."""
    like = " OR ".join([R["queries"]["item_like"]] * len(pfxs))
    with sync.sql(cfg, cfg["sql"]["db"]) as cn:
        return {a.lower(): float(q) for a, q in cn.execute(R["queries"]["stock"].format(like=like), *[p + "%" for p in pfxs]).fetchall()
                if owns(a, pfxs)}


def handled_tabs():
    return [t for t, rows in _C["tabs"].items() if rows and prefixes_of(rows[0])]


def setup(tab):
    """Article families from A1 (blank = skipped, returns None), delivery days from M1, shelf days from both."""
    global TAB, PFXS, DAYS, COVER
    rows = _C["tabs"].get(tab) or [[]]
    pfxs = prefixes_of(rows[0])
    if not pfxs:
        return None
    TAB, PFXS, DAYS = tab, tuple(pfxs), days_of(rows[0])
    COVER = max(1, round(DAYS * (FAST_K if DAYS <= FAST_DAYS else SLOW_K)))
    return PFXS, DAYS


# ---------- the proposal ----------

def build():
    c = _C; cfg = c["cfg"]; today = dt.date.today()
    newer = dict(R["editions"]["pairs"]); gone = {a: True for a in R["editions"]["gone"]}
    ART = re.compile(r"([A-Za-z]{2,4}-?\d{3,5}(?:[A-Za-z]\d)?(?:[-_/]\d{2})?)")
    new_words = re.compile(R["edition_words"]["newer"]); oop = R["edition_words"]["out_of_print"]
    plan, sheetname = {}, {}
    for r in c["stock_rows"]:
        r = list(r) + [""] * 16; a = sync.norm(r[0]); note = " ".join(str(r[15] or "").split())
        if a and note:
            # a note naming another item = a newer edition; the out-of-print words alone = never proposed
            other = [x for x in ART.findall(note) if x.lower() != a.lower()]
            if other and new_words.search(note.lower()): newer[a.lower()] = other[0].lower()
            elif oop in note.lower(): gone.setdefault(a.lower(), True)
        if owns(a, PFXS):
            plan[a.lower()] = float(r[12]) if str(r[12]).strip() != "" else 0.0
            sheetname[a.lower()] = sync.norm(r[1])
    older = collections.defaultdict(list)
    for o, n in newer.items(): older[n].append(o)

    def lineage(a, seen=None):
        seen = seen or set(); out = []
        for o in older.get(a.lower(), []):
            if o not in seen: seen.add(o); out += [o] + lineage(o, seen)
        return out

    v = c["tabs"][TAB]; hd = R["headers"]
    h = v[1]; ia, ib, ie, i_f, ist = [h.index(hd[k]) for k in ("item", "name", "shelf", "clients", "status")]
    pend = collections.defaultdict(lambda: [0.0, 0.0, []]); names = {}; problem = {}; bot = collections.defaultdict(list)
    rejected = {}; mgr = collections.defaultdict(list); first = {}; case = {}
    active = {x.casefold() for x in R["statuses"]["active"]}
    p_prop, p_upd = date_pattern("proposed"), date_pattern("updated")
    num = lambda x: float(str(x).replace(",", ".") or 0) if str(x).strip() else 0.0
    for n, r in enumerate(v[2:], start=3):
        r = list(r) + [""] * 14
        a0 = sync.norm(r[ia]); a = a0.lower(); s = sync.norm(r[ist]).casefold()
        if not a: continue
        case.setdefault(a, a0)
        if sync.norm(r[ib]): names[a] = sync.norm(r[ib])
        if s == st("bot"):
            mm = p_prop.search(r[13]) or p_upd.search(r[13])
            if mm and a not in first: first[a] = f"{mm.group(1)}.{mm.group(2)}"
            bot[a].append(n)   # own earlier proposals are recalculated and rewritten, not counted as ordered
        elif s == st("rejected"):
            # the manager said no: no shelf proposal for it on that day, again from the next day
            mm = p_upd.search(r[13])
            if mm and (int(mm.group(2)), int(mm.group(1))) == (today.month, today.day): rejected[a] = n
        elif s == st("to_order"):
            p = pend[a]; p[0] += num(r[ie]); p[1] += num(r[i_f]); p[2].append(n)
        if s in active:
            mgr[a].append((n, num(r[ie]) + num(r[i_f])))   # a manager row below a pending proposal = taken over
        if s == st("problem"): problem[a] = n

    if c["sales"] is None:
        c["sales"] = sales_cache(cfg, sorted({p for t in handled_tabs() for p in prefixes_of(c["tabs"][t][0])}))
    daily = collections.defaultdict(dict); dbname = {}
    for a, d, q, nm in c["sales"]:
        if owns(a, PFXS) or a.lower() in newer:   # own families, plus older editions whose history continues here
            daily[a.lower()][dt.date.fromisoformat(d)] = q; dbname[a.lower()] = nm; case.setdefault(a.lower(), a)
    own = {a: dict(x) for a, x in daily.items()}
    for a in list(own):
        for o in lineage(a):
            for d, q in own.get(o, {}).items(): daily[a][d] = daily[a].get(d, 0.0) + q
    dbstock = db_stock(cfg, PFXS)
    price = c["prices"]
    tot = lambda a, d0, d1: sum(q for d, q in daily[a].items() if d0 <= d < d1)
    dd = lambda x: f"{x:%d.%m}"
    out = []
    for a in sorted({x for x in set(plan) | set(daily) if owns(x, PFXS)}):
        if a in newer or a in gone:
            continue
        weeks = [tot(a, today - dt.timedelta(days=7 * (k + 1)), today - dt.timedelta(days=7 * k)) for k in range(4)]
        last4 = sum(weeks); s90 = tot(a, today - dt.timedelta(days=90), today + dt.timedelta(days=1))
        L = DAYS; ly0 = today - dt.timedelta(days=365)
        ly_prev = tot(a, ly0 - dt.timedelta(days=28), ly0)
        ly_lead = tot(a, ly0, ly0 + dt.timedelta(days=L))
        ly_after = tot(a, ly0 + dt.timedelta(days=L), ly0 + dt.timedelta(days=L + COVER))
        # typical week: the median ignores a one-week spike, the last two weeks catch a fading season
        weekly = min(statistics.median(weeks), (weeks[0] + weeks[1]) / 2)
        spans = [(today - dt.timedelta(days=7 * (k + 1)), today - dt.timedelta(days=7 * k + 1)) for k in range(4)]
        basis = T("sales", weeks=", ".join(T("week", a=dd(x0), b=dd(x1), n=f"{w:g}") for (x0, x1), w in zip(spans, weeks)))
        basis += T("rate", w=f"{weekly:g}".replace(".", ","))
        # an order placed today only helps after it arrives: sales until then come from the stock we have, the order
        # covers the shelf days after arrival; last year's shape is applied to both windows apart
        t_lead = t_after = 1.0; season_only = False
        if ly_prev >= MIN_SOLD:
            t_lead = min(TREND_CAP, (ly_lead / max(L, 1) * 28) / ly_prev)
            t_after = min(TREND_CAP, (ly_after / COVER * 28) / ly_prev)
            basis += T("last_year", d=dd(today), prev=f"{ly_prev:g}", L=L, lead=f"{ly_lead:g}", C=COVER, after=f"{ly_after:g}", t=f"{t_after:.1f}")
        elif last4 < MIN_SOLD and ly_after >= MIN_SOLD and s90 > 0:
            season_only = True
        sell_lead = weekly / 7 * L * t_lead
        need_after = weekly / 7 * COVER * t_after
        insheet = a in plan
        m = plan[a] if insheet else dbstock.get(a, 0.0)
        pe, pf, prow = pend.get(a, [0, 0, []])
        F = max(0, math.ceil(-m - pf))
        at_arrival = max(0.0, max(m, 0) + pe - sell_lead)
        E = 0
        if last4 >= MIN_SOLD and a not in rejected:
            E = max(0, math.ceil(need_after - at_arrival))
        # demand that only started this week looks like a spike; with nothing in stock it becomes a hint
        fresh = (not E and weeks[0] >= MIN_SOLD and sum(weeks[1:]) <= 1 and max(m, 0) + pe <= 0 and a not in rejected)
        season_only = season_only or fresh
        below = [x for x in mgr.get(a, []) if a in bot and x[0] > min(bot[a])]
        took = T("took", rows=", ".join(T("took_row", n=n, q=f"{q:g}") for n, q in below)) if below else ""
        if not (E or F or season_only) and below:
            out.append(dict(a=case.get(a, a), name="", E=0, F=0, D=0, G="", why=took, rows=bot[a], delete=True))
            continue
        head = [T("proposed", d=first.get(a, dd(today))), T("updated", d=dd(today))]
        if not (E or F or season_only):
            if a in bot:
                out.append(dict(a=case.get(a, a), name="", E=0, F=0, D=0, G=price.get(a, ""), rows=bot[a],
                                why="; ".join(head + [basis, T("plan", m=f"{m:g}"), T("not_needed")])))
            continue
        if a in problem: continue
        why = [basis]
        if lineage(a): why.insert(0, T("lineage", items=", ".join(lineage(a))))
        why.append(T("plan", m=f"{m:g}") if insheet else T("plan_db", m=f"{m:g}"))
        if last4 >= MIN_SOLD:
            why.append(T("flow", L=L, sell=f"{sell_lead:.0f}", arr=f"{at_arrival:.0f}", C=COVER, need=f"{need_after:.0f}"))
        if fresh: why.append(T("fresh", w0=f"{weeks[0]:g}", rest=f"{sum(weeks[1:]):g}"))
        elif season_only: why.append(T("season", C=COVER, ly=f"{ly_after:g}"))
        if F: why.append(T("clients", n=f"{-m:g}"))
        if a in rejected: why.append(T("rejected", row=rejected[a]))
        if prow: why.append(T("pending", rows=", ".join(map(str, prow))))
        if took: why.append(T("took_short", took=took, n=E + F))
        why.append(T("order", n=E + F) if E + F else T("review"))
        nm = names.get(a) or sync.norm(sync.norm(dbname.get(a, "") or sheetname.get(a, "")).replace(case.get(a, a), "").replace(TAB, ""))
        for p in R.get("name_strip", []):
            nm = re.sub(r"^(" + re.escape(p) + r"\s+)+", "", nm)
        out.append(dict(a=case.get(a, a), name=nm, E=E, F=F, D=E + F, G=price.get(a, ""), why="; ".join(head + why),
                        rows=bot.get(a, []), hint=season_only))
    out.sort(key=lambda r: (r.get("hint", False), -r["F"], -r["D"]))
    return out


# ---------- writing ----------

def layout(vals):
    """(last filled row, last row with a status other than the bot's, pending bot rows below it)"""
    filled = lambda r: r and ((r[:1] and str(r[0]).strip()) or (len(r) > 9 and str(r[9]).strip()))
    last_any = max([i for i, r in enumerate(vals, 1) if i > 2 and filled(r)] + [2])
    last_mgr = max([i for i, r in enumerate(vals, 1) if i > 2 and len(r) > 9 and str(r[9]).strip() and str(r[9]).strip().casefold() != st("bot")] + [2])
    below = [i for i, r in enumerate(vals, 1) if i > last_mgr and len(r) > 9 and str(r[9]).strip().casefold() == st("bot")]
    return last_any, last_mgr, below


def write(out, dry=False):
    """Own rows rewritten in place (duplicates zeroed, taken-over ones deleted), new ones appended below everything,
    at least GAP empty rows under the manager's last row, with the look and status list of the row above."""
    c = _C; ws = c["sheets"][TAB]; vals = c["tabs"][TAB]
    last_any, last_mgr, _ = layout(vals)
    nxt = max(last_any + 1, last_mgr + GAP + 1)
    data = []; log = []; gone_rows = []
    today = dt.date.today()
    stamp = float(f"{today.day}.{today.month:02d}")   # dates in column K are typed as numbers like 28.09
    for r in out:
        rows = list(r.get("rows") or [])
        price = int(float(r["G"])) if r["G"] else ""
        if r.get("delete"):
            gone_rows += rows; log += [(n, "deleted") for n in rows]
            continue
        if rows:
            n = rows[0]
            data.append({"range": f"D{n}:G{n}", "values": [[f"=F{n}+E{n}", r["E"] or "", r["F"] or "", price]]})
            data.append({"range": f"K{n}:N{n}", "values": [[stamp, "", "", r["why"]]]})
            log.append((n, "updated"))
            for extra in rows[1:]:
                data.append({"range": f"E{extra}:F{extra}", "values": [["", ""]]})
                data.append({"range": f"N{extra}", "values": [[T("merged", n=n)]]})
                log.append((extra, "zeroed"))
        elif r["D"] or r.get("hint"):
            n = nxt; nxt += 1
            data.append({"range": f"A{n}:N{n}", "values": [[r["a"], r["name"], "", f"=F{n}+E{n}", r["E"] or "", r["F"] or "", price,
                                                            "", "", R["statuses"]["bot_value"], stamp, "", "", r["why"]]]})
            log.append((n, "new"))
    if dry:
        return log
    new_rows = [n for n, kind in log if kind == "new"]
    if new_rows:
        bots = [i for i, r in enumerate(vals, 1) if i > 2 and len(r) > 9 and str(r[9]).strip().casefold() == st("bot")]
        src = max(bots) if bots else last_mgr
        box = lambda sid, r0, r1, c0=0, c1=W: {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}
        lo, hi = min(new_rows) - 1, max(new_rows)
        reqs = [{"copyPaste": {"source": box(ws.id, src - 1, src), "destination": box(ws.id, lo, hi), "pasteType": "PASTE_FORMAT"}},
                {"copyPaste": {"source": box(ws.id, src - 1, src), "destination": box(ws.id, lo, hi), "pasteType": "PASTE_DATA_VALIDATION"}}]
        tpl = c["cells"].get(TAB) or c["template"]
        if R["statuses"]["bot_value"] not in c["lists"].get(TAB, []) and tpl:
            reqs.append({"copyPaste": {"source": box(tpl[0], tpl[1] - 1, tpl[1], 9, 10), "destination": box(ws.id, lo, hi, 9, 10), "pasteType": "PASTE_DATA_VALIDATION"}})
        c["book"].batch_update({"requests": reqs})
    if data:
        ws.batch_update(data, value_input_option="USER_ENTERED")
    if gone_rows:
        c["book"].batch_update({"requests": [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
                                             for n in sorted(set(gone_rows), reverse=True)]})
    log += move_stale(ws, vals, changed=bool(new_rows or gone_rows))
    if new_rows or gone_rows or any(k == "moved" for _, k in log) or gap_short(vals):
        log += keep_gap(ws)
    return log


def stale_rows(vals):
    """Pending proposals from earlier days with manager rows below them."""
    last = max([i for i, r in enumerate(vals, 1) if r and ((r[:1] and str(r[0]).strip()) or (len(r) > 9 and str(r[9]).strip()))] + [0])
    today = dt.date.today(); out = []; p = date_pattern("proposed")
    for i, r in enumerate(vals, 1):
        r = list(r) + [""] * 14
        if i < 3 or sync.norm(r[9]).casefold() != st("bot"): continue
        mm = p.search(str(r[13]))
        when = dt.date(today.year, int(mm.group(2)), int(mm.group(1))) if mm else today
        later = [j for j in range(i + 1, last + 1) if sync.norm((list(vals[j - 1]) + [""] * 10)[9]).casefold() not in ("", st("bot"))]
        if when < today and later: out.append(i)
    return out, last


def move_stale(ws, vals, changed):
    """Yesterday's pending proposals with manager rows below them go to the bot block at the bottom."""
    stale, last = stale_rows(vals)
    if not stale:
        return []
    if changed:
        vals = ws.get(f"A1:N{ws.row_count}")
        stale, last = stale_rows(vals)
        if not stale:
            return []
    last = max(last, max([i for i, r in enumerate(vals, 1) if r and any(str(x).strip() for x in r)] + [last]))
    ws.spreadsheet.batch_update({"requests": [{"moveDimension": {"source": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": n - 1 - k, "endIndex": n - k},
                                                                 "destinationIndex": last}} for k, n in enumerate(stale)]})
    return [(n, "moved") for n in stale]


def empty(vals, i):
    return not any(str(x).strip() for x in (vals[i - 1] if i - 1 < len(vals) else []))


def gap_short(vals):
    _, last_mgr, below = layout(vals)
    if not below:
        return False
    first = min(below)
    return all(empty(vals, i) for i in range(last_mgr + 1, first)) and first - last_mgr - 1 < GAP


def keep_gap(ws):
    """Manager rows came closer than GAP empty rows to the bot block: empty rows are inserted above it, dressed like
    the manager's last row (format, status list, formula of column O)."""
    vals = ws.get(f"A1:N{ws.row_count}")
    _, last_mgr, below = layout(vals)
    if not below:
        return []
    first = min(below)
    gap = list(range(last_mgr + 1, first))
    if not all(empty(vals, i) for i in gap) or len(gap) >= GAP:
        return []
    add = GAP - len(gap)
    box = lambda r0, r1, c0, c1: {"sheetId": ws.id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}
    s, d0, d1 = (last_mgr - 1, last_mgr), first - 1, first - 1 + add
    ws.spreadsheet.batch_update({"requests": [
        {"insertDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": d0, "endIndex": d1}, "inheritFromBefore": False}},
        {"copyPaste": {"source": box(*s, 0, 26), "destination": box(d0, d1, 0, 26), "pasteType": "PASTE_FORMAT"}},
        {"copyPaste": {"source": box(*s, 0, 26), "destination": box(d0, d1, 0, 26), "pasteType": "PASTE_DATA_VALIDATION"}},
        {"copyPaste": {"source": box(*s, 14, 15), "destination": box(d0, d1, 14, 15), "pasteType": "PASTE_FORMULA"}}]})
    return [(first, "gap")]


# ---------- run ----------

def run_all(cfg, log=print, dry=False, only=()):
    """One pass over every tab with families in A1. -> number of rows touched"""
    load_config(); load(cfg, refresh=True)
    touched = 0; tabs = 0
    for t in list(_C["tabs"]):
        if only and t not in only:
            continue
        if not setup(t):
            continue
        tabs += 1
        res = write(build(), dry=dry)
        touched += len(res)
    log(f"restock: {tabs} tabs, {touched} rows")
    return touched


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    run_all(sync.load_config(), dry=dry, only=[a for a in sys.argv[1:] if a != "--dry"])
