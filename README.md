# Gtech ⇄ Shopify Sync (Vercel version)

Ek web app jo Vercel par chalti hai. Aap isay kahin se bhi browser mein khol sakte hain, password ke saath.

1. **Inventory Sync (Gtech → Shopify):** Gtech ki stock report (CSV/Excel) upload karein. Jis SKU ka Shopify par exact match milega, uski inventory update ho jayegi. Koi prefix ki shart nahi.
2. **Orders Export (Shopify → Gtech):** Shopify orders ki CSV download karein aur Gtech mein sale import kar dein. Jo order export ho jaye us par Shopify mein `gtech-exported` tag lag jata hai, is liye wo dobara export nahi hota.
3. **Sale Report:** Kisi bhi article ka SKU search karein (jaise `KHD-301`) aur dekhein kis size ke kitne pieces bike, kitne return ya cancel hue, aur abhi Shopify par kitna stock hai.

---

## 1) Shopify app (API keys)
1. Shopify Admin → **Settings → Apps and sales channels → Develop apps** (ya **dev.shopify.com**) → **Create app** → naam `Gtech Sync`.
2. **Admin API scopes** select karein:
   - `read_products`, `read_inventory`, `write_inventory`, `read_locations`
   - `read_orders`, `write_orders` (export tag lagane ke liye)
   - (optional) `read_customers`: CSV mein customer ka naam aur phone ke liye
   - (optional) `read_all_orders`: 60 din se purane orders ke liye
3. Version release karein aur store par **Install** karein.
4. **Client ID** aur **Client secret** copy karein. (Purana `shpat_...` token ho to wo bhi chalega.)

## 2) GitHub par code daalein
1. GitHub par nayi **private** repo banayein, jaise `gtech-shopify-sync`.
2. Is folder ki saari files us repo mein upload karein (**Add file → Upload files** → saari files drag karein → **Commit**).

## 3) Vercel par deploy
1. https://vercel.com → **Add New… → Project** → apni GitHub repo **Import** karein.
2. Framework khud **Flask** detect ho jayega. Build settings mein kuch nahi badalna.
3. Deploy se pehle **Environment Variables** mein ye daalein:

| Name | Value | Zaroori? |
|---|---|---|
| `APP_PASSWORD` | App kholne ka password (mazboot rakhein) | ✅ |
| `SECRET_KEY` | Koi bhi lamba random text (jaise 40 aksharon ka) | ✅ |
| `SHOPIFY_STORE` | `aapkastore.myshopify.com` | ✅ |
| `SHOPIFY_CLIENT_ID` | Shopify app ka Client ID | ✅* |
| `SHOPIFY_CLIENT_SECRET` | Shopify app ka Client secret | ✅* |
| `SHOPIFY_ACCESS_TOKEN` | `shpat_...` (sirf agar Client ID/Secret nahi) | * |
| `SHOPIFY_LOCATION_ID` | Default location (khali chhor sakte hain, app mein select ho jati hai) | |
| `ORDER_SKU_FILTER` | Orders export mein default filter, jaise `KHD-` (khali = sab items) | |
| `ITEM_CODE_FORMAT` | Default `{sku}-{size}` → `KHD-301-M` | |
| `DATE_FORMAT` | Default `%d-%m-%Y` → 27-10-2026 | |
| `UTC_OFFSET_HOURS` | Default `5` (Pakistan) | |
| `EXPORT_TAG` | Default `gtech-exported` | |

\* Client ID + Secret **ya** Access Token, dono mein se ek.

4. **Deploy** dabayein. Jo link milega (jaise `gtech-shopify-sync.vercel.app`) usay kholein, password daalein, aur **Settings → Test Connection** karein.

> Environment Variable badalne ke baad **Deployments → Redeploy** karna zaroori hai.

---

## Rozana ka kaam

### Inventory Sync
1. Gtech se stock / closing balance report CSV ya Excel mein export karein.
2. **Inventory Sync** → file choose karein → columns check karein.
3. **Preview banao** dabayein. Abhi Shopify par kuch nahi badalta.
4. Check kar ke **Shopify par update karo** dabayein.

**Match kaise hota hai:** Gtech ka SKU Shopify ke SKU se exact milna chahiye (chhote bade aksharon ka farq nahi parta). Jo SKU Shopify par nahi milta, wo "Shopify par nahi mila" mein dikh jata hai aur skip hota hai.

**Size zaroori hai:** Shopify par ek SKU har size par laga hai (`KHD-301` → XS…XL). Is liye Gtech file mein har size ki qty alag honi chahiye: `KHD-301-M`, `KHD-301 M`, ya size ka alag column. Agar sirf `KHD-301` ki total qty ho to wo item skip hoga.

### Orders Export
- Upar **"Sirf ye SKUs / prefix"** mein `KHD-` likhein to sirf KHD wale items aayenge. Khali chhor dein to saare items aayenge (bina SKU wale items skip hote hain).
1. **Orders Export** → dates select karein → **Orders preview**.
2. **CSV download** dabayein. Orders par `gtech-exported` tag lag jata hai.
3. CSV Gtech mein sale import kar dein.
4. Agar import fail ho to **Export history** mein order number (jaise `#8780`) likh kar **Un-mark** karein.

CSV ke columns ke naam aur tarteeb **Settings → Orders CSV columns** mein Gtech ke format ke mutabiq set karein. Ye setting browser mein save hoti hai.

### Sale Report
1. **Sale Report** tab → SKU likhein (jaise `KHD-301`, ya comma laga kar kai SKUs).
2. Date range chunein, ya **Aaj / 7 din / 30 din / Is mahine / 90 din** dabayein.
3. **Sale dekho** dabayein. Har size ke liye dikhega: kitne pieces bike, kitne orders, returns, ship hona baqi, cancelled, amount, aur abhi ka stock.
4. Neeche har order ki list hai, aur dono ki CSV bhi nikal sakte hain.

> 60 din se purani sale dekhne ke liye Shopify app mein `read_all_orders` scope chahiye.

---

## Masail (Troubleshooting)
- **Login page par "APP_PASSWORD set nahi":** Vercel mein variable daal kar Redeploy karein.
- **401 / token error:** Client ID/Secret ghalat hai, ya app store par install nahi.
- **403 / permission:** Upar wale scopes add kar ke naya app version release karein.
- **"Qty badal gayi" error:** Preview ke baad naya order aa gaya. Dobara Preview → Update karein.
- **Timeout:** Bahut zyada orders ek saath hon to date range chhoti kar dein.

## Local chalana (optional)
```
pip install -r requirements.txt
set APP_PASSWORD=... & set SHOPIFY_STORE=... & set SHOPIFY_CLIENT_ID=... & set SHOPIFY_CLIENT_SECRET=...
python app.py
```
