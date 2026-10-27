"""
Gtech <-> Shopify Sync  (Vercel version)
Settings Vercel ke Environment Variables se aati hain — README dekhein.
"""
import hmac
import os
from datetime import timedelta

from flask import Flask, jsonify, request, session, redirect, Response

import sync_logic as L
from shopify_client import Shopify, ShopifyError

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-change-me"
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
                  MAX_CONTENT_LENGTH=4 * 1024 * 1024)

DEFAULT_COLUMNS = [
    {"header": "Date", "field": "date"},
    {"header": "Invoice No", "field": "order_no"},
    {"header": "Customer", "field": "customer"},
    {"header": "Phone", "field": "phone"},
    {"header": "City", "field": "city"},
    {"header": "Item Code", "field": "item_code"},
    {"header": "SKU", "field": "sku"},
    {"header": "Size", "field": "size"},
    {"header": "Product", "field": "product"},
    {"header": "Qty", "field": "qty"},
    {"header": "Rate", "field": "rate"},
    {"header": "Discount", "field": "discount"},
    {"header": "Amount", "field": "amount"},
    {"header": "Shipping", "field": "shipping"},
    {"header": "Payment", "field": "payment"},
]


def cfg():
    e = os.environ.get
    return {
        "store": e("SHOPIFY_STORE", ""),
        "access_token": e("SHOPIFY_ACCESS_TOKEN", ""),
        "client_id": e("SHOPIFY_CLIENT_ID", ""),
        "client_secret": e("SHOPIFY_CLIENT_SECRET", ""),
        "location_id": e("SHOPIFY_LOCATION_ID", ""),
        "order_sku_filter": e("ORDER_SKU_FILTER", ""),
        "item_code_format": e("ITEM_CODE_FORMAT", "{sku}-{size}"),
        "date_format": e("DATE_FORMAT", "%d-%m-%Y"),
        "utc_offset_hours": float(e("UTC_OFFSET_HOURS", "5")),
        "export_tag": e("EXPORT_TAG", "gtech-exported"),
    }


def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def shop():
    return Shopify(cfg())


