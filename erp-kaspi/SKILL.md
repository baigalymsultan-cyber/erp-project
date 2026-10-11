---
name: kaspi
description: Live Kaspi product search, seller-level delivery verification, decision-ready comparison, and official app QR codes for Claude Code and Codex. Use when the user invokes /kaspi or $kaspi; says "найди на Каспи", "подбери на Kaspi", or "сравни товары"; asks what can arrive today, tomorrow, by express, or by a date; or wants a current price, availability check, shortlist, or best-value choice on Kaspi in Kazakhstan.
---

# Kaspi

Find current Kaspi listings and return a small, verified shortlist. Price, seller, stock, and delivery are live and location-sensitive. Reply in the user's language; use Russian when the request is in Russian.

## Default workflow

1. Extract the product job, hard requirements, budget, acceptable compromises, and delivery deadline. Avoid a questionnaire; ask only one genuinely blocking question.
2. Resolve city and zone from explicit context or the saved location profile. Default to Almaty only when context supports it. Never infer an exact address from IP and never claim city-level availability guarantees an address.
3. Run 2–4 query variants through `shortlist`. Add `--require-term`, `--require-material`, and `--exclude-term` for hard constraints. Do not put delivery words into the product query.
4. Let the CLI reject keyword spam, rank by token/model/category evidence, and group duplicate listings by canonical brand/category/model. A faster listing wins within the same model group.
5. Treat search-card delivery as preliminary. For finalists, the seller-offers API is authoritative for current seller, price, cost, and absolute delivery date. The absolute seller date overrides stale enums such as `TOMORROW`.
6. Verify decisive specs from the product page. Surface contradictions between description and characteristics; do not silently choose one.
7. Return 3–6 final rows, at most two meaningful later alternatives, one recommendation, delivery evidence, warnings, and the local checked timestamp.

## Delivery gates

| User intent | Main shortlist | CLI window |
|---|---|---|
| `сегодня`, `экспресс сегодня` | Same-day/express only | `today` |
| `именно завтра, не сегодня` | Tomorrow only | `tomorrow` |
| `на завтра`, `до завтра`, unspecified | Today or tomorrow | `fast` |
| `не срочно`, `можно подождать` | Any verified date | `any` |

Never silently relax the gate. Put missed-deadline options under `Можно подождать`. Missing delivery remains unverified.

## CLI

Resolve `scripts/kaspi.py` relative to the directory containing this `SKILL.md`. Do not assume the user's current working directory contains the script. The examples below use a relative path for readability; use the resolved absolute path when executing them outside the skill directory.

The decision-ready command is:

```bash
python3 scripts/kaspi.py shortlist \
  --query "лопатка кухонная деревянная" \
  --query "IKEA UTFORMA лопатка бук" \
  --require-term "лопатка|шпатель" \
  --require-material "дерево|деревян|бук|бамбук" \
  --exclude-term "силикон|пластик|пвх" \
  --delivery-window fast \
  --top 4 \
  --format markdown
```

Use `search` for broad JSON discovery without seller verification:

```bash
python3 scripts/kaspi.py search \
  --query "лазерный принтер Wi-Fi" \
  --query "Brother лазерный принтер Wi-Fi" \
  --delivery-window fast \
  --limit 20
```

Use `details` for known product URLs:

```bash
python3 scripts/kaspi.py details \
  --url "https://kaspi.kz/shop/p/example-123/?c=750000000"
```

Save reusable city-level defaults without an address:

```bash
python3 scripts/kaspi.py location set \
  --city-code 750000000 \
  --city-name "Алматы" \
  --zone Magnum_ZONE1 \
  --timezone Asia/Almaty
```

CLI flags override the saved profile. `--no-zone` broadens discovery, but delivery still needs seller verification. Traffic limits are six queries, twenty results per query, and six detail pages.

## Official QR contract

`shortlist` and `details` default to `--qr-mode official`. For each final product the CLI:

1. asks Kaspi for the official `https://l.kaspi.kz/...` app short-link;
2. opens the product with only the city-code cookie;
3. opens Kaspi's own `Сканируйте, чтобы перейти` modal;
4. captures its 160×160 QR canvas together with the Kaspi logo;
5. returns `qrLocalPath`, `qrMarkdown`, `qrTargetUrl`, `qrKind=kaspi_official_app`, and `qrEvidence=kaspi_product_modal`.

Embed `qrMarkdown` exactly as returned. It must be an absolute local PNG path. In a terminal that cannot render local images, keep the Markdown path and the clickable product link. Do not emit remote QR Markdown or relabel a generic QR as official. `--qr-mode fallback` is an explicit generic public-web-URL fallback for diagnostics only; `--qr-mode none` disables QR. If official capture fails, keep the clickable product link and report `qrError`.

The QR and location profile never contain an exact address, cookie, token, or session parameter. The official QR target contains the public product, city code, and Kaspi's `referrer=desktop_QR` only. `agent-browser` is required for official visual capture; search and seller checks otherwise use the Python standard library.

## Selection and evidence rules

- Delivery is a gate before price or rating.
- Prefer seller-offers price over search-card price and show delivery cost separately in the label.
- Use absolute local dates such as `23 июля до 14:00`, not only `завтра`.
- Ratings and review counts are confidence signals, not proof of quality.
- Check exact model/SKU. Similar titles can hide different products.
- A small higher price is worth recommending only when it buys a meaningful fit, quality, ownership, or delivery upgrade.
- For safety-critical or expensive purchases, verify critical claims on the manufacturer's official documentation too.
- If the undocumented Kaspi endpoint changes, inspect the current browser request; never fabricate live results.

## Response shape

Start with `Подходит по доставке`:

| Товар | Цена сейчас | Доставка | Ключевые параметры | Рейтинг | Почему / компромисс | QR |
|---|---:|---|---|---:|---|---|
| [Название](Kaspi URL) | ... | Express, 23 июля до 14:00, 995 ₸* | ... | ... | ... | official local QR |

Then include, when applicable:

- `Можно подождать`
- **Мой выбор:** one product and the deciding reason
- **Доставка:** city/zone, seller-offers evidence, and checkout caveat
- **Избегать:** a concrete conflict or risk only
- **Проверено:** absolute local timestamp

Never claim an order was placed when the QR or link only opens Kaspi.
