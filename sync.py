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
        f["ts"] = snapshot_time(f)
    files = [f for f in files if f["ts"]]
    files.sort(key=lambda f: f["ts"], reverse=True)
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
    """-> ({article: {location: qty}}, {all article keys known to the db})"""
    with sql(cfg, cfg["sql"]["db"]) as cn:
        known = {norm(r[0]).casefold() for r in cn.execute(cfg["sql"]["articles_query"]).fetchall() if norm(r[0])}
        stock = {}
        for a, loc, q in cn.execute(cfg["sql"]["stock_query"]).fetchall():
            a, loc = norm(a).casefold(), norm(loc)
            if a and loc and q:
                stock.setdefault(a, {})
                stock[a][loc] = stock[a].get(loc, 0.0) + float(q)
    return stock, known


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
        vals = {name: per.get(name, 0.0) for name in new_cols if name and name not in ignore}
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


def apply(sh, ws, p, cfg, stamp):
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
        old = ws.acell(stamp_cell).value or ""
        new = re.sub(r"\d{2}\.\d{2}\.\d{4}", stamp.strftime("%d.%m.%Y"), old, count=1)
        new = re.sub(r"\d{1,2}:\d{2}", stamp.strftime("%H:%M"), new, count=1)
        data.append({"range": stamp_cell, "values": [[new]]})
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
    return now.weekday() in sch["weekdays"] and sch["from"] <= hm <= sch["to"]


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true", help="only tell whether a new snapshot is waiting")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--reprocess", action="store_true")
    ap.add_argument("--row", type=int, help="test: write only this sheet row")
    ap.add_argument("--sheet", help="test: use another spreadsheet id")
    args = ap.parse_args()
    if args.row:
        args.any_time = args.reprocess = True

    cfg = load_config()
    if args.sheet:
        cfg["sheet"]["id"] = args.sheet
    if os.environ.get("BACKUP_DIR"):
        cfg["sql"]["backup_dir"] = os.environ["BACKUP_DIR"]
    log = Log(cfg.get("quiet", False))
    try:
        run_once(cfg, args, log)
    except Exception as e:
        if not log.quiet:
            raise
        log.status(f"failed: {type(e).__name__}")
        sys.exit(1)


def run_once(cfg, args, log):
    set_output("need", "0")
    tz = ZoneInfo(cfg["schedule"]["tz"])
    now = dt.datetime.now(tz)
    if not args.any_time and not in_window(now, cfg["schedule"]):
        log.status("outside window")
        return

    creds = credentials(cfg)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    files = list_snapshots(drive, cfg)
    if not files:
        log.status("no snapshots")
        return
    latest = files[0]
    log(f"Latest snapshot: {latest['ts']:%d.%m.%Y %H:%M:%S}, {int(latest['size']) / 2**20:.0f} MB")

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(cfg["sheet"]["id"])
    ws = sh.get_worksheet_by_id(cfg["sheet"]["gid"])
    in_sheet = sheet_stamp(ws, cfg)
    log(f"Sheet stamp: {in_sheet:%d.%m.%Y %H:%M}" if in_sheet else "Sheet stamp: none")
    if in_sheet and latest["ts"].replace(second=0) <= in_sheet and not args.reprocess:
        log.status("up to date")
        return
    if args.check:
        set_output("need", "1")
        log.status("new snapshot")
        return

    have = restored_time(cfg)
    if have and abs((have - latest["ts"]).total_seconds()) < 60:
        log("Database already restored from this snapshot")
    else:
        bak = Path(cfg["sql"]["backup_dir"]) / "snap.bak"
        t0 = dt.datetime.now()
        download(drive, latest["id"], bak)
        log(f"Downloaded in {(dt.datetime.now() - t0).seconds}s")
        t0 = dt.datetime.now()
        restore(cfg, cfg["sql"].get("server_backup_path") or str(bak))
        log(f"Restored in {(dt.datetime.now() - t0).seconds}s")

    stock, known = read_stock(cfg)
    log(f"Articles in db: {len(known)}, with stock: {len(stock)}")
    p = plan_sheet(ws, cfg, stock, known)
    report(p, log)

    if args.row:
        art = apply_row(ws, p, cfg, args.row)
        log.status(f"row {args.row} written ({art})" if art else f"row {args.row}: article not found in db")
    elif args.write:
        apply(sh, ws, p, cfg, latest["ts"])
        log.status("written")
    else:
        out = HERE / "reports" / f"dry-run {now:%Y%m%d-%H%M}.txt"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(log.lines), encoding="utf-8")
        log.status(f"dry run, report: {out.name}")


if __name__ == "__main__":
    main()
