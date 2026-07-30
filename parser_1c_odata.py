# -*- coding: utf-8 -*-
"""
Парсер 1С OData -> SQLite. Финблок ATAMŪRA.
Полный срез: контрагенты(БИН) + исходящие/входящие оплаты + поступление(АВР) →
дедуп оплат поставщикам, «три ноги» (выполнено vs выплачено), БДДС (приток/отток).
Только стандартная библиотека Python 3.

Настрой .env РЯДОМ со скриптом (см. .env.example), потом:
    python parser_1c_odata.py
"""
import base64, json, os, sqlite3, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

# ==================== НАСТРОЙКИ (.env) ====================
# Секреты (BASE / USER / PASS) — в .env рядом со скриптом, в git НЕ коммитим.
def _load_env():
    cfg = {}
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg
_ENV = _load_env()
BASE   = _ENV.get("BASE", "http://localhost/21_Silver_Development/odata/standard.odata")
USER   = _ENV.get("USER", "")
PASS   = _ENV.get("PASS", "")
MONTHS = int(_ENV.get("MONTHS", "1") or "1")
DB     = "finance_1c.sqlite3"
# =========================================================

OUT_DOCS = [   # исходящие (отток)
    "Document_ПлатежноеПоручениеИсходящее",
    "Document_ПлатежныйОрдерСписаниеДенежныхСредств",
    "Document_РасходныйКассовыйОрдер",
]
IN_DOCS = [    # входящие (приток)
    "Document_ПлатежноеПоручениеВходящее",
    "Document_ПлатежныйОрдерПоступлениеДенежныхСредств",
    "Document_ПриходныйКассовыйОрдер",
    "Document_ОплатаОтПокупателяПлатежнойКартой",
]
RECEIPT_DOCS = [  # выполнено / АВР (принято работ и товаров)
    "Document_ПоступлениеТоваровУслуг",
]

def _req(url):
    r = urllib.request.Request(url)
    if USER:
        r.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    r.add_header("Accept", "application/json")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp)

def fetch_all(entity, filter_=None):
    """Постранично тянет все записи сущности (по 1000)."""
    out, skip = [], 0
    while True:
        parts = ["$format=json", "$top=1000", f"$skip={skip}"]
        if filter_:
            parts.append("$filter=" + urllib.parse.quote(filter_, safe="'"))
        # имя сущности кириллицей → URL-кодируем, иначе HTTP-строка падает на ASCII
        url = BASE + "/" + urllib.parse.quote(entity) + "?" + "&".join(parts)
        data = _req(url).get("value", [])
        out += data
        if len(data) < 1000:
            return out
        skip += 1000

def pick(rec, names):
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return v
    return None

def money(n):
    return f"{n:,.0f}".replace(",", " ")

def pull(docs, since, kind):
    """Тянет и нормализует документы одного денежного потока (out/in/receipt)."""
    rows = []
    for doc in docs:
        try:
            recs = fetch_all(doc, f"Date ge datetime'{since}' and DeletionMark eq false")
        except Exception as e:
            print(f"    {doc}: пропуск ({e})"); continue
        print(f"    {doc}: {len(recs)}")
        for r in recs:
            amount = pick(r, ["СуммаДокумента", "Сумма"])
            if amount is None:
                continue
            # структурная ссылка на договор (в табличной части РасшифровкаПлатежа) — надёжнее текста
            dog = ""
            for ln in (r.get("РасшифровкаПлатежа") or []):
                dk = ln.get("ДоговорКонтрагента_Key")
                if dk and dk != "00000000-0000-0000-0000-000000000000":
                    dog = dk; break
            rows.append({
                "doc": doc.replace("Document_", ""),
                "kind": kind,
                "number": r.get("Number", ""),
                "date": str(pick(r, ["Date", "Дата"]) or "")[:10],
                "cref": pick(r, ["Контрагент_Key", "Получатель_Key", "Плательщик_Key", "Контрагент"]),
                "amount": float(amount),
                "vidop": r.get("ВидОперации", "") or "",
                "purpose": r.get("НазначениеПлатежа", "") or "",   # НАЗНАЧЕНИЕ платежа — тут суть/№ договора/счёт
                "comment": r.get("Комментарий", "") or "",         # + комментарий (иногда № договора именно тут)
                "dogovor_key": dog,                                # структурная ссылка на договор (для Накопителя)
            })
    return rows

