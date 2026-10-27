"""Gtech file parsing, SKU matching, aur order export logic."""
import csv
import io
import re
from datetime import datetime, timedelta, timezone

SKU_HINTS = ["sku", "item code", "itemcode", "item_code", "product code", "code", "article", "barcode", "item"]
QTY_HINTS = ["closing", "balance", "in hand", "inhand", "available", "stock", "qty", "quantity", "bal"]
SIZE_HINTS = ["size", "saiz"]


# ------------------------------------------------------------------ file reading
def _decode(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def read_table(filename, raw):
    """Returns list of rows (list of str). Supports csv / txt / xlsx / xls(as csv)."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)).strip()
                         for v in r])
        return rows
    text = _decode(raw)
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)]


def find_header(rows):
    """Gtech reports mein upar title lines hoti hain; header row dhoondo."""
    best, best_score = 0, -1
    for i, row in enumerate(rows[:30]):
        cells = [c.lower() for c in row if c]
        if not cells:
            continue
        score = 0
        for c in cells:
            if any(h in c for h in SKU_HINTS):
                score += 2
            if any(h in c for h in QTY_HINTS):
                score += 2
        score += min(len(cells), 6) * 0.1
        if score > best_score:
            best, best_score = i, score
    return best


def guess_column(headers, hints):
    low = [h.lower().strip() for h in headers]
    for hint in hints:
        for i, h in enumerate(low):
            if h == hint:
                return i
    for hint in hints:
        for i, h in enumerate(low):
            if hint in h:
                return i
    return None


def parse_qty(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except ValueError:
        return None
    n = -n if neg else n
    return int(round(n))


# ------------------------------------------------------------------ matching
def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def split_code(code, known_skus):
    """'KHD-301-M' / 'KHD-301 M' / 'KHD-301/M' -> ('KHD-301','M'). Longest known SKU wins."""
    c = norm(code)
    if c in known_skus:
        return c, ""
    best = None
    for sku in known_skus:
        if c.startswith(sku) and len(c) > len(sku) and c[len(sku)] in " -_/.(":
            if best is None or len(sku) > len(best):
                best = sku
    if best:
        size = c[len(best):].strip(" -_/.()")
        return best, size
    return c, ""


SEPS = " -_/.("


def candidate_skus(code):
    """'KHD-301-M' -> {'KHD-301-M','KHD-301','KHD'}: Shopify se in exact SKUs ko dhoonda jata hai."""
    c = norm(code)
    out = set()
    if not c:
        return out
    out.add(c)
    cur = c
    for _ in range(2):
        idx = max(cur.rfind(ch) for ch in SEPS)
        if idx <= 0:
            break
        cur = cur[:idx].strip(SEPS)
        if cur:
            out.add(cur)
    return out


def gtech_codes(gtech_rows, sku_col):
    codes = set()
    for r in gtech_rows:
        code = r[sku_col] if sku_col < len(r) else ""
        codes |= candidate_skus(code)
    return codes


def build_preview(variants, gtech_rows, sku_col, qty_col, size_col=None,
                  zero_missing=False, negative_to_zero=True, sum_duplicates=True):
    known_skus = {norm(v["sku"]) for v in variants}

    # Shopify index
    by_key, by_sku = {}, {}
    for v in variants:
        k = (norm(v["sku"]), norm(v["size"]))
        by_key.setdefault(k, []).append(v)
        by_sku.setdefault(norm(v["sku"]), []).append(v)

    # Gtech quantities per key
    gq, notes, raw_order = {}, {}, []
    ignored = 0
    for r in gtech_rows:
        code = r[sku_col] if sku_col < len(r) else ""
        if not norm(code):
            ignored += 1
            continue
        qty = parse_qty(r[qty_col] if qty_col < len(r) else "")
        if qty is None:
            qty = 0
        if size_col is not None and size_col < len(r) and r[size_col].strip():
            sku, size = norm(code), norm(r[size_col])
            if sku not in known_skus:
                sku2, size2 = split_code(code, known_skus)
                if size2 == "":
                    sku = sku2
        else:
            sku, size = split_code(code, known_skus)
        key = (sku, size)
        if key in gq:
            if sum_duplicates:
                gq[key] += qty
                notes[key] = "Gtech file mein ye item ek se zyada baar tha, qty jama ki gayi"
            else:
                gq[key] = qty
        else:
            gq[key] = qty
            raw_order.append(key)

    rows, used_variants = [], set()

    def add(v, gtech_qty, status, note=""):
        cur = v["available"] if v else None
        target = gtech_qty
        if target is not None and negative_to_zero and target < 0:
            target = 0
        change = (target - (cur or 0)) if (v and target is not None) else None
        rows.append({
            "sku": v["sku"] if v else "",
            "size": v["size"] if v else "",
            "product": v["product"] if v else "",
            "shopify_qty": cur,
            "gtech_qty": gtech_qty,
            "new_qty": target,
            "change": change,
            "status": status,
            "note": note,
            "inventory_item_id": v["inventory_item_id"] if v else None,
            "variant_id": v["variant_id"] if v else None,
        })

    for key in raw_order:
        sku, size = key
        qty = gq[key]
        note = notes.get(key, "")
        targets = []
        if size:
            targets = by_key.get(key, [])
            if not targets:
                if sku in by_sku:
                    rows.append(_missing(sku, size, qty, "Size Shopify par nahi mila"))
                else:
                    rows.append(_missing(sku, size, qty, "SKU Shopify par nahi mila"))
                continue
        else:
            vs = by_sku.get(sku, [])
            if not vs:
                rows.append(_missing(sku, "", qty, "SKU Shopify par nahi mila"))
                continue
            sizes = {norm(v["size"]) for v in vs}
            if len(sizes) > 1:
                rows.append(_missing(sku, "", qty,
                                     f"Shopify par is SKU ke {len(sizes)} sizes hain — Gtech file mein size nahi, skip kiya"))
                continue
            targets = vs
        if len(targets) > 1:
            note = (note + "; " if note else "") + f"Ye SKU+size {len(targets)} products par hai (sab ko same qty milegi)"
        for v in targets:
            used_variants.add(v["variant_id"])
            if not v["tracked"]:
                add(v, qty, "ERROR", "Shopify par inventory tracking off hai")
            elif not v["stocked"]:
                add(v, qty, "ERROR", "Is location par stocked nahi")
            else:
                status = "SAME" if (v["available"] == (0 if (negative_to_zero and qty < 0) else qty)) else "UPDATE"
                add(v, qty, status, note)

    for v in variants:
        if v["variant_id"] in used_variants:
            continue
        if zero_missing and v["tracked"] and v["stocked"]:
            add(v, 0, "UPDATE" if (v["available"] or 0) != 0 else "SAME", "Ye size Gtech file mein nahi tha, 0 kiya ja raha hai")
        else:
            add(v, None, "NOT_IN_GTECH", "Is SKU ka ye size Gtech file mein nahi (koi change nahi)")

    order = {"UPDATE": 0, "ERROR": 1, "NO_MATCH": 2, "NOT_IN_GTECH": 3, "SAME": 4}
    rows.sort(key=lambda x: (order.get(x["status"], 9), x["sku"], x["size"]))
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in order}
    summary["ignored_blank_rows"] = ignored
    return rows, summary


def _missing(sku, size, qty, note):
    return {"sku": sku, "size": size, "product": "", "shopify_qty": None, "gtech_qty": qty,
            "new_qty": None, "change": None, "status": "NO_MATCH", "note": note,
            "inventory_item_id": None, "variant_id": None}


# ------------------------------------------------------------------ orders export
ORDER_FIELDS = {
    "date": "Order date",
    "order_no": "Order number (#1234)",
    "customer": "Customer name",
    "phone": "Phone",
    "city": "City",
    "address": "Address",
    "item_code": "Item code (format setting se)",
    "sku": "SKU",
    "size": "Size",
    "product": "Product name",
    "qty": "Quantity",
    "rate": "Rate (unit price)",
    "discount": "Discount (line)",
    "amount": "Amount (line, discount ke baad)",
    "shipping": "Shipping (sirf pehli line par)",
    "order_total": "Order total (sirf pehli line par)",
    "payment": "Payment method",
    "financial_status": "Payment status",
    "fulfillment_status": "Fulfillment status",
}


def _money(x):
    try:
        return float((x or {}).get("shopMoney", {}).get("amount", 0))
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(n):
    return str(int(n)) if float(n).is_integer() else f"{n:.2f}"


def order_rows(orders, cfg, export_tag, opts):
    filters = [norm(x) for x in re.split(r"[,\n]", opts.get("sku_filter") or "") if norm(x)]
    offset = timedelta(hours=float(cfg.get("utc_offset_hours", 5)))
    date_fmt = cfg.get("date_format", "%d-%m-%Y")
    code_fmt = cfg.get("item_code_format", "{sku}-{size}")
    rows, skipped = [], {"cancelled": 0, "already_exported": 0, "no_matching_items": 0, "no_sku_lines": 0}
    included_ids = []
    for o in orders:
        if opts.get("skip_cancelled", True) and o.get("cancelledAt"):
            skipped["cancelled"] += 1
            continue
        if opts.get("skip_exported", True) and export_tag in (o.get("tags") or []):
            skipped["already_exported"] += 1
            continue
        dt = datetime.strptime(o["createdAt"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        local = dt.astimezone(timezone(offset))
        cust = (o.get("customer") or {}).get("displayName") or ""
        addr = o.get("shippingAddress") or {}
        if not cust:
            cust = re.sub(r"\s*\(.*?\)\s*$", "", addr.get("name") or "")
        lines = []
        for li in o["lineItems"]["nodes"]:
            sku = (li.get("sku") or "").strip()
            if not sku:
                skipped["no_sku_lines"] += 1
                continue
            if filters and not any(norm(sku).startswith(f) for f in filters):
                continue
            q = li.get("currentQuantity")
            if q is None:
                q = li.get("quantity", 0)
            if q <= 0:
                continue
            rate = _money(li.get("originalUnitPriceSet"))
            gross = rate * li.get("quantity", q)
            amount_full = _money(li.get("discountedTotalSet"))
            # discountedTotal original qty par hota hai, current qty ke hisaab se proportion
            orig_q = li.get("quantity") or q
            amount = amount_full * q / orig_q if orig_q else amount_full
            discount = max(0.0, (gross - amount_full) * q / orig_q) if orig_q else 0.0
            size = (li.get("variantTitle") or "").strip()
            if size == "Default Title":
                size = ""
            item_code = code_fmt.format(sku=sku, size=size).strip(" -_/") if size else sku
            lines.append({
                "date": local.strftime(date_fmt),
                "order_no": o["name"],
                "customer": cust,
                "phone": addr.get("phone") or "",
                "city": addr.get("city") or "",
                "address": " ".join(x for x in [addr.get("address1"), addr.get("address2")] if x),
                "item_code": item_code,
                "sku": sku,
                "size": size,
                "product": li.get("title") or li.get("name") or "",
                "qty": str(q),
                "rate": _fmt_num(rate),
                "discount": _fmt_num(round(discount, 2)),
                "amount": _fmt_num(round(amount, 2)),
                "shipping": "",
                "order_total": "",
                "payment": ", ".join(o.get("paymentGatewayNames") or []),
                "financial_status": o.get("displayFinancialStatus") or "",
                "fulfillment_status": o.get("displayFulfillmentStatus") or "",
            })
        if not lines:
            skipped["no_matching_items"] += 1
            continue
        lines[0]["shipping"] = _fmt_num(_money(o.get("totalShippingPriceSet")))
        lines[0]["order_total"] = _fmt_num(_money(o.get("totalPriceSet")))
        rows.extend(lines)
        included_ids.append(o["id"])
    return rows, skipped, included_ids


def rows_to_csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c["header"] for c in columns])
    for r in rows:
        w.writerow([r.get(c["field"], "") for c in columns])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ sale report
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "XXXL", "3XL", "4XL", "FREE", ""]


def _size_key(s):
    u = norm(s)
    return (SIZE_ORDER.index(u) if u in SIZE_ORDER else 50, u)


def sales_report(orders, sku_terms, variants, cfg, include_cancelled=False):
    """SKU ki size-wise sale. sku_terms: ['KHD-301', ...]"""
    terms = [norm(t) for t in sku_terms if norm(t)]
    offset = timedelta(hours=float(cfg.get("utc_offset_hours", 5)))
    date_fmt = cfg.get("date_format", "%d-%m-%Y")

    def match(sku):
        u = norm(sku)
        for t in terms:
            if u == t or (u.startswith(t) and len(u) > len(t) and u[len(t)] in SEPS):
                return t
        return None

    agg, lines = {}, []
    totals = {"orders": set(), "qty": 0, "amount": 0.0, "returned": 0, "cancelled_qty": 0}
    for o in orders:
        refunded = {}
        for rf in o.get("refunds") or []:
            for rl in ((rf.get("refundLineItems") or {}).get("nodes") or []):
                lid = (rl.get("lineItem") or {}).get("id")
                if lid:
                    refunded[lid] = refunded.get(lid, 0) + (rl.get("quantity") or 0)
        cancelled = bool(o.get("cancelledAt"))
        dt = datetime.strptime(o["createdAt"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone(offset))
        for li in o["lineItems"]["nodes"]:
            t = match(li.get("sku"))
            if not t:
                continue
            size = (li.get("variantTitle") or "").strip()
            if size == "Default Title":
                size = ""
            sku_n = norm(li.get("sku"))
            if sku_n != t and not size:  # 'KHD-301-M' style SKU
                size = sku_n[len(t):].strip(SEPS)
            key = (t, norm(size))
            a = agg.setdefault(key, {"sku": t, "size": size, "orders": set(), "qty": 0, "amount": 0.0,
                                     "returned": 0, "cancelled_qty": 0, "unfulfilled": 0})
            q = li.get("currentQuantity")
            if q is None:
                q = li.get("quantity", 0)
            orig_q = li.get("quantity") or q or 1
            amount = _money(li.get("discountedTotalSet")) * (q / orig_q if orig_q else 1)
            ret = refunded.get(li.get("id"), 0)
            status = "Cancelled" if cancelled else (o.get("displayFulfillmentStatus") or "")
            lines.append({"date": dt.strftime(date_fmt), "order_no": o["name"], "sku": li.get("sku"), "size": size,
                          "qty": li.get("quantity", 0) if cancelled else q, "amount": round(amount, 2),
                          "returned": ret, "status": status, "payment": o.get("displayFinancialStatus") or ""})
            if cancelled:
                a["cancelled_qty"] += li.get("quantity", 0)
                totals["cancelled_qty"] += li.get("quantity", 0)
                if not include_cancelled:
                    continue
            if q <= 0:
                continue
            a["orders"].add(o["id"])
            a["qty"] += q
            a["amount"] += amount
            a["returned"] += ret
            if (o.get("displayFulfillmentStatus") or "").upper() in ("UNFULFILLED", "PARTIALLY_FULFILLED", "ON_HOLD", "SCHEDULED"):
                a["unfulfilled"] += q
            totals["orders"].add(o["id"])
            totals["qty"] += q
            totals["amount"] += amount
            totals["returned"] += ret

    # stock (Shopify)
    stock = {}
    for v in variants:
        k = (norm(v["sku"]), norm(v["size"]))
        s = stock.setdefault(k, {"available": None, "listings": 0, "product": v["product"]})
        s["listings"] += 1
        if v["available"] is not None:
            s["available"] = v["available"] if s["available"] is None else max(s["available"], v["available"])
    for k, s in stock.items():
        if k[0] in terms and k not in agg:
            agg[k] = {"sku": k[0], "size": k[1], "orders": set(), "qty": 0, "amount": 0.0,
                      "returned": 0, "cancelled_qty": 0, "unfulfilled": 0}

    rows = []
    for k, a in agg.items():
        s = stock.get(k, {})
        rows.append({"sku": a["sku"], "size": a["size"], "product": s.get("product", ""),
                     "orders": len(a["orders"]), "qty": a["qty"], "net_qty": a["qty"] - a["returned"],
                     "returned": a["returned"], "unfulfilled": a["unfulfilled"], "cancelled_qty": a["cancelled_qty"],
                     "amount": round(a["amount"], 2), "stock": s.get("available"), "listings": s.get("listings", 0)})
    rows.sort(key=lambda r: (r["sku"], _size_key(r["size"])))
    lines.sort(key=lambda r: r["order_no"], reverse=True)
    return {"rows": rows, "lines": lines,
            "totals": {"orders": len(totals["orders"]), "qty": totals["qty"], "amount": round(totals["amount"], 2),
                       "returned": totals["returned"], "net_qty": totals["qty"] - totals["returned"],
                       "cancelled_qty": totals["cancelled_qty"]}}
