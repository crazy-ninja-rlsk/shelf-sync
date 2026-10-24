"""New marketplace orders -> a chat note, one message per order.

    python market.py            # dry run: the notes for orders of the last days, to the console
    python market.py --send     # send them to the chat from the config (the sent cell is not touched)

Called every minute by the long sync job (digest.SyncHook.tick). Settings come from the digest config
block "market" (api base, chat key, sent cell, days to look back, order types); texts from its "texts".
The api key comes from MARKET_KEY (or the block's "key" locally). The highest order id already sent is
kept in a sheet cell: the first run only remembers the newest order, so old orders are not sent.
"""
import argparse
import datetime as dt
import os
from html import escape

import requests

import sync


def api(mcfg, path, params):
    key = (os.environ.get("MARKET_KEY") or mcfg.get("key") or "").strip()
    r = requests.get(mcfg["api"].rstrip("/") + "/" + path, params=params, timeout=60,
                     headers={"Authorization": "Bearer " + key, "Content-Language": mcfg.get("lang", "uk")})
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"market {path}: {r.status_code} {(j.get('errors') or {}).get('message', '')}")
    return j["content"]


def fetch(mcfg, now):
    """-> orders created in the last days, every type, by id"""
    since = (now - dt.timedelta(days=mcfg.get("days", 3))).strftime("%Y-%m-%d")
    out = {}
    for t in mcfg.get("types", [1, 2, 3]):
        page = 1
        while True:
            c = api(mcfg, "orders/search", {"type": t, "created_from": since, "page": page,
                                            "expand": "purchases,delivery,payment,count_buyer_orders"})
            for o in c["orders"]:
                out[o["id"]] = o
            if page >= c["_meta"]["pageCount"]:
                break
            page += 1
    return out


def stock_by_article(cfg):
    """-> {article: qty} from the restored db (all locations except the ignored ones), None without a db"""
    if not sync.restored_time(cfg):
        return None
    stock, _, _ = sync.read_stock(cfg)
    ignore = {sync.norm(v) for v in cfg.get("ignore_locations", [])}
    return {a: sum(q for loc, q in locs.items() if loc not in ignore and q > 0) for a, locs in stock.items()}


def num(v):
    return float(v or 0)


def person(title, phone):
    name = (title or {}).get("full_name") if isinstance(title, dict) else title
    phone = sync.norm(phone or "")
    if phone.isdigit() and len(phone) == 12:
        phone = "+" + phone
    return ", ".join(x for x in (sync.norm(name or ""), phone) if x)


def message(T, money, o, stock):
    lines = [T("m_title", id=o["id"], created=dt.datetime.fromisoformat(o["created"]).strftime("%d.%m %H:%M")), ""]
    fee = 0.0
    for p in o.get("purchases", []):
        item = p.get("item") or {}
        art = sync.norm(item.get("article") or "")
        cost = num(p.get("cost_with_discount") or p.get("cost"))
        # the market's own fee per unit when it has one, else the percent of the line
        fee += (num(item.get("commission_sum")) * num(p.get("quantity")) if item.get("commission_sum")
                else cost * num(item.get("commission_percent")) / 100)
        old = num(item.get("price_old"))
        price = num(p.get("price_with_discount") or p.get("price"))
        lines.append(T("m_line", name=escape(p.get("item_name") or item.get("name") or ""), art=escape(art),
                       qty=p.get("quantity"), sum=money(cost),
                       old=T("m_old", old=money(old)) if old > price else ""))
        if stock is not None and art:
            q = stock.get(art.casefold(), 0)
            lines.append(T("m_stock", qty=f"{q:g}") if q > 0 else T("m_nostock"))
    lines.append("")
    lines.append(T("m_total", sum=money(num(o.get("amount_with_discount") or o.get("amount"))), fee=money(fee)))
    buyer = person(o.get("user_title"), o.get("user_phone"))
    if buyer:
        lines.append(T("m_buyer", who=escape(buyer)))
    recipient = person(o.get("recipient_title"), o.get("recipient_phone"))
    if recipient and recipient != buyer:
        lines.append(T("m_recipient", who=escape(recipient)))
    d = o.get("delivery") or {}
    city = (d.get("city") or {}).get("name_ua") or (d.get("city") or {}).get("title") or ""
    place = " ".join(x for x in (d.get("place_street"), d.get("place_number")) if x)
    where = ", ".join(x for x in (d.get("delivery_service_name"), city, place) if x)
    if where:
        lines.append(T("m_delivery", where=escape(where)))
    pay = (o.get("payment") or {}).get("payment_method_name")
    if pay:
        lines.append(T("m_pay", pay=escape(pay)))
    if (o.get("comment") or "").strip():
        lines.append(T("m_comment", text=escape(o["comment"].strip())))
    n = int(o.get("count_buyer_orders") or 0)
    if n > 1:
        lines.append(T("m_repeat", n=n))
    return "\n".join(lines)


def read_cell(sh, ref):
    tab, cell = ref.split("!")
    return (sh.worksheet(tab).acell(cell).value or "").strip()


def write_cell(sh, ref, value):
    tab, cell = ref.split("!")
    sh.worksheet(tab).update_acell(cell, value)


def tick(cfg, dcfg, sh, now, log, send):
    """Send notes for orders newer than the one in the sent cell. send(texts) -> True when delivered."""
    import digest
    mcfg = dcfg["market"]
    if mcfg.get("from") and now.date().isoformat() < mcfg["from"]:
        return 0
    orders = fetch(mcfg, now)
    if not orders:
        return 0
    last = read_cell(sh, mcfg["sent_cell"])
    if not last.isdigit():
        write_cell(sh, mcfg["sent_cell"], str(max(orders)))
        log.status(f"{now:%H:%M} market orders: remembered the newest")
        return 0
    new = sorted(i for i in orders if i > int(last))
    if not new:
        return 0
    stock = stock_by_article(cfg)
    for i in new:
        if not send([message(digest.T, digest.money, orders[i], stock)]):
            break  # the next tick retries from this order
        write_cell(sh, mcfg["sent_cell"], str(i))
        log.status(f"{now:%H:%M} market order sent")
    return len(new)


def main():
    import digest
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    cfg = sync.load_config()
    dcfg = digest.load_digest_config()
    digest.TX.update(dcfg["texts"])
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo(dcfg["tz"]))
    orders = fetch(dcfg["market"], now)
    stock = stock_by_article(cfg)
    texts = [message(digest.T, digest.money, orders[i], stock) for i in sorted(orders)]
    if args.send:
        token = (os.environ.get("TELEGRAM_TOKEN") or dcfg["telegram"].get("token") or "").strip()
        digest.send_all(token, digest.resolve_chats(token, dcfg, dcfg["market"]["chat"]), texts, [],
                        sync.Log(False))
    else:
        print("\n\n----\n\n".join(texts) or "no orders")


if __name__ == "__main__":
    main()