def main():
    print("Тяну контрагентов…")
    conts = fetch_all("Catalog_Контрагенты", "IsFolder eq false and DeletionMark eq false")
    cmap = {}
    for c in conts:
        cmap[c["Ref_Key"]] = {
            "bin":  (pick(c, ["ИдентификационныйКодЛичности", "РНН"]) or "").strip(),
            "name": c.get("Description", ""),
        }
    print(f"  контрагентов: {len(cmap)}")

    since = (datetime.now() - timedelta(days=30 * MONTHS)).strftime("%Y-%m-%dT00:00:00")
    print("Тяну ИСХОДЯЩИЕ (отток)…");           out_pay  = pull(OUT_DOCS, since, "out")
    print("Тяну ВХОДЯЩИЕ (приток)…");           in_pay   = pull(IN_DOCS, since, "in")
    print("Тяну ПОСТУПЛЕНИЕ (выполнено/АВР)…"); receipts = pull(RECEIPT_DOCS, since, "receipt")

    # обогащаем именем/БИН из справочника + тег «поставщику» на исходящих
    for rows in (out_pay, in_pay, receipts):
        for p in rows:
            info = cmap.get(p["cref"], {})
            p["bin"]  = info.get("bin", "")
            p["name"] = info.get("name", "") or "(нет в справочнике)"
    for p in out_pay:
        p["supplier"] = "поставщик" in (p["vidop"] or "").lower()

    out_total  = sum(p["amount"] for p in out_pay)
    sup        = [p for p in out_pay if p["supplier"]]
    sup_total  = sum(p["amount"] for p in sup)
    in_total   = sum(p["amount"] for p in in_pay)
    done_total = sum(p["amount"] for p in receipts)

    # ---- СВОДКА (БДДС) ----
    print(f"\n===== СВОДКА (за {MONTHS} мес) =====")
    print(f"  Контрагентов (БИН):            {len(cmap)}")
    print(f"  Исходящие (отток):             {money(out_total):>16} ₸   из них поставщикам: {money(sup_total)} ({len(sup)} плат.)")
    print(f"  Входящие (приток):             {money(in_total):>16} ₸")
    print(f"  Выполнено (поступление/АВР):   {money(done_total):>16} ₸")
    print(f"  Операц. сальдо (приток−отток): {money(in_total - out_total):>16} ₸")

    # диагностика: виды операций в исходящих (чтобы точно настроить фильтр «поставщику»)
    vk = defaultdict(float)
    for p in out_pay:
        vk[p["vidop"] or "(пусто)"] += p["amount"]
    print("  ---- виды операций в исходящих (топ) ----")
    for v, s in sorted(vk.items(), key=lambda x: -x[1])[:8]:
        print(f"    {money(s):>14} ₸  {v}")

    # ---- SQLite ----
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS flow(
        kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    con.execute("DELETE FROM flow")
    rows = []
    for kind, lst in (("out", out_pay), ("in", in_pay), ("receipt", receipts)):
        for p in lst:
            rows.append((kind, p["doc"], p["number"], p["date"], p["bin"], p["name"], p["amount"],
                         p["vidop"], 1 if p.get("supplier") else 0, p["purpose"], p["comment"], p["dogovor_key"]))
    con.executemany("INSERT INTO flow VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()

    # ---- ДУБЛИ по оплатам ПОСТАВЩИКАМ (Шерлок) ----
    groups = defaultdict(list)
    for p in sup:
        if p["bin"]:
            groups[(p["bin"], round(p["amount"], 2), p["date"])].append(p)
    dubs = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n===== КАНДИДАТЫ В ДУБЛИ (оплаты поставщикам): {len(dubs)} =====")
    for (bin_, amt, date), items in sorted(dubs.items(), key=lambda x: -x[0][1]):
        nums = ", ".join(str(i["number"]) for i in items)
        print(f"  🔴 {items[0]['name'][:28]:28} {money(amt):>13} ₸ · {date} · платежей {len(items)} (№ {nums})")
    if not dubs:
        print("  чисто — задвоений среди оплат поставщикам не найдено")

    # ---- ТРИ НОГИ: выполнено (АВР) vs выплачено — Накопитель без договора ----
    byc = defaultdict(lambda: {"done": 0.0, "paid": 0.0, "name": ""})
    for p in sup:
        if p["bin"]:
            byc[p["bin"]]["paid"] += p["amount"]; byc[p["bin"]]["name"] = p["name"]
    for p in receipts:
        if p["bin"]:
            byc[p["bin"]]["done"] += p["amount"]
            if not byc[p["bin"]]["name"]:
                byc[p["bin"]]["name"] = p["name"]
    three = sorted(((abs(d["done"] - d["paid"]), d["name"], d["done"], d["paid"], d["done"] - d["paid"])
                    for d in byc.values()), reverse=True)
    print("\n===== РАСХОЖДЕНИЯ: оплачено vs поступило (СЫРОЙ сигнал — вердикт только с договором) =====")
    for _, name, done, paid, diff in three[:12]:
        flag = "оплата > поступления (аванс? переплата?)" if diff < -1 else ("поступление > оплаты (акт раньше / недоплата?)" if diff > 1 else "≈ сходится")
        print(f"  {name[:26]:26} поступ {money(done):>13}  оплач {money(paid):>13}  Δ {money(diff):>13}  ⚠ {flag}")
    print("  (это НЕ переплата/долг по договору — без договора норму не знаем: аванс/бартер/удержания. Флаг = «проверить».)")

    # ---- ТОП поставщиков по оплате ----
    tot, nm = defaultdict(float), {}
    for p in sup:
        key = p["bin"] or p["name"]
        tot[key] += p["amount"]; nm[key] = p["name"]
    print("\n===== ТОП-10 ПОСТАВЩИКОВ (по оплате) =====")
    for key, s in sorted(tot.items(), key=lambda x: -x[1])[:10]:
        print(f"  {money(s):>16} ₸  {nm[key]}")

    print(f"\nГотово. Данные в {DB} (таблица flow: out / in / receipt).")

if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}. Если 401 — проверь USER/PASS в .env.")
    except Exception as e:
        print(f"Ошибка: {e}")
