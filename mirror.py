"""Mirror of item terms, stock and options into the shop database.

Run by the long sync job after each stock update; python mirror.py runs it once by hand (--write applies,
otherwise only the report is written). Everything specific - database access, spreadsheet ids, tab names,
item families, queries, patterns and every text - comes from MIRROR_JSON (or mirror.local.json).

One run: read the restored snapshot (one query), the shop catalogue (short columns + options of the item
family that has them), the settings spreadsheet; plan; apply grouped guarded UPDATEs; write the report and
the change log to the spreadsheet.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import pymysql

import sync

HERE = os.path.dirname(os.path.abspath(__file__))
M = {}   # mirror config
T = {}   # texts


def load_config():
    raw = os.environ.get("MIRROR_JSON") or open(os.path.join(HERE, "mirror.local.json"), encoding="utf-8").read()
    M.clear(); M.update(json.loads(raw)); T.clear(); T.update(M["texts"])
    return M


def key(s):
    return " ".join(str(s or "").split()).casefold()


def stem(s):
    return re.sub(M["stem"], "", key(s))


def code(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v or "").strip()


# ---------- shop database ----------

def db_connect():
    d = M["db"]
    cn = pymysql.connect(host=d["host"], port=int(d.get("port") or 3306), user=d["user"], password=d["pass"],
                         database=d["name"], charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                         autocommit=False, connect_timeout=30)
    return cn, d.get("prefix", "")


_CAT = {}   # catalogue read once per snapshot and shared with the restock module


def catalogue(cfg):
    """All shop items (short columns + name + supplier code), read at most once per restored snapshot."""
    if not M:
        load_config()
    stamp = sync.restored_time(cfg)
    if _CAT.get("stamp") != stamp or "rows" not in _CAT:
        days, lang = M["days_field"], int(M["lang"])
        cn, p = db_connect()
        try:
            with cn.cursor() as cur:
                cur.execute(f"SELECT i.product_id id, i.model, i.status, i.quantity, i.price, i.{days} days, "
                            f"i.source_sku sku, d.name FROM {p}product i LEFT JOIN {p}product_description d "
                            f"ON d.product_id = i.product_id AND d.language_id = {lang}")
                _CAT.update(stamp=stamp, rows=cur.fetchall())
        finally:
            cn.close()
    return [dict(r) for r in _CAT["rows"]]


def prices(cfg):
    """{code: (price, name, supplier code)} in the shape the restock module used to take from the price feed."""
    out = {}
    for r in catalogue(cfg):
        m = str(r["model"] or "").strip()
        if m and m.lower() not in out:
            pr = float(r["price"] or 0)
            out[m.lower()] = (f"{pr:g}" if pr else "0", " ".join(str(r["name"] or "").split()),
                              " ".join(str(r["sku"] or "").split()))
    return out


def load_shop(cn, p, goods, links, cfg):
    lang = int(M["lang"])
    prods = catalogue(cfg)
    shop = defaultdict(list)  # code -> items (a code may be there twice)
    for r in sorted(prods, key=lambda r: (-int(r["status"]), r["id"])):
        k = key(r["model"])
        if k:
            r["model"] = " ".join(str(r["model"]).split())
            r["name"] = " ".join(str(r["name"] or "").split())
            r["values"], r["option_ids"] = {}, set()
            shop[k].append(r)
    mark = M["mark"].casefold()
    want = {shop_code(g["code"], shop) for g in goods if g["family"]} | {a for a, _v in links.values()} \
        | {a for a in shop if mark in a}
    ids = [r["id"] for a in want for r in shop.get(a, [])]
    opts, dictionary = [], defaultdict(set)
    if ids:
        with cn.cursor() as cur:
            cur.execute(f"SELECT v.product_option_value_id id, v.product_id, v.option_id, v.quantity q, v.price, "
                        f"od.name oname, vd.name vname FROM {p}product_option_value v "
                        f"JOIN {p}option_description od ON od.option_id = v.option_id AND od.language_id = {lang} "
                        f"JOIN {p}option_value_description vd ON vd.option_value_id = v.option_value_id "
                        f"AND vd.language_id = {lang} WHERE v.product_id IN ({','.join(map(str, ids))})")
            opts = cur.fetchall()
            oids = sorted({o["option_id"] for o in opts})
            if oids:
                cur.execute(f"SELECT option_id, name FROM {p}option_value_description "
                            f"WHERE language_id = {lang} AND option_id IN ({','.join(map(str, oids))})")
                for r in cur.fetchall():
                    dictionary[r["option_id"]].add(key(r["name"]))
    by_item = defaultdict(dict)
    for o in opts:
        by_item[o["product_id"]].setdefault(key(o["vname"]), []).append(o)
    for items in shop.values():
        for r in items:
            r["values"] = by_item.get(r["id"], {})
            r["option_ids"] = {o["option_id"] for v in r["values"].values() for o in v}
            r["has_price"] = float(r["price"]) > 0 or any(float(o["price"]) > 0 for v in r["values"].values() for o in v)
    return shop, dictionary


def fill_names(cn, p, problems, shop):
    ids = {r["id"]: r for e in problems if e[1] and not e[2] for r in shop.get(key(e[1]), [])[:1]}
    if not ids:
        return problems
    with cn.cursor() as cur:
        cur.execute(f"SELECT product_id, name FROM {p}product_description "
                    f"WHERE language_id = {int(M['lang'])} AND product_id IN ({','.join(map(str, ids))})")
        names = {r["product_id"]: " ".join(str(r["name"] or "").split()) for r in cur.fetchall()}
    by_code = {key(r["model"]): names.get(i, "") for i, r in ids.items()}
    return [e if e[2] or not e[1] else (e[0], e[1], by_code.get(key(e[1]), ""), *e[3:]) for e in problems]


# ---------- snapshot ----------

def places(cfg, gc):
    """The same locations the stock sheet uses."""
    s = cfg["sheet"]
    ws = gc.open_by_key(s["id"]).get_worksheet_by_id(s["gid"])
    first = sync.gspread.utils.a1_to_rowcol(s["loc_first_col"] + "1")[1]
    return {sync.norm(h) for h in ws.row_values(s["header_row"])[first - 1:] if sync.norm(h)} \
        - {sync.norm(x) for x in cfg.get("ignore_locations", [])}


def read_goods(cfg, locs):
    """goods_query -> (code, name, barcode, size, colour, group, location, qty) per item and location."""
    groups = tuple(M["groups"])
    mark = M["mark"].casefold()
    size_in_name = re.compile(M["size_in_name"])
    goods, per_loc = {}, defaultdict(lambda: defaultdict(float))
    with sync.sql(cfg, cfg["sql"]["db"]) as cn:
        for c, name, bc, size, colour, grp, loc, qty in cn.execute(M["goods_query"]).fetchall():
            name, bc, c = sync.norm(name), code(bc), (c or "").strip()
            gid = bc or "name:" + name
            if gid not in goods:
                m = size_in_name.search(name)
                goods[gid] = dict(code=c, name=name, barcode=bc, size=(size or "").strip(), colour=(colour or "").strip(),
                                  name_size=m.group(1) if m else "",
                                  family=(grp or "")[:len(groups[0])] in groups or mark in c.casefold())
            if loc and qty and sync.norm(loc) in locs:
                per_loc[gid][sync.norm(loc)] += float(qty)
    for gid, g in goods.items():
        g["stock"] = sum(max(q, 0.0) for q in per_loc[gid].values())
        g["negative"] = {l: q for l, q in per_loc[gid].items() if q < 0}
    return list(goods.values())


# ---------- settings spreadsheet ----------

def read_settings(sh):
    tabs = M["tabs"]
    rows = sh.worksheet(tabs["terms"]).get_all_values()[1:]
    terms, skip = [], set()
    for r in rows:
        r = r + [""] * 7
        if key(r[0]) and str(r[1]).strip() and sync.num(r[1]) is not None:
            d = sync.num(r[1])
            terms.append((key(r[0]), int(d) if float(d).is_integer() else d))
        if key(r[6]):
            skip.add(key(r[6]))
    terms.sort(key=lambda t: -len(t[0]))
    links = {}
    for r in sh.worksheet(tabs["links"]).get_all_values()[1:]:
        r = r + [""] * 3
        if code(r[0]) and key(r[1]):
            links[code(r[0])] = (key(r[1]), key(r[2]))
    return terms, skip, links


def skipped(c, skip):
    return any(c == x or (c.startswith(x) and not c[len(x)].isalnum()) for x in skip)


# ---------- plan ----------

def match_value(g, values):
    size = g["size"] or g["name_size"]
    for cand in (size, g["colour"]):
        if cand and key(cand) in values:
            return key(cand)
    if g["colour"]:
        hits = [v for v in values if stem(v) == stem(g["colour"])]
        if len(hits) == 1:
            return hits[0]
    ws = {key(w) for w in re.split(r"[\s()]+", g["name"]) if w}
    hits = [v for v in values if v in ws] or [v for v in values if stem(v) in {stem(w) for w in ws}]
    return hits[0] if len(hits) == 1 else None


def shop_code(c, shop):
    k = key(c)
    if k not in shop and "_" in k and k.split("_")[0] in shop:
        return k.split("_")[0]
    return k


def plan(goods, shop, dictionary, terms, links, skip):
    changes = []   # (table, id, field, old, new, code, what)
    problems = []  # reason, code, name, barcode, qty, details

    stock = defaultdict(float)
    for g in goods:
        if g["code"]:
            stock[key(g["code"])] += g["stock"]

    qty = defaultdict(lambda: defaultdict(float))
    total_by_code = defaultdict(float)
    for g in (g for g in goods if g["family"]):
        if not re.search(r"[^\W\d_]", g["code"]):
            problems.append((T["no_letters"], g["code"], g["name"], g["barcode"], g["stock"], ""))
            continue
        target = None
        if g["barcode"] in links:
            a, v = links[g["barcode"]]
            if a in shop:
                target = (a, v if v in shop[a][0]["values"] else "")
        a = shop_code(g["code"], shop)
        items = shop.get(a)
        if target is None and items:
            vals = items[0]["values"]
            v = match_value(g, vals) if vals else ""
            if v is not None:
                target = (a, v)
            elif g["stock"] > 0:
                size = g["size"] or g["name_size"]
                if size and any(key(size) in dictionary[o] for o in items[0]["option_ids"]):
                    problems.append((T["size_missing"], items[0]["model"], g["name"], g["barcode"], g["stock"],
                                     T["size_missing_detail"].format(size)))
                else:
                    have = ", ".join(sorted({o[0]["vname"] for o in vals.values()}))
                    problems.append((T["option_missing"], items[0]["model"], g["name"], g["barcode"], g["stock"],
                                     T["option_missing_detail"].format(have)))
        if target is None and not items and g["stock"] > 0:
            problems.append((T["not_in_shop"], g["code"], g["name"], g["barcode"], g["stock"], ""))
        if items or target:
            total_by_code[(target or (a,))[0]] += g["stock"]
        if target:
            qty[target[0]][target[1]] += g["stock"]

    scope = set(qty) | set(total_by_code) | {a for a in shop if M["mark"].casefold() in a}
    for a in sorted(scope):
        q = qty.get(a, {})
        for r in shop[a]:
            if r["values"]:
                total = 0
                for v, rows in r["values"].items():
                    n = int(q.get(v, 0))
                    total += n
                    for o in rows:
                        if n != o["q"]:
                            changes.append(("option", o["id"], "quantity", o["q"], n, r["model"],
                                            f"{o['oname'].rstrip(':')}: {o['vname']}"))
            else:
                total = int(q.get("", 0))
            if total != r["quantity"]:
                changes.append(("product", r["id"], "quantity", r["quantity"], total, r["model"], "quantity"))
            want = 1 if max(total, total_by_code.get(a, 0)) > 0 else 0
            if want == 1 and r["status"] == 0 and not r["has_price"]:
                problems.append((T["zero_price"], r["model"], r["name"], "", total, ""))
            elif want != r["status"]:
                changes.append(("product", r["id"], "status", r["status"], want, r["model"], "status"))

    for a, items in shop.items():
        for r in items:
            if r["status"] != 1:
                continue
            if stock.get(a, 0) > 0 or total_by_code.get(a, 0) > 0:
                d = 0
            else:
                d = next((n for pre, n in terms if a.startswith(pre)), None)
            if d is not None and d != r["days"]:
                changes.append(("product", r["id"], "days", r["days"], d, r["model"], "days"))

    active = {a for a, items in shop.items() if any(r["status"] == 1 for r in items)}
    word = M["family_word"]
    for g in goods:
        k = key(g["code"])
        if g["stock"] > 0 and g["name"].startswith(word):
            if not k:
                problems.append((T["word_no_code"], "", g["name"], g["barcode"], g["stock"], ""))
            elif k not in active:
                problems.append((T["word_not_in_shop"] + (T["off_suffix"] if k in shop else ""),
                                 g["code"], g["name"], g["barcode"], g["stock"], ""))
        if g["negative"]:
            problems.append((T["negative"], g["code"], g["name"], g["barcode"], sum(g["negative"].values()),
                             "; ".join(f"{l}: {q:g}" for l, q in sorted(g["negative"].items()))))
    for a, items in shop.items():
        if sum(1 for r in items if r["status"] == 1) > 1:
            problems.append((T["duplicate"], items[0]["model"], items[0]["name"], "", "", ""))
    problems = [e for e in problems if not (e[1] and skipped(key(e[1]), skip))]
    return changes, problems


# ---------- apply / report ----------

def apply(cn, p, changes, chunk=500):
    """Guarded UPDATEs grouped by (field, old -> new); one read-back tells what really changed."""
    table = {"product": (f"{p}product", "product_id"), "option": (f"{p}product_option_value", "product_option_value_id")}
    field = {"days": M["days_field"], "quantity": "quantity", "status": "status"}
    groups = defaultdict(list)
    for c in changes:
        groups[(c[0], c[2], c[3], c[4])].append(c)
    statements, now = 0, {}
    with cn.cursor() as cur:
        for (tbl, f, old, new), cs in groups.items():
            t, idcol = table[tbl]
            for i in range(0, len(cs), chunk):
                ids = ",".join(str(c[1]) for c in cs[i:i + chunk])
                cur.execute(f"UPDATE {t} SET {field[f]} = %s WHERE {field[f]} = %s AND {idcol} IN ({ids})", (new, old))
                statements += 1
        cn.commit()
        for tbl in {c[0] for c in changes}:
            t, idcol = table[tbl]
            cs = [c for c in changes if c[0] == tbl]
            fs = sorted({c[2] for c in cs})
            cols = ", ".join(f"{field[f]} AS {f}" for f in fs)
            for i in range(0, len(cs), chunk * 4):
                ids = ",".join(str(c[1]) for c in cs[i:i + chunk * 4])
                cur.execute(f"SELECT {idcol} AS id, {cols} FROM {t} WHERE {idcol} IN ({ids})")
                for r in cur.fetchall():
                    for f in fs:
                        now[(tbl, r["id"], f)] = r[f]
    return [c for c in changes if now.get((c[0], c[1], c[2])) == c[4]], statements


def report(sh, changes, problems, mode, taken, now, state):
    tabs = M["tabs"]
    stamp = now.strftime("%d.%m.%Y %H:%M")
    snap = taken.strftime("%d.%m.%Y %H:%M") if taken else "-"
    ws = sh.worksheet(tabs["errors"])
    problems = sorted(problems, key=lambda e: (e[0], key(e[1])))
    body = [[T["errors_title"].format(stamp=stamp, snap=snap, mode=mode), "", "", "", "", "", "",
             json.dumps(state, ensure_ascii=False)], T["errors_head"]]
    body += [[e[0], e[1], e[2], e[3], (int(e[4]) if isinstance(e[4], float) and e[4].is_integer() else e[4]), e[5]]
             for e in problems]
    ws.clear()
    ws.update(values=body, range_name="A1", value_input_option="RAW")

    ws = sh.worksheet(tabs["journal"])
    if ws.row_values(1) != T["journal_head"]:
        ws.update(values=[T["journal_head"]], range_name="A1")
    label = {"days": M["days_field"]}
    rows = [[stamp, mode, c[5], label.get(c[6], c[6]), c[3], c[4]] for c in changes]
    if rows:
        ws.insert_rows(rows, row=2, value_input_option="RAW")
    dates = ws.col_values(1)[1:]
    limit = now - dt.timedelta(days=M.get("journal_days", 30))
    for i, v in enumerate(dates, start=2):
        try:
            if dt.datetime.strptime(v, "%d.%m.%Y %H:%M").replace(tzinfo=now.tzinfo) < limit:
                ws.delete_rows(i, len(dates) + 1)
                break
        except ValueError:
            continue


def run(cfg, log=print, write=None, force=False):
    """One pass on the snapshot already restored by the sync job."""
    load_config()
    write = M.get("write", False) if write is None else write
    tz = ZoneInfo(cfg["schedule"]["tz"])
    now = dt.datetime.now(tz)
    mode = T["mode_write"] if write else T["mode_dry"]
    t0 = time.time()

    gc = sync.gspread.authorize(sync.credentials(cfg))
    sh = gc.open_by_key(M["sheet_id"])
    taken = sync.restored_time(cfg)
    taken = taken.replace(tzinfo=tz) if taken else None
    terms, skip, links = read_settings(sh)
    inputs = hashlib.sha1(json.dumps([terms, sorted(skip), sorted(links.items())], ensure_ascii=False).encode()).hexdigest()[:16]
    state = {"snapshot": taken.isoformat() if taken else "", "inputs": inputs, "mode": "w" if write else "d"}
    if write and not force:
        try:
            last = json.loads(sh.worksheet(M["tabs"]["errors"]).acell("H1").value or "{}")
        except (ValueError, TypeError):
            last = {}
        if last == state:
            log(f"{now:%H:%M} mirror: nothing new")
            return

    goods = read_goods(cfg, places(cfg, gc))
    cn, p = db_connect()
    try:
        shop, dictionary = load_shop(cn, p, goods, links, cfg)
        g = M["guards"]
        stop = []
        if not taken or (now - taken).total_seconds() > g["max_snapshot_age_hours"] * 3600:
            stop.append(T["guard_old"])
        if sum(1 for x in goods if x["stock"] > 0) < g["min_items_with_stock"]:
            stop.append(T["guard_few"])
        fam = [r for a, items in shop.items() if M["mark"].casefold() in a for r in items if r["status"] == 1]
        if fam and sum(1 for r in fam if not r["has_price"]) / len(fam) > g["max_zero_price_share"]:
            stop.append(T["guard_prices"])

        changes, problems = plan(goods, shop, dictionary, terms, links, skip)
        problems = fill_names(cn, p, problems, shop)
        statements = 0
        if stop:
            problems.insert(0, (T["stopped"] + "; ".join(stop), "", "", "", "", ""))
            changes, mode, state["mode"] = [], T["mode_stopped"], "s"
        elif write:
            changes, statements = apply(cn, p, changes)
    finally:
        cn.close()
    report(sh, changes, problems, mode, taken, now, state)
    kinds = defaultdict(int)
    for c in changes:
        kinds[c[6] if c[0] == "product" else "option"] += 1
    log(f"{now:%H:%M} mirror: {'stopped' if stop else ('written' if write else 'dry')} {len(changes)} "
        f"({', '.join(f'{k} {n}' for k, n in sorted(kinds.items())) or '-'}), updates {statements}, "
        f"report {len(problems)}, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    run(sync.load_config(), write=a.write or None, force=a.force)
