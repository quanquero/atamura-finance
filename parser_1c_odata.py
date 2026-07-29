# -*- coding: utf-8 -*-
"""
Парсер 1С OData -> SQLite. Пилот финблока (база 21_Silver_Development).
Тянет контрагентов (БИН) + исходящие оплаты, джойнит по БИН, ищет дубли.
Только стандартная библиотека Python 3 — ставить ничего не надо.

Настрой 3 строки ниже (BASE / USER / PASS) и запусти:
    python parser_1c_odata.py
"""
import base64, json, sqlite3, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

# ==================== НАСТРОЙКИ ====================
# Если запускаешь НА сервере 1С — оставь localhost.
# Если с другого компа в той же сети — поставь http://192.168.0.200/21_Silver_Development/odata/standard.odata
BASE = "http://localhost/21_Silver_Development/odata/standard.odata"
USER = ""          # 1С-пользователь (если OData попросит логин при заходе браузером — впиши). Пусто = без авторизации.
PASS = ""
MONTHS = 1         # за сколько последних месяцев тянуть оплаты
DB    = "finance_1c.sqlite3"
# ==================================================

PAY_DOCS = [   # документы исходящих платежей в этой конфигурации
    "Document_ПлатежноеПоручениеИсходящее",
    "Document_ПлатежныйОрдерСписаниеДенежныхСредств",
    "Document_РасходныйКассовыйОрдер",
]

def _req(url):
    r = urllib.request.Request(url)
    if USER:
        r.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    r.add_header("Accept", "application/json")
    with urllib.request.urlopen(r, timeout=90) as resp:
        return json.load(resp)

def fetch_all(entity, filter_=None):
    """Постранично тянет все записи сущности (по 1000)."""
    out, skip = [], 0
    while True:
        parts = ["$format=json", "$top=1000", f"$skip={skip}"]
        if filter_:
            parts.append("$filter=" + urllib.parse.quote(filter_, safe="'"))
        # имя сущности кириллицей (Catalog_Контрагенты) → URL-кодируем, иначе HTTP-строка падает на ASCII
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

def main():
    # 1) Контрагенты -> карта Ref_Key -> (БИН, имя, тип)
    print("Тяну контрагентов…")
    conts = fetch_all("Catalog_Контрагенты", "IsFolder eq false and DeletionMark eq false")
    cmap = {}
    for c in conts:
        cmap[c["Ref_Key"]] = {
            "bin":  (pick(c, ["ИдентификационныйКодЛичности", "РНН"]) or "").strip(),
            "name": c.get("Description", ""),
            "type": c.get("ЮрФизЛицо", ""),
        }
    print(f"  контрагентов: {len(cmap)}")

    # 2) Оплаты за последние MONTHS месяцев
    since = (datetime.now() - timedelta(days=30 * MONTHS)).strftime("%Y-%m-%dT00:00:00")
    payments = []
    for doc in PAY_DOCS:
        print(f"Тяну {doc}…")
        try:
            recs = fetch_all(doc, f"Date ge datetime'{since}' and DeletionMark eq false")
        except Exception as e:
            print(f"  пропуск ({e})"); continue
        print(f"  записей: {len(recs)}")
        if recs:
            print("  поля первой записи:", ", ".join(list(recs[0].keys())[:45]))
        for r in recs:
            cref   = pick(r, ["Контрагент_Key", "Получатель_Key", "Контрагент"])
            amount = pick(r, ["СуммаДокумента", "Сумма"])
            date   = pick(r, ["Date", "Дата"])
            if amount is None or cref is None:
                continue
            info = cmap.get(cref, {})
            payments.append({
                "doc": doc.replace("Document_", ""),
                "number": r.get("Number", ""),
                "date": str(date or "")[:10],
                "bin": info.get("bin", ""),
                "name": info.get("name", "") or "(нет в справочнике)",
                "amount": float(amount),
                "purpose": pick(r, ["НазначениеПлатежа", "Комментарий"]) or "",
                "posted": 1 if r.get("Posted") else 0,
            })
    total = sum(p["amount"] for p in payments)
    print(f"\nВсего оплат за {MONTHS} мес: {len(payments)} на сумму {money(total)} ₸")

    # 3) В SQLite
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS payment(
        doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT, amount REAL, purpose TEXT, posted INT)""")
    con.execute("DELETE FROM payment")
    con.executemany("INSERT INTO payment VALUES(?,?,?,?,?,?,?,?)",
        [(p["doc"], p["number"], p["date"], p["bin"], p["name"], p["amount"], p["purpose"], p["posted"]) for p in payments])
    con.commit(); con.close()

    # 4) Дубли: тот же БИН + сумма + дата (то, что Шерлок ловит)
    groups = defaultdict(list)
    for p in payments:
        if p["bin"]:
            groups[(p["bin"], round(p["amount"], 2), p["date"])].append(p)
    dubs = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n=== КАНДИДАТЫ В ДУБЛИ: {len(dubs)} ===")
    for (bin_, amt, date), items in sorted(dubs.items(), key=lambda x: -x[0][1]):
        nums = ", ".join(str(i["number"]) for i in items)
        print(f"  🔴 {items[0]['name']} · {money(amt)} ₸ · {date} · платежей {len(items)} (№ {nums})")

    # 5) Топ по контрагенту
    tot = defaultdict(float)
    for p in payments:
        tot[p["name"]] += p["amount"]
    print("\n=== ТОП-10 ПО ОПЛАТЕ ===")
    for name, s in sorted(tot.items(), key=lambda x: -x[1])[:10]:
        print(f"  {money(s):>16} ₸  {name}")

    print(f"\nГотово. Данные в {DB} (таблица payment).")

if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}. Если 401 — нужен логин/пароль 1С (впиши USER/PASS вверху).")
    except Exception as e:
        print(f"Ошибка: {e}")