# ------------------------------------------------------------------ login
LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login · Gtech Sync</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#12312e;font:15px "Segoe UI",system-ui,sans-serif}
form{background:#fff;padding:28px 30px;border-radius:12px;width:320px}
h1{font-size:18px;margin:0 0 4px}p{color:#6b7078;font-size:13px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd;border-radius:6px;font:inherit;margin-bottom:12px}
button{width:100%;padding:10px;border:0;border-radius:6px;background:#0f766e;color:#fff;font:inherit;font-weight:600;cursor:pointer}
.e{color:#b91c1c;font-size:13px;margin-bottom:10px}</style></head><body>
<form method="post"><h1>Gtech ⇄ Shopify Sync</h1><p>Password daalein</p>__ERR__
<input type="password" name="password" autofocus placeholder="Password"><button>Login</button></form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return Response(LOGIN_HTML.replace("__ERR__", '<div class="e">Vercel mein APP_PASSWORD set nahi hai.</div>'),
                        mimetype="text/html")
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), pw):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        error = '<div class="e">Ghalat password</div>'
    return Response(LOGIN_HTML.replace("__ERR__", error), mimetype="text/html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.before_request
def guard():
    if request.path in ("/login", "/favicon.ico"):
        return None
    if not session.get("ok"):
        if request.path.startswith("/api/"):
            return err("Login expire ho gaya. Page refresh karein.", 401)
        return redirect("/login")
    return None


# ------------------------------------------------------------------ config / locations
@app.get("/api/config")
def api_config():
    c = cfg()
    return jsonify({"ok": True, "store": c["store"], "order_sku_filter": c["order_sku_filter"],
                    "item_code_format": c["item_code_format"], "date_format": c["date_format"],
                    "export_tag": c["export_tag"], "env_location_id": c["location_id"],
                    "default_columns": DEFAULT_COLUMNS, "order_fields": L.ORDER_FIELDS})


@app.get("/api/locations")
def api_locations():
    try:
        s, locs = shop().shop_info()
        return jsonify({"ok": True, "shop": s, "locations": locs})
    except ShopifyError as e:
        return err(e)
    except Exception as e:
        return err(f"Connection error: {e}")


# ------------------------------------------------------------------ inventory
def _read_upload():
    f = request.files.get("file")
    if not f:
        raise ValueError("File select karein")
    rows = [r for r in L.read_table(f.filename, f.read()) if any(c for c in r)]
    if not rows:
        raise ValueError("File khali hai")
    h = L.find_header(rows)
    return rows[h], rows[h + 1:]


@app.post("/api/inventory/upload")
def inv_upload():
    try:
        headers, data = _read_upload()
    except Exception as e:
        return err(f"File parh nahi saka: {e}")
    return jsonify({"ok": True, "headers": headers, "sample": data[:8], "count": len(data),
                    "guess": {"sku": L.guess_column(headers, L.SKU_HINTS),
                              "qty": L.guess_column(headers, L.QTY_HINTS),
                              "size": L.guess_column(headers, L.SIZE_HINTS),
                              "barcode": L.guess_column(headers, L.BARCODE_HINTS)}})


@app.post("/api/inventory/preview")
def inv_preview():
    try:
        headers, data = _read_upload()
    except Exception as e:
        return err(f"File parh nahi saka: {e}")
    c = cfg()
    loc = request.form.get("location_id") or c["location_id"]
    if not loc:
        return err("Location select karein")
    f = request.form
    sku_col = int(f["sku_col"])
    codes = L.gtech_codes(data, sku_col)
    if not codes:
        return err("File ke SKU column mein koi SKU nahi mila")
    try:
        variants = shop().variants_by_skus(codes, loc)
    except ShopifyError as e:
        return err(e)
    size_col = f.get("size_col")
    rows, summary = L.build_preview(
        variants, data, sku_col, int(f["qty_col"]),
        None if size_col in (None, "", "-1") else int(size_col),
        zero_missing=f.get("zero_missing") == "1",
        negative_to_zero=f.get("negative_to_zero", "1") == "1",
        sum_duplicates=f.get("sum_duplicates", "1") == "1",
    )
    return jsonify({"ok": True, "rows": rows, "summary": summary, "shopify_variants": len(variants)})


@app.post("/api/inventory/apply")
def inv_apply():
    b = request.get_json(force=True)
    c = cfg()
    loc = b.get("location_id") or c["location_id"]
    items = b.get("items") or []
    if not loc or not items:
        return err("Kuch update karne ko nahi")
    clean = []
    for it in items:
        if not str(it.get("inventory_item_id", "")).startswith("gid://shopify/InventoryItem/"):
            continue
        clean.append({"inventory_item_id": it["inventory_item_id"], "quantity": max(0, int(it["quantity"])),
                      "change_from": None if it.get("change_from") is None else int(it["change_from"])})
    try:
        errors = shop().set_quantities(loc, clean)
    except ShopifyError as e:
        return err(e)
    out = []
    for e in errors:
        msg = e["message"]
        if e.get("code") == "CHANGE_FROM_QUANTITY_STALE":
            msg = "Preview ke baad Shopify par qty badal gayi (naya order aaya hoga). Dobara preview kar ke apply karein."
        out.append({"inventory_item_id": (e.get("item") or {}).get("inventory_item_id"), "message": msg})
    failed = {e["inventory_item_id"] for e in out if e["inventory_item_id"]}
    return jsonify({"ok": True, "updated": len(clean) - len(failed), "errors": out})


# ------------------------------------------------------------------ orders
@app.post("/api/orders/preview")
def orders_preview():
    b = request.get_json(force=True)
    c = cfg()
    d_from, d_to = b.get("from"), b.get("to")
    if not d_from or not d_to:
        return err("Dates select karein")
    search = f"created_at:>={d_from} created_at:<={d_to}"
    ff = b.get("fulfillment", "any")
    if ff == "unfulfilled":
        search += " fulfillment_status:unfulfilled"
    elif ff == "fulfilled":
        search += " fulfillment_status:shipped"
    try:
        orders = shop().orders(search)
    except ShopifyError as e:
        return err(e)
    rows, skipped, ids = L.order_rows(orders, c, c["export_tag"], {
        "skip_cancelled": b.get("skip_cancelled", True),
        "skip_exported": b.get("skip_exported", True),
        "sku_filter": b.get("sku_filter", ""),
    })
    return jsonify({"ok": True, "rows": rows, "skipped": skipped, "order_ids": ids,
                    "orders_found": len(orders), "orders_included": len(ids)})


@app.post("/api/orders/mark")
def orders_mark():
    ids = [i for i in (request.get_json(force=True).get("order_ids") or [])
           if str(i).startswith("gid://shopify/Order/")]
    if not ids:
        return jsonify({"ok": True, "marked": 0, "errors": []})
    try:
        errors = shop().add_tag(ids, cfg()["export_tag"])
    except ShopifyError as e:
        return err(f"Orders par tag nahi laga: {e}")
    return jsonify({"ok": True, "marked": len(ids) - len(errors), "errors": errors})


@app.get("/api/orders/history")
def orders_history():
    try:
        items = shop().orders_by_tag(cfg()["export_tag"])
    except ShopifyError as e:
        return err(e)
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.post("/api/orders/unmark")
def orders_unmark():
    names = request.get_json(force=True).get("names") or []
    try:
        sh = shop()
        found = sh.order_ids_by_names(names)
        errors = sh.remove_tag(list(found.values()), cfg()["export_tag"]) if found else []
    except ShopifyError as e:
        return err(e)
    return jsonify({"ok": True, "unmarked": list(found.keys()), "errors": errors})


# ------------------------------------------------------------------ manual stock edit
@app.post("/api/stock/lookup")
def stock_lookup():
    import re as _re
    b = request.get_json(force=True)
    c = cfg()
    terms = [t.strip() for t in _re.split(r"[,\n]", b.get("sku") or "") if t.strip()][:10]
    if not terms:
        return err("SKU likhein (jaise KHD-301)")
    loc = b.get("location_id") or c["location_id"]
    if not loc:
        return err("Pehle Settings mein location select karein")
    try:
        variants = shop().variants_by_skus(terms, loc)
    except ShopifyError as e:
        return err(e)
    groups = {}
    for v in variants:
        k = (L.norm(v["sku"]), L.norm(v["size"]))
        g = groups.setdefault(k, {"sku": v["sku"], "size": v["size"], "products": [], "items": []})
        if v["product"] not in g["products"]:
            g["products"].append(v["product"])
        g["items"].append({"inventory_item_id": v["inventory_item_id"], "available": v["available"],
                           "tracked": v["tracked"], "stocked": v["stocked"], "product": v["product"]})
    rows = []
    for g in groups.values():
        ok = [i for i in g["items"] if i["tracked"] and i["stocked"]]
        qtys = [i["available"] for i in ok if i["available"] is not None]
        g["current"] = max(qtys) if qtys else None
        g["mismatch"] = len(set(qtys)) > 1
        g["problem"] = "" if ok else ("Tracking off hai" if not any(i["tracked"] for i in g["items"]) else "Is location par stocked nahi")
        rows.append(g)
    rows.sort(key=lambda r: (L.norm(r["sku"]), L._size_key(r["size"])))
    found = {L.norm(v["sku"]) for v in variants}
    missing = [t for t in terms if L.norm(t) not in found]
    return jsonify({"ok": True, "rows": rows, "missing": missing})


# ------------------------------------------------------------------ sale report
@app.post("/api/sales")
def sales():
    import re as _re
    b = request.get_json(force=True)
    c = cfg()
    terms = [t.strip() for t in _re.split(r"[,\n]", b.get("sku") or "") if t.strip()][:10]
    if not terms:
        return err("SKU likhein (jaise KHD-301)")
    d_from, d_to = b.get("from"), b.get("to")
    if not d_from or not d_to:
        return err("Dates select karein")
    loc = b.get("location_id") or c["location_id"]
    try:
        sh = shop()
        orders, seen = [], set()
        for t in terms:
            q = f'sku:"{t.replace(chr(34), "")}" created_at:>={d_from} created_at:<={d_to}'
            for o in sh.orders(q):
                if o["id"] not in seen:
                    seen.add(o["id"])
                    orders.append(o)
        variants = sh.variants_by_skus(terms, loc) if loc else []
    except ShopifyError as e:
        return err(e)
    rep = L.sales_report(orders, terms, variants, c, include_cancelled=bool(b.get("include_cancelled")))
    return jsonify({"ok": True, "terms": terms, **rep})


# ------------------------------------------------------------------ helpers
def _loc(b):
    loc = (b.get("location_id") if hasattr(b, "get") else None) or cfg()["location_id"]
    if not loc:
        raise ValueError("Settings mein location select karein")
    return loc



# ------------------------------------------------------------------ scan
@app.post("/api/scan")
def scan():
    b = request.get_json(force=True)
    code = (b.get("code") or "").strip()
    if not code:
        return err("Code khali hai")
    try:
        loc = _loc(b)
        sh = shop()
        variants = sh.variants_by_barcode(code, loc)
        via = "barcode"
        if not variants:  # fallback: KHD-301-M jaisa SKU label
            vs = sh.variants_by_skus(L.candidate_skus(code), loc)
            known = {L.norm(v["sku"]) for v in vs}
            base, size = L.split_code(code, known)
            variants = [v for v in vs if L.norm(v["sku"]) == base and (L.norm(v["size"]) == L.norm(size))]
            via = "sku"
        if not variants:
            return jsonify({"ok": True, "found": False, "code": code})
        groups = L.group_variants(variants)
        if len(groups) > 1:
            return jsonify({"ok": True, "found": False, "code": code,
                            "error": "Ye barcode ek se zyada SKU/size par laga hai: " +
                                     ", ".join(f'{g["sku"]} {g["size"]}' for g in groups)})
        g = groups[0]
        delta = int(b.get("delta") or 0)
        applied = False
        if b.get("apply") and delta and g["inventory_item_ids"]:
            errs = sh.adjust_quantities(loc, [{"inventory_item_id": i, "delta": delta} for i in g["inventory_item_ids"]])
            if errs:
                return err("; ".join(errs))
            applied = True
            if g["available"] is not None:
                g["available"] += delta
    except (ShopifyError, ValueError) as e:
        return err(e)
    return jsonify({"ok": True, "found": True, "code": code, "via": via, "group": g, "applied": applied})


@app.post("/api/scan/apply")
def scan_apply():
    b = request.get_json(force=True)
    try:
        loc = _loc(b)
    except ValueError as e:
        return err(e)
    changes = []
    for it in b.get("items") or []:
        d = int(it.get("delta") or 0)
        for i in it.get("inventory_item_ids") or []:
            if str(i).startswith("gid://shopify/InventoryItem/") and d:
                changes.append({"inventory_item_id": i, "delta": d})
    if not changes:
        return jsonify({"ok": True, "updated": 0, "errors": []})
    try:
        errors = shop().adjust_quantities(loc, changes)
    except ShopifyError as e:
        return err(e)
    return jsonify({"ok": True, "updated": len(changes), "errors": errors})


# ------------------------------------------------------------------ barcodes
@app.post("/api/barcodes/preview")
def barcodes_preview():
    try:
        headers, data = _read_upload()
    except Exception as e:
        return err(f"File parh nahi saka: {e}")
    f = request.form
    sku_col, bc_col = int(f["sku_col"]), int(f["bc_col"])
    size_col = f.get("size_col")
    size_col = None if size_col in (None, "", "-1") else int(size_col)
    codes = L.gtech_codes(data, sku_col)
    if not codes:
        return err("SKU column mein koi SKU nahi mila")
    try:
        variants = shop().variants_by_skus(codes, f.get("location_id") or cfg()["location_id"] or "gid://shopify/Location/0")
    except ShopifyError as e:
        return err(e)
    rows, summary = L.build_barcode_preview(variants, data, sku_col, bc_col, size_col, replace=f.get("replace", "1") == "1")
    return jsonify({"ok": True, "rows": rows, "summary": summary})


@app.post("/api/barcodes/apply")
def barcodes_apply():
    items = []
    for it in request.get_json(force=True).get("items") or []:
        if str(it.get("variant_id", "")).startswith("gid://shopify/ProductVariant/") and \
           str(it.get("product_id", "")).startswith("gid://shopify/Product/"):
            bcs = [str(x).strip() for x in it.get("barcodes") or [] if str(x).strip()][:20]
            items.append({"variant_id": it["variant_id"], "product_id": it["product_id"], "barcodes": bcs})
    if not items:
        return err("Kuch update karne ko nahi")
    try:
        errors = shop().set_barcodes(items)
    except ShopifyError as e:
        msg = str(e)
        if "access" in msg.lower() or "403" in msg:
            msg += " — Shopify app mein write_products scope add kar ke naya version release karein."
        return err(msg)
    failed = {v for e in errors for v in e["variant_ids"]}
    return jsonify({"ok": True, "updated": len(items) - len(failed), "errors": errors})


# ------------------------------------------------------------------ UI
with open(os.path.join(BASE, "ui.html"), encoding="utf-8") as _f:
    UI_HTML = _f.read()


@app.get("/")
def index():
    return Response(UI_HTML, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=True)
