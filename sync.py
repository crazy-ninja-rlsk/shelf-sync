"""Shelf sync: newest snapshot from a Drive folder -> stock per location -> spreadsheet.

    python sync.py                 # dry run: report of what would change, sheet untouched
    python sync.py --write         # apply changes to the sheet
    python sync.py --any-time      # ignore the working-hours window
    python sync.py --reprocess     # process even if the sheet already has this snapshot

All names, ids, queries and paths live in config.local.json (or the CONFIG_JSON env var).
With "quiet": true nothing about the data is printed (for public CI logs).
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import pyodbc
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

HERE = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
STAMP_RE = re.compile(r"(\d{2})[-.](\d{2})[-.](\d{4})[_ ](\d{2})-?(\d{2})-?(\d{2})")


def load_config():
    raw = os.environ.get("CONFIG_JSON") or (HERE / "config.local.json").read_text(encoding="utf-8")
    return json.loads(raw)


class Log:
    def __init__(self, quiet):
        self.quiet = quiet
        self.lines = []

    def __call__(self, *a):
        s = " ".join(str(x) for x in a)
        self.lines.append(s)
        if not self.quiet:
            print(s, flush=True)

    def status(self, s):
        # the only thing that goes to the console in quiet mode
        print(s, flush=True)


def norm(s):
    return " ".join(str(s or "").split())


def num(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        return None


def cell(q):
    """0 -> empty cell, whole numbers as int."""
    if not q:
        return ""
    return int(q) if float(q).is_integer() else round(q, 3)


def col_letter(i):
    return gspread.utils.rowcol_to_a1(1, i).rstrip("1")


# ---------- source ----------

def credentials(cfg):
    key = os.environ.get("GOOGLE_KEY_JSON")
    if key:
        return service_account.Credentials.from_service_account_info(json.loads(key), scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(cfg["google_key_file"], scopes=SCOPES)


def snapshot_time(f):
    m = STAMP_RE.search(f["name"])
    if m:
        d, mo, y, h, mi, s = map(int, m.groups())
        return dt.datetime(y, mo, d, h, mi, s)
    return None


def list_snapshots(drive, cfg):
    r = drive.files().list(
        q=f"'{cfg['folder_id']}' in parents and trashed = false",
        orderBy="createdTime desc", pageSize=200,
        fields="files(id,name,size,createdTime)",
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    ext = cfg.get("file_ext", "").lower()
    files = [f for f in r.get("files", []) if f["name"].lower().endswith(ext)]
    for f in files:
        f["taken"] = snapshot_time(f)
    files = [f for f in files if f["taken"]]
    files.sort(key=lambda f: f["taken"], reverse=True)
    return files


def download(drive, file_id, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


# ---------- database ----------

def sql(cfg, db="master"):
    conn = cfg["sql"]["conn"].replace("{pwd}", os.environ.get("SQL_PASSWORD", "")) + f";DATABASE={db}"
    for attempt in range(40):  # a fresh server container needs a while to accept logins
        try:
            cn = pyodbc.connect(conn, autocommit=True, timeout=30)
            break
        except pyodbc.Error:
            if attempt == 39:
                raise
            time.sleep(3)
    cn.timeout = 0
    return cn


def run(cur, q):
    cur.execute(q)
    while cur.nextset():
        pass


def restored_time(cfg):
    """Start time of the backup the working db was restored from (None if not restored)."""
    with sql(cfg) as cn:
        row = cn.execute(
            "SELECT TOP 1 b.backup_start_date FROM msdb.dbo.restorehistory r "
            "JOIN msdb.dbo.backupset b ON b.backup_set_id = r.backup_set_id "
            "WHERE r.destination_database_name = ? ORDER BY r.restore_date DESC", cfg["sql"]["db"]).fetchone()
    return row[0] if row else None


def restore(cfg, bak):
    db = cfg["sql"]["db"]
    with sql(cfg) as cn:
        cur = cn.cursor()
        data_dir = cur.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(400))").fetchone()[0]
        sep = "\\" if "\\" in data_dir else "/"
        if not data_dir.endswith(sep):
            data_dir += sep
        files = cur.execute(f"RESTORE FILELISTONLY FROM DISK = N'{bak}'").fetchall()
        moves = []
        for f in files:
            ext = ".ldf" if f.Type == "L" else (".mdf" if f.FileId == 1 else f"_{f.FileId}.ndf")
            moves.append(f"MOVE N'{f.LogicalName}' TO N'{data_dir}{db}{ext}'")
        run(cur, f"IF DB_ID('{db}') IS NOT NULL ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        run(cur, f"RESTORE DATABASE [{db}] FROM DISK = N'{bak}' WITH {', '.join(moves)}, REPLACE, RECOVERY")
        run(cur, f"ALTER DATABASE [{db}] SET MULTI_USER")


def read_stock(cfg):
    """-> ({article: {location: qty}}, {article: title} for every article known to the db)"""
    with sql(cfg, cfg["sql"]["db"]) as cn:
        known = {}
        for r in cn.execute(cfg["sql"]["articles_query"]).fetchall():
            a = norm(r[0]).casefold()
            if a and a not in known:
                known[a] = norm(r[1]) if len(r) > 1 else ""
        stock = {}
        for a, loc, q in cn.execute(cfg["sql"]["stock_query"]).fetchall():
            a, loc = norm(a).casefold(), norm(loc)
            if a and loc and q:
                stock.setdefault(a, {})
                stock[a][loc] = stock[a].get(loc, 0.0) + float(q)
    return stock, known


# ---------- orders api ----------

class RateLimited(Exception):
    pass


def fetch_orders(cfg, status_ids):
    """All orders in the given statuses -> (orders, number of requests)."""
    api = cfg["orders_api"]
    key = (os.environ.get("ORDERS_API_KEY") or api.get("key") or "").strip()
    url = api["url"].format(domain=api["domain"])
    orders, page, calls = [], 1, 0
    while True:
        params = [("page", page), ("limit", api.get("page_size", 100))]
        params += [(f"filter[statusId][{i}]", s) for i, s in enumerate(status_ids)]
        r = requests.get(url, params=params, headers={api["key_header"]: key}, timeout=60)
        calls += 1
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        j = r.json()
        data = j.get("data") or []
        orders += data
        pages = (j.get("pagination") or {}).get("pageCount") or 0
        if not data or page >= pages:
            return orders, calls
        page += 1
        time.sleep(api.get("pause", 1.5))


def order_lines(orders):
    """-> [(status id, article key, amount)]"""
    out = []
    for o in orders:
        for p in o.get("products") or []:
            a = norm(p.get("parameter")).casefold()
            if a:
                out.append((str(o.get("statusId")), a, float(p.get("amount") or 0), norm(p.get("parameter"))))
    return out


# ---------- settings tab ----------

def read_settings(sh, cfg):
    st = cfg["settings"]
    ranges = list(st["api_columns"].values()) + [st["new_articles_statuses"]]
    ranges += [x["statuses"] for x in st["file_columns"].values()]
    got = sh.worksheet(st["tab"]).batch_get(ranges)
    flat = {rng: [norm(v) for row in vals for v in row if norm(v)] for rng, vals in zip(ranges, got)}
    ids = lambda rng: [v for v in flat[rng] if v.isdigit()]
    return dict(
        api_columns={col: ids(rng) for col, rng in st["api_columns"].items()},
        new_articles=ids(st["new_articles_statuses"]),
        file_columns={col: flat[x["statuses"]] for col, x in st["file_columns"].items()},
    )


# ---------- order file ----------

def read_order_file(gc, cfg, settings):
    """-> {target column: {article key: qty}}, number of tabs used"""
    f = cfg["order_file"]
    hdr = f["header_row"]
    everything = norm(cfg["settings"].get("all_statuses_word", ""))
    rules = []
    for col, x in cfg["settings"]["file_columns"].items():
        wanted = {s.casefold() for s in settings["file_columns"][col]}
        take_all = x.get("all_if_empty") and (not wanted or everything.casefold() in wanted)
        rules.append((col, x["sources"], wanted, take_all))
    totals = {col: {} for col, *_ in rules}
    used = 0
    book = gc.open_by_key(f["id"])
    tabs = book.worksheets()
    got = book.values_batch_get([f"'{t.title}'" for t in tabs], params={"valueRenderOption": "UNFORMATTED_VALUE"})
    for vr in got.get("valueRanges", []):
        data = vr.get("values", [])
        if len(data) < hdr:
            continue
        h = [norm(x) for x in data[hdr - 1]]
        need = [f["article"], f["status"]] + [s for _, src, _, _ in rules for s in src]
        if any(n not in h for n in need):
            continue
        used += 1
        ai, si = h.index(f["article"]), h.index(f["status"])
        for row in data[hdr:]:
            row = list(row) + [""] * (len(h) - len(row))
            a, status = norm(row[ai]).casefold(), norm(row[si]).casefold()
            if not a or not status:
                continue
            for col, src, wanted, take_all in rules:
                if take_all or status in wanted:
                    q = sum((num(row[h.index(s)]) or 0) for s in src)
                    if q:
                        totals[col][a] = totals[col].get(a, 0.0) + q
    return totals, used


# ---------- sheet ----------

def plan_sheet(ws, cfg, stock, known):
    s = cfg["sheet"]
    hdr_row, first_row = s["header_row"], s["first_row"]
    sum_col = gspread.utils.a1_to_rowcol(s["sum_col"] + "1")[1]
    loc_first = gspread.utils.a1_to_rowcol(s["loc_first_col"] + "1")[1]
    ignore = {norm(x) for x in cfg.get("ignore_locations", [])}
    skip_articles = {norm(x).casefold() for x in cfg.get("ignore_articles", [])}

    grid = ws.get(f"A1:{col_letter(ws.col_count)}{ws.row_count}", value_render_option="UNFORMATTED_VALUE")
    grid = [list(r) for r in grid]
    width = max(len(r) for r in grid)
    for r in grid:
        r.extend([""] * (width - len(r)))

    header = grid[hdr_row - 1]
    loc_last = loc_first - 1
    for c in range(loc_first, width + 1):
        if norm(header[c - 1]):
            loc_last = c
    old_cols = [(c, norm(header[c - 1])) for c in range(loc_first, loc_last + 1)]

    seen, keep, drop = set(), [], []
    for c, name in old_cols:
        if name and name in seen:
            drop.append(c)
        else:
            seen.add(name)
            keep.append((c, name))

    # locations without a column in the sheet are skipped (columns are added by hand)
    add = []
    missing = sorted({loc for per in stock.values() for loc, q in per.items() if q and loc not in ignore and loc not in seen})
    new_cols = [name for _, name in keep]

    rows = []  # (row, article, old C, new C, [(name, old, new)])
    for r in range(first_row, len(grid) + 1):
        a = norm(grid[r - 1][0]).casefold()
        if not a or a not in known or a in skip_articles:
            continue
        per = stock.get(a, {})
        # a negative balance counts as nothing on that location
        vals = {name: max(per.get(name, 0.0), 0.0) for name in new_cols if name and name not in ignore}
        total = sum(vals.values())
        old_by_name = {name: grid[r - 1][c - 1] for c, name in keep}
        # ignored locations that still have a column keep whatever is there
        rows.append((r, grid[r - 1][0], grid[r - 1][sum_col - 1], cell(total),
                     [(name, old_by_name.get(name, ""),
                       old_by_name.get(name, "") if not name or name in ignore else cell(vals.get(name, 0.0)))
                      for name in new_cols]))

    return dict(grid=grid, sum_col=sum_col, loc_first=loc_first, loc_last=loc_last,
                keep=keep, drop=drop, add=add, missing=missing, new_cols=new_cols, rows=rows,
                dropped=[(c, norm(header[c - 1])) for c in drop])


def same(a, b):
    na, nb = num(a), num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return norm(a) == norm(b)


def report(p, log):
    changed_cells, changed_rows, samples = 0, 0, []
    for r, art, old_c, new_c, locs in p["rows"]:
        diffs = []
        if not same(old_c, new_c):
            diffs.append(("SUM", old_c, new_c))
        diffs += [d for d in locs if not same(d[1], d[2])]
        if diffs:
            changed_rows += 1
            changed_cells += len(diffs)
            if len(samples) < 40:
                samples.append((r, art, diffs))
    log(f"Articles matched in sheet: {len(p['rows'])}")
    log(f"Rows to change: {changed_rows}, cells to change: {changed_cells}")
    for c, name in p["dropped"]:
        log(f"Remove duplicate column {col_letter(c)}: {name}")
    for name in p["add"]:
        log(f"Add column: {name}")
    for name in p["missing"]:
        log(f"No column, skipped: {name}")
    for r, art, diffs in samples:
        log(f"  row {r} {art}: " + "; ".join(f"{n}: {o!r} -> {v!r}" for n, o, v in diffs))
    return changed_cells


def apply(sh, ws, p, cfg, stamp, extra=()):
    """stamp=None: the stock part is skipped, only `extra` is written."""
    if stamp is None:
        if extra:
            ws.batch_update(list(extra), value_input_option="RAW")
        return
    s = cfg["sheet"]
    reqs = []
    for c in sorted(p["drop"], reverse=True):
        reqs.append({"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                   "startIndex": c - 1, "endIndex": c}}})
    last_after_drop = p["loc_first"] + len(p["keep"]) - 1
    if p["add"]:
        reqs.append({"insertDimension": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                                   "startIndex": last_after_drop, "endIndex": last_after_drop + len(p["add"])},
                                         "inheritFromBefore": True}})
    if reqs:
        sh.batch_update({"requests": reqs})

    loc_last = p["loc_first"] + len(p["new_cols"]) - 1
    data = []
    if p["add"]:
        a = col_letter(last_after_drop + 1)
        data.append({"range": f"{a}{s['header_row']}", "values": [p["add"]]})
    for r, _, _, new_c, locs in p["rows"]:
        data.append({"range": f"{s['sum_col']}{r}", "values": [[new_c]]})
        data.append({"range": f"{col_letter(p['loc_first'])}{r}:{col_letter(loc_last)}{r}",
                     "values": [[v for _, _, v in locs]]})

    # only the date and time inside each stamp text are replaced, the text itself stays
    for stamp_cell in [s["stamp_cell"]] + s.get("extra_stamp_cells", []):
        data.append({"range": stamp_cell, "values": [[restamp(ws.acell(stamp_cell).value, stamp)]]})
    data += list(extra)
    ws.batch_update(data, value_input_option="RAW")


def apply_row(ws, p, cfg, row):
    """Test write of a single row: C and the existing location columns only, no column changes, no stamp."""
    pos = {name: c for c, name in p["keep"]}
    for r, art, _, new_c, locs in p["rows"]:
        if r != row:
            continue
        data = [{"range": f"{cfg['sheet']['sum_col']}{r}", "values": [[new_c]]}]
        data += [{"range": f"{col_letter(pos[name])}{r}", "values": [[v]]} for name, _, v in locs if name in pos]
        ws.batch_update(data, value_input_option="RAW")
        return art
    return None


def restamp(text, when):
    """Replace only the date and the time inside a stamp text."""
    text = re.sub(r"\d{2}\.\d{2}\.\d{4}", when.strftime("%d.%m.%Y"), text or "", count=1)
    return re.sub(r"\d{1,2}:\d{2}", when.strftime("%H:%M"), text, count=1)


def last_article_row(values, first_row):
    last = first_row - 1
    for r in range(first_row, len(values) + 1):
        if values[r - 1] and norm(values[r - 1][0]):
            last = r
    return last


def add_new_articles(sh, ws, cfg, lines, settings, write):
    """Articles from active orders that the sheet does not list yet go to the end of the list."""
    s = cfg["sheet"]
    col_a = ws.get(f"A1:A{ws.row_count}")
    have = {norm(r[0]).casefold() for r in col_a if r}
    active = set(settings["new_articles"])
    new = {}
    for status, key, _, raw in lines:
        if status in active and key not in have:
            new.setdefault(key, raw)
    new = sorted(new.values(), key=str.casefold)
    if write and new:
        last = last_article_row(col_a, s["first_row"])
        sh.batch_update({"requests": [{"insertDimension": {
            "range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": last, "endIndex": last + len(new)},
            "inheritFromBefore": True}}]})
        mark = s["new_article_mark_col"]
        ws.batch_update([
            {"range": f"A{last + 1}:A{last + len(new)}", "values": [[a] for a in new]},
            {"range": f"{mark}{last + 1}:{mark}{last + len(new)}", "values": [[s["new_article_mark"]]] * len(new)},
        ], value_input_option="RAW")
    return new


def fill_formulas(sh, ws, cfg, header, last):
    """Copy the formula columns from their first row down to the last article."""
    s = cfg["sheet"]
    src = s["formula_first_row"]
    reqs = []
    for name in s["formula_headers"]:
        c = header.get(norm(name))
        if c and last > src:
            box = lambda r0, r1: {"sheetId": ws.id, "startRowIndex": r0, "endRowIndex": r1,
                                  "startColumnIndex": c - 1, "endColumnIndex": c}
            reqs.append({"copyPaste": {"source": box(src - 1, src), "destination": box(src, last),
                                       "pasteType": "PASTE_FORMULA"}})
    if reqs:
        sh.batch_update({"requests": reqs})


def column_update(grid, col, first, last, new):
    """One range for a whole column; rows not in `new` keep their value. -> (range, changed cells)"""
    vals, changed = [], 0
    for r in range(first, last + 1):
        row = grid[r - 1] if r - 1 < len(grid) else []
        old = row[col - 1] if col - 1 < len(row) else ""
        v = new.get(r, old)
        if r in new and not same(old, v):
            changed += 1
        vals.append([v])
    return {"range": f"{col_letter(col)}{first}:{col_letter(col)}{last}", "values": vals}, changed


def sheet_stamp(ws, cfg):
    v = ws.acell(cfg["sheet"]["stamp_cell"]).value or ""
    d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", v)
    t = re.search(r"(\d{1,2}):(\d{2})", v)
    if not d:
        return None
    h, m = (int(t.group(1)), int(t.group(2))) if t else (0, 0)
    return dt.datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)), h, m)


# ---------- main ----------

def in_window(now, sch):
    hm = now.strftime("%H:%M")
    start, end = sch["from"], sch["to"]
    # an end earlier than the start means the window runs past midnight
    inside = start <= hm <= end if start <= end else (hm >= start or hm <= end)
    return now.weekday() in sch["weekdays"] and inside


def minutes_to_start(now, sch):
    h, m = map(int, sch["from"].split(":"))
    return (h * 60 + m) - (now.hour * 60 + now.minute)


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


class Session:
    def __init__(self, cfg):
        self.creds = credentials(cfg)
        self.drive = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        self.gc = gspread.authorize(self.creds)
        self.sh = self.gc.open_by_key(cfg["sheet"]["id"])
        self.ws = self.sh.get_worksheet_by_id(cfg["sheet"]["gid"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true", help="only tell whether a new snapshot is waiting")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--reprocess", action="store_true", help="update even if the sheet already has this snapshot")
    ap.add_argument("--mode", choices=["all", "stock", "orders"], default="all")
    ap.add_argument("--loop", action="store_true", help="keep polling once a minute until the window closes")
    ap.add_argument("--minutes", type=int, help="loop: stop after this many minutes")
    ap.add_argument("--window", action="store_true", help="only tell whether a loop should start now")
    ap.add_argument("--row", type=int, help="test: write only this sheet row")
    ap.add_argument("--sheet", help="test: use another spreadsheet id")
    ap.add_argument("--fresh", action="store_true", help="download and restore even if the db already has this snapshot")
    args = ap.parse_args()
    if args.row:
        args.any_time = args.reprocess = True

    cfg = load_config()
    if args.sheet:
        cfg["sheet"]["id"] = args.sheet
    if os.environ.get("BACKUP_DIR"):
        cfg["sql"]["backup_dir"] = os.environ["BACKUP_DIR"]
    log = Log(cfg.get("quiet", False))
    if args.window:
        now = dt.datetime.now(ZoneInfo(cfg["schedule"]["tz"]))
        sch = cfg["schedule"]
        soon = now.weekday() in sch["weekdays"] and 0 <= minutes_to_start(now, sch) <= sch.get("early_minutes", 45)
        go = args.any_time or in_window(now, sch) or soon
        set_output("run", "1" if go else "0")
        log.status("in window" if go else "outside window")
        return
    try:
        if args.loop:
            loop(cfg, args, log)
        else:
            run_once(cfg, args, log)
    except Exception as e:
        if not log.quiet:
            raise
        log.status(f"failed: {type(e).__name__}")
        sys.exit(1)


def latest_snapshot(ses, cfg, log):
    files = list_snapshots(ses.drive, cfg)
    if not files:
        return None
    latest = files[0]
    log(f"Latest snapshot: {latest['taken']:%d.%m.%Y %H:%M:%S}, {int(latest['size']) / 2**20:.0f} MB")
    return latest


def run_once(cfg, args, log):
    set_output("need", "0")
    tz = ZoneInfo(cfg["schedule"]["tz"])
    if not args.any_time and not in_window(dt.datetime.now(tz), cfg["schedule"]):
        log.status("outside window")
        return
    ses = Session(cfg)
    latest = latest_snapshot(ses, cfg, log)
    if not latest and args.mode != "orders":
        log.status("no snapshots")
        return
    if args.mode != "orders" and not args.reprocess:
        in_sheet = sheet_stamp(ses.ws, cfg)
        log(f"Sheet stamp: {in_sheet:%d.%m.%Y %H:%M}" if in_sheet else "Sheet stamp: none")
        if in_sheet and latest["taken"].replace(second=0) <= in_sheet:
            log.status("up to date")
            return
    if args.check:
        set_output("need", "1")
        log.status("new snapshot")
        return
    update(cfg, ses, log, args.mode, latest, args)


def read_requests(ses, cfg):
    rq = cfg.get("requests")
    if not rq:
        return {}
    got = ses.sh.worksheet(rq["tab"]).batch_get([rq["orders"], rq["stock"]])
    val = lambda g: norm(g[0][0]) if g and g[0] else ""
    return {"orders": bool(val(got[0])), "stock": bool(val(got[1]))}


def clear_requests(ses, cfg, which):
    rq = cfg["requests"]
    cells = [rq[w] for w in which]
    if cells:
        ses.sh.worksheet(rq["tab"]).batch_clear(cells)


def loop(cfg, args, log):
    """Long job: every minute look for a new snapshot and for update requests from the sheet."""
    tz = ZoneInfo(cfg["schedule"]["tz"])
    sch = cfg["schedule"]
    deadline = time.time() + (args.minutes or sch.get("max_minutes", 340)) * 60
    ses = Session(cfg)
    done = sheet_stamp(ses.ws, cfg)
    # chat notes that go out together with a stock update (their settings come from the digest config)
    hook = None
    if os.environ.get("DIGEST_JSON"):
        import digest
        hook = digest.SyncHook(cfg, log)
    log.status("loop started")
    while time.time() < deadline:
        tick = time.time()
        now = dt.datetime.now(tz)
        if not args.any_time and not in_window(now, sch):
            if now.weekday() in sch["weekdays"] and 0 < minutes_to_start(now, sch) <= sch.get("early_minutes", 45):
                time.sleep(30)
                continue
            break
        try:
            latest = latest_snapshot(ses, cfg, log)
            req = read_requests(ses, cfg)
            new = bool(latest) and (done is None or latest["taken"].replace(second=0) > done)
            stock = (new or req.get("stock")) and latest is not None
            orders = new or req.get("orders")
            if stock or orders:
                mode = "all" if stock and orders else ("stock" if stock else "orders")
                note = False
                if hook and new:
                    try:
                        note = hook.due(ses.sh)
                    except Exception as e:
                        log.status(f"{now:%H:%M} chat note check failed: {type(e).__name__}")
                args.extra_statuses = hook.statuses() if note else []
                try:
                    update(cfg, ses, log, mode, latest, args)
                    if stock:
                        done = latest["taken"].replace(second=0)
                    log.status(f"{now:%H:%M} updated: {mode}" + (" (new snapshot)" if new else " (request)"))
                    if note:
                        try:
                            hook.send(ses.sh, getattr(ses, "orders", None))
                        except Exception as e:
                            log.status(f"{now:%H:%M} chat note failed: {type(e).__name__}")
                    # restock proposals follow every fresh stock update (sales are re-read once a day)
                    if stock and os.environ.get("RESTOCK_JSON"):
                        try:
                            import restock
                            restock.run_all(cfg, log=log.status)
                        except Exception as e:
                            log.status(f"{now:%H:%M} restock failed: {type(e).__name__}")
                finally:
                    clear_requests(ses, cfg, [w for w in ("orders", "stock") if req.get(w)])
        except Exception as e:
            log.status(f"{now:%H:%M} failed: {type(e).__name__}")
            if not log.quiet:
                import traceback
                traceback.print_exc()
            try:
                ses = Session(cfg)
            except Exception:
                pass
        # report requests written to the chat bot are answered from the db this job keeps up to date
        if hook:
            try:
                digest.poll_commands(cfg, log)
            except Exception as e:
                log.status(f"{now:%H:%M} chat requests failed: {type(e).__name__}")
        time.sleep(max(5, 60 - (time.time() - tick)))
    log.status("loop finished")


def update(cfg, ses, log, mode, latest, args):
    """mode: all = snapshot + orders, stock = snapshot only, orders = api + order file only."""
    do_stock, do_orders = mode in ("all", "stock"), mode in ("all", "orders")
    tz = ZoneInfo(cfg["schedule"]["tz"])
    sh, ws = ses.sh, ses.ws
    write = args.write and not args.row

    stock, known = {}, {}
    if do_stock:
        have = None if args.fresh else restored_time(cfg)
        if have and abs((have - latest["taken"]).total_seconds()) < 60:
            log("Database already restored from this snapshot")
        else:
            bak = Path(cfg["sql"]["backup_dir"]) / "snap.bak"
            t0 = time.time()
            download(ses.drive, latest["id"], bak)
            log(f"Downloaded in {time.time() - t0:.0f}s")
            t0 = time.time()
            restore(cfg, cfg["sql"].get("server_backup_path") or str(bak))
            log(f"Restored in {time.time() - t0:.0f}s")
        stock, known = read_stock(cfg)
        log(f"Articles in db: {len(known)}, with stock: {len(stock)}")

    # orders from the api: new articles first, because inserting rows shifts everything below
    settings = read_settings(sh, cfg) if do_orders and "settings" in cfg else None
    lines = None
    ses.orders = None
    if settings and "orders_api" in cfg:
        ids = {i for v in settings["api_columns"].values() for i in v} | set(settings["new_articles"])
        # statuses another consumer needs ride along in the same requests instead of separate ones
        ids = sorted(ids | {str(i) for i in getattr(args, "extra_statuses", [])}, key=int)
        t0 = time.time()
        try:
            orders, calls = fetch_orders(cfg, ids)
            ses.orders = orders
            lines = order_lines(orders)
            log(f"Orders api: {len(orders)} orders, {len(lines)} lines, {calls} requests, {time.time() - t0:.1f}s")
        except Exception as e:
            detail = str(e) if isinstance(e, RuntimeError) else ""
            log.status(f"orders api skipped: {type(e).__name__} {detail}".rstrip())
    new_articles = []
    if lines is not None:
        new_articles = add_new_articles(sh, ws, cfg, lines, settings, write)
        log(f"New articles from orders: {len(new_articles)}" + (f" ({', '.join(new_articles[:15])})" if new_articles else ""))

    file_totals = None
    if settings and "order_file" in cfg:
        t0 = time.time()
        try:
            file_totals, used = read_order_file(ses.gc, cfg, settings)
            log(f"Order file: {used} tabs, " + ", ".join(f"{k}: {len(v)} articles" for k, v in file_totals.items())
                + f", {time.time() - t0:.1f}s")
        except Exception as e:
            log.status(f"order file skipped: {type(e).__name__}")

    p = plan_sheet(ws, cfg, stock, known)
    if do_stock:
        report(p, log)

    # everything else is written as whole columns next to the stock data
    s = cfg["sheet"]
    grid = p["grid"]
    header = {}
    for i, v in enumerate(grid[s["header_row"] - 1]):
        header.setdefault(norm(v), i + 1)
    first = s["first_row"]
    last = last_article_row(grid, first)
    keys = {r: norm(grid[r - 1][0]).casefold() for r in range(first, last + 1) if norm(grid[r - 1][0])}
    extra, stamps_now = [], []

    def put(title, new):
        col = header.get(norm(title))
        if not col:
            log(f"No column: {title}")
            return
        rng, changed = column_update(grid, col, first, last, new)
        extra.append(rng)
        log(f"Column {col_letter(col)} {title}: {changed} cells change")

    name_col = header.get(norm(s["name_header"]))
    if do_stock and name_col:
        put(s["name_header"], {r: known[a] for r, a in keys.items()
                                if known.get(a) and not norm(grid[r - 1][name_col - 1])})
    if lines is not None:
        for title, ids in settings["api_columns"].items():
            ids = set(ids)
            per = {}
            for status, a, q, _ in lines:
                if status in ids:
                    per[a] = per.get(a, 0.0) + q
            put(title, {r: cell(per.get(a, 0)) for r, a in keys.items()})
        stamps_now.append(s["api_stamp_cell"])
        extra.append({"range": s["new_articles_stamp_cell"], "values": [[
            s["new_articles_stamp"].format(n=len(new_articles)) if new_articles else s["new_articles_stamp_none"]]]})
    if file_totals is not None:
        for title, per in file_totals.items():
            put(title, {r: cell(per.get(a, 0)) for r, a in keys.items()})
        stamps_now.append(s["file_stamp_cell"])

    if args.row:
        art = apply_row(ws, p, cfg, args.row)
        log.status(f"row {args.row} written ({art})" if art else f"row {args.row}: article not found in db")
    elif write:
        now_local = dt.datetime.now(tz)
        for c in stamps_now:
            extra.append({"range": c, "values": [[restamp(ws.acell(c).value, now_local)]]})
        if do_orders:
            fill_formulas(sh, ws, cfg, header, last)
        apply(sh, ws, p, cfg, latest["taken"] if do_stock else None, extra)
        if not args.loop:
            log.status("written")
    else:
        out = HERE / "reports" / f"dry-run {dt.datetime.now(tz):%Y%m%d-%H%M%S}.txt"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(log.lines), encoding="utf-8")
        log.status(f"dry run, report: {out.name}")


if __name__ == "__main__":
    main()
