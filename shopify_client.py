"""Shopify Admin GraphQL client (Gtech <-> Shopify sync)."""
import time
import uuid
import requests

API_VERSION = "2026-10"


class ShopifyError(Exception):
    pass


def normalize_store(store):
    s = (store or "").strip().lower()
    s = s.replace("https://", "").replace("http://", "").strip("/")
    if "/" in s:
        s = s.split("/")[0]
    if s and not s.endswith(".myshopify.com"):
        s = s + ".myshopify.com"
    return s


class Shopify:
    def __init__(self, cfg):
        self.store = normalize_store(cfg.get("store", ""))
        self.access_token = (cfg.get("access_token") or "").strip()
        self.client_id = (cfg.get("client_id") or "").strip()
        self.client_secret = (cfg.get("client_secret") or "").strip()
        self.api_version = cfg.get("api_version") or API_VERSION
        self._token = None
        self._token_exp = 0
        if not self.store:
            raise ShopifyError("Vercel Environment Variable SHOPIFY_STORE set nahi hai.")
        if not self.access_token and not (self.client_id and self.client_secret):
            raise ShopifyError("Vercel mein SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (ya SHOPIFY_ACCESS_TOKEN) set karein.")

    # ---------- auth ----------
    def token(self):
        if self.access_token:
            return self.access_token
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        r = requests.post(
            f"https://{self.store}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise ShopifyError(f"Token nahi mila ({r.status_code}): {r.text[:300]}. "
                               "Client ID/Secret check karein aur app store par install hai ya nahi.")
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.time() + int(j.get("expires_in", 86399))
        return self._token

    # ---------- graphql ----------
    def gql(self, query, variables=None, retries=6):
        url = f"https://{self.store}/admin/api/{self.api_version}/graphql.json"
        for attempt in range(retries):
            r = requests.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": self.token(), "Content-Type": "application/json"},
                timeout=60,
            )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code == 401:
                raise ShopifyError("Shopify ne token reject kar diya (401). Token/credentials check karein.")
            if r.status_code == 403:
                raise ShopifyError("Permission nahi hai (403). App mein scopes check karein: "
                                   "read_products, read_inventory, write_inventory, read_orders, write_orders, read_locations.")
            if r.status_code != 200:
                raise ShopifyError(f"Shopify error {r.status_code}: {r.text[:300]}")
            j = r.json()
            errs = j.get("errors")
            if errs:
                if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
                    time.sleep(2 + attempt * 2)
                    continue
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errs))
            # gentle throttle handling
            cost = (j.get("extensions") or {}).get("cost") or {}
            st = cost.get("throttleStatus") or {}
            if st and st.get("currentlyAvailable", 1000) < 200:
                time.sleep(1.5)
            return j["data"]
        raise ShopifyError("Shopify bar bar busy hai, thori der baad dobara try karein.")

    # ---------- shop / locations ----------
    def shop_info(self):
        d = self.gql("{ shop { name myshopifyDomain currencyCode ianaTimezone } "
                     "locations(first: 50) { nodes { id name isActive } } }")
        return d["shop"], [l for l in d["locations"]["nodes"] if l["isActive"]]

    # ---------- variants ----------
    VARIANT_Q = """
    query($q: String!, $loc: ID!, $after: String) {
      productVariants(first: 100, after: $after, query: $q) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id sku title
          selectedOptions { name value }
          product { id title status }
          inventoryItem {
            id tracked
            inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { quantity } }
          }
        }
      }
    }"""

    @staticmethod
    def _parse_variant(n):
        sku = (n.get("sku") or "").strip()
        size = ""
        for o in n.get("selectedOptions") or []:
            if o["name"].strip().lower() in ("size", "sizes", "saiz"):
                size = o["value"].strip()
        if not size and n.get("title") and n["title"] != "Default Title":
            size = n["title"].strip()
        inv = n.get("inventoryItem") or {}
        lvl = inv.get("inventoryLevel")
        qty = None
        if lvl and lvl.get("quantities"):
            qty = lvl["quantities"][0]["quantity"]
        return {
            "variant_id": n["id"], "sku": sku, "size": size,
            "variant_title": n.get("title") or "",
            "product": n["product"]["title"], "product_status": n["product"]["status"],
            "inventory_item_id": inv.get("id"), "tracked": inv.get("tracked", False),
            "stocked": lvl is not None, "available": qty,
        }

    def variants_by_skus(self, skus, location_id):
        """Sirf woh variants jin ka SKU in mein se kisi se exact match ho (case-insensitive)."""
        wanted = {}
        for s in skus:
            s = (s or "").strip().replace('"', "")
            if s:
                wanted[s.upper()] = s
        keys = list(wanted.values())
        out, seen = [], set()
        for i in range(0, len(keys), 25):
            chunk = keys[i:i + 25]
            q = " OR ".join(f'sku:"{k}"' for k in chunk)
            after = None
            while True:
                d = self.gql(self.VARIANT_Q, {"q": q, "loc": location_id, "after": after})
                pv = d["productVariants"]
                for n in pv["nodes"]:
                    if n["id"] in seen:
                        continue
                    v = self._parse_variant(n)
                    if v["sku"].upper() in wanted:
                        seen.add(n["id"])
                        out.append(v)
                if not pv["pageInfo"]["hasNextPage"]:
                    break
                after = pv["pageInfo"]["endCursor"]
        return out

    # ---------- set inventory ----------
    def set_quantities(self, location_id, items):
        """items: list of dict(inventory_item_id, quantity, change_from). Returns list of errors."""
        m = """
        mutation($input: InventorySetQuantitiesInput!, $key: String!) {
          inventorySetQuantities(input: $input) @idempotent(key: $key) {
            inventoryAdjustmentGroup { id }
            userErrors { field message code }
          }
        }"""
        errors = []
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            inp = {
                "name": "available",
                "reason": "correction",
                "referenceDocumentUri": "gtech://inventory-sync",
                "quantities": [{
                    "inventoryItemId": it["inventory_item_id"],
                    "locationId": location_id,
                    "quantity": int(it["quantity"]),
                    "changeFromQuantity": it.get("change_from"),
                } for it in batch],
            }
            d = self.gql(m, {"input": inp, "key": str(uuid.uuid4())})
            for e in d["inventorySetQuantities"]["userErrors"]:
                idx = None
                f = e.get("field") or []
                for part in f:
                    if str(part).isdigit():
                        idx = int(part)
                item = batch[idx] if idx is not None and idx < len(batch) else None
                errors.append({"item": item, "message": e.get("message"), "code": e.get("code")})
        return errors

    # ---------- orders ----------
    # ---------- tags (export history Shopify mein hi) ----------
    def add_tag(self, order_ids, tag):
        return self._tag_batch("tagsAdd", order_ids, tag)

    def remove_tag(self, order_ids, tag):
        return self._tag_batch("tagsRemove", order_ids, tag)

    def _tag_batch(self, op, ids, tag):
        errors = []
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            parts, var_defs, variables = [], ["$tag: [String!]!"], {"tag": [tag]}
            for j, oid in enumerate(chunk):
                var_defs.append(f"$id{j}: ID!")
                variables[f"id{j}"] = oid
                parts.append(f"m{j}: {op}(id: $id{j}, tags: $tag) {{ userErrors {{ field message }} }}")
            q = f"mutation({', '.join(var_defs)}) {{ {' '.join(parts)} }}"
            d = self.gql(q, variables)
            for j in range(len(chunk)):
                for e in (d.get(f"m{j}") or {}).get("userErrors") or []:
                    errors.append(f"{chunk[j]}: {e['message']}")
        return errors

    def orders_by_tag(self, tag, limit=200):
        q = """query($q: String!, $after: String) { orders(first: 100, after: $after, query: $q,
               sortKey: CREATED_AT, reverse: true) { pageInfo { hasNextPage endCursor }
               nodes { id name createdAt } } }"""
        out, after = [], None
        while len(out) < limit:
            d = self.gql(q, {"q": f"tag:'{tag}'", "after": after})
            out.extend(d["orders"]["nodes"])
            if not d["orders"]["pageInfo"]["hasNextPage"]:
                break
            after = d["orders"]["pageInfo"]["endCursor"]
        return out[:limit]

    def order_ids_by_names(self, names):
        out = {}
        for n in names:
            n = n.strip().lstrip("#")
            if not n:
                continue
            d = self.gql("query($q: String!) { orders(first: 1, query: $q) { nodes { id name } } }",
                         {"q": f"name:#{n}"})
            for o in d["orders"]["nodes"]:
                if o["name"].lstrip("#") == n:
                    out[o["name"]] = o["id"]
        return out

    ORDER_FIELDS_FULL = """
      id name createdAt cancelledAt tags
      displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
      totalPriceSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      customer { displayName }
      shippingAddress { name phone city address1 address2 }
      lineItems(first: 100) { nodes {
        id sku name title variantTitle quantity currentQuantity
        originalUnitPriceSet { shopMoney { amount } }
        discountedTotalSet { shopMoney { amount } }
      } }
      refunds(first: 10) { refundLineItems(first: 50) { nodes { quantity lineItem { id } } } }"""

    ORDER_FIELDS_BASIC = """
      id name createdAt cancelledAt tags
      displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
      totalPriceSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      lineItems(first: 100) { nodes {
        id sku name title variantTitle quantity currentQuantity
        originalUnitPriceSet { shopMoney { amount } }
        discountedTotalSet { shopMoney { amount } }
      } }
      refunds(first: 10) { refundLineItems(first: 50) { nodes { quantity lineItem { id } } } }"""

    def orders(self, search):
        out, after, fields = [], None, self.ORDER_FIELDS_FULL
        while True:
            q = ("query($q: String!, $after: String) { orders(first: 50, after: $after, query: $q, "
                 "sortKey: CREATED_AT) { pageInfo { hasNextPage endCursor } nodes { " + fields + " } } }")
            try:
                d = self.gql(q, {"q": search, "after": after})
            except ShopifyError as e:
                # customer data access na ho to bina customer fields ke dobara
                if fields is self.ORDER_FIELDS_FULL and ("access" in str(e).lower() or "customer" in str(e).lower()):
                    fields = self.ORDER_FIELDS_BASIC
                    continue
                raise
            o = d["orders"]
            out.extend(o["nodes"])
            if not o["pageInfo"]["hasNextPage"]:
                break
            after = o["pageInfo"]["endCursor"]
        return out
