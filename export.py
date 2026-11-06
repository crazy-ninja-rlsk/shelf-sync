"""Small read-only copy of the working db, sent to a host after each fresh snapshot.

Everything specific (tables, queries, target) lives in EXPORT_JSON or export.local.json:
  {"tables": [{"name": "...", "create": "column defs", "query": "SELECT ..."}],
   "indexes": ["CREATE INDEX ..."],
   "target": {"host": "...", "port": 22, "user": "...", "key": "...", "known_hosts": "..."}}

python export.py --file out.db   builds the file and sends nothing.
"""
import argparse
import datetime as dt
import decimal
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import sync

HERE = Path(__file__).resolve().parent
CHUNK = 5000


def load_spec():
    raw = os.environ.get("EXPORT_JSON") or (HERE / "export.local.json").read_text(encoding="utf-8-sig")
    return json.loads(raw)


def plain(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def build(cfg, spec, path, taken=None):
    """Write the sqlite file, return {table: rows}."""
    path = Path(path)
    path.unlink(missing_ok=True)
    counts = {}
    with sync.sql(cfg, cfg["sql"]["db"]) as src, sqlite3.connect(path) as out:
        out.execute("PRAGMA journal_mode=OFF")
        out.execute("PRAGMA synchronous=OFF")
        for t in spec["tables"]:
            out.execute(f'CREATE TABLE {t["name"]} ({t["create"]})')
            cur = src.cursor()
            cur.execute(t["query"])
            marks = ",".join("?" * len(cur.description))
            total = 0
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                out.executemany(f'INSERT INTO {t["name"]} VALUES ({marks})',
                                [[plain(v) for v in r] for r in rows])
                total += len(rows)
            counts[t["name"]] = total
        for q in spec.get("indexes", []):
            out.execute(q)
        out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta = {"snapshot": taken.isoformat() if taken else "", "built_at": stamp}
        meta.update({f"rows_{k}": str(v) for k, v in counts.items()})
        out.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
        out.commit()
    with sqlite3.connect(path) as out:
        out.execute("VACUUM")
    return counts


def send(spec, path):
    """Pipe the gzipped file to the host; the key there runs one fixed command."""
    t = spec["target"]
    tmp = Path(tempfile.mkdtemp())
    try:
        key = tmp / "k"
        hosts = tmp / "h"
        key.write_text(t["key"].rstrip() + "\n", encoding="ascii")
        key.chmod(0o600)
        hosts.write_text(t["known_hosts"].rstrip() + "\n", encoding="ascii")
        packed = tmp / "p.gz"
        with open(path, "rb") as a, gzip.open(packed, "wb", compresslevel=6) as b:
            shutil.copyfileobj(a, b)
        cmd = ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts}",
               "-o", "ConnectTimeout=20", "-p", str(t.get("port", 22)), f'{t["user"]}@{t["host"]}']
        with open(packed, "rb") as data:
            done = subprocess.run(cmd, stdin=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
        if done.returncode:
            raise RuntimeError(f"send exit {done.returncode}")
        return packed.stat().st_size
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(cfg, taken=None, log=print):
    spec = load_spec()
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "copy.db"
        build(cfg, spec, path, taken)
        size = send(spec, path)
    log(f"copy sent: {size // 1024} KB in {time.time() - t0:.0f} s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    a = ap.parse_args()
    counts = build(sync.load_config(), load_spec(), a.file)
    print(counts, Path(a.file).stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
