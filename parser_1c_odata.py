# -*- coding: utf-8 -*-
"""
Парсер 1С OData -> SQLite. Финблок ATAMŪRA. МУЛЬТИ-БАЗА.
Проходит по ВСЕМ базам из bases.json, тянет контрагентов + оплаты + поступления,
складывает в ОДНО ядро (помечая компанией), ищет дубли, «три ноги», БДДС.
Только стандартная библиотека Python 3.

Настрой bases.json рядом со скриптом (см. bases.example.json), потом:
    python parser_1c_odata.py
"""
import base64, json, os, sqlite3, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "finance_1c.sqlite3")

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
RECEIPT_DOCS = ["Document_ПоступлениеТоваровУслуг"]  # выполнено / АВР

# текущая база (переустанавливается в цикле по базам)
BASE = USER = PASS = ""


def _load_config():
    """bases.json: {months, push_url, push_key, bases:[{company, base, user, pass}]}. Фолбэк — .env."""
    try:
        cfg = json.load(open(os.path.join(HERE, "bases.json"), encoding="utf-8-sig"))  # -sig: терпит BOM
        push = {"url": cfg.get("push_url", ""), "key": cfg.get("push_key", "")}
        return int(cfg.get("months", 1)), cfg.get("bases", []), push
    except Exception:
        env = {}
        try:
            for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1); env[k.strip()] = v.strip()
        except Exception:
            pass
        return int(env.get("MONTHS", "1") or "1"), [{
            "company": "(из .env)", "base": env.get("BASE", ""),
            "user": env.get("USER", ""), "pass": env.get("PASS", ""),
        }], {"url": env.get("PUSH_URL", ""), "key": env.get("PUSH_KEY", "")}


def _req(url):
    r = urllib.request.Request(url)
    # Basic-заголовок шлём ВСЕГДА (даже с пустыми user:pass): часть баз требует заголовок,
    # но впускает с пустым логином/паролем — а без заголовка отвечает 401.
    r.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    r.add_header("Accept", "application/json")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp)


def fetch_all(entity, filter_=None):
    out, skip = [], 0
    while True:
        parts = ["$format=json", "$top=1000", f"$skip={skip}"]
        if filter_:
            parts.append("$filter=" + urllib.parse.quote(filter_, safe="'"))
        url = BASE + "/" + urllib.parse.quote(entity) + "?" + "&".join(parts)  # имя кириллицей → URL-кодируем
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


def contractors():
    cmap = {}
    for c in fetch_all("Catalog_Контрагенты", "IsFolder eq false and DeletionMark eq false"):
        cmap[c["Ref_Key"]] = {"bin": (pick(c, ["ИдентификационныйКодЛичности", "РНН"]) or "").strip(),
                              "name": c.get("Description", "")}
    return cmap


def pull(docs, since, kind):
    rows = []
    for doc in docs:
        try:
            recs = fetch_all(doc, f"Date ge datetime'{since}' and DeletionMark eq false")
        except Exception as e:
            print(f"      {doc}: пропуск ({e})"); continue
        for r in recs:
            amount = pick(r, ["СуммаДокумента", "Сумма"])
            if amount is None:
                continue
            dog = ""
            for ln in (r.get("РасшифровкаПлатежа") or []):
                dk = ln.get("ДоговорКонтрагента_Key")
                if dk and dk != "00000000-0000-0000-0000-000000000000":
                    dog = dk; break
            rows.append({
                "doc": doc.replace("Document_", ""), "kind": kind,
                "number": r.get("Number", ""), "date": str(pick(r, ["Date", "Дата"]) or "")[:10],
                "cref": pick(r, ["Контрагент_Key", "Получатель_Key", "Плательщик_Key", "Контрагент"]),
                "amount": float(amount), "vidop": r.get("ВидОперации", "") or "",
                "purpose": r.get("НазначениеПлатежа", "") or "", "comment": r.get("Комментарий", "") or "",
                "dogovor_key": dog,
            })
    return rows


def main():
    global BASE, USER, PASS
    months, bases, push = _load_config()
    since = (datetime.now() - timedelta(days=30 * months)).strftime("%Y-%m-%dT00:00:00")
    out_all, in_all, rec_all = [], [], []
    ncontr = 0

    for b in bases:
        BASE, USER, PASS = b.get("base", ""), b.get("user", ""), b.get("pass", "")
        company = b.get("company") or BASE
        print(f"\n### БАЗА: {company} ###")
        if not BASE:
            print("  нет адреса — пропуск"); continue
        try:
            cmap = contractors()
        except Exception as e:
            print(f"  ⚠ ПРОПУСК базы ({e})"); continue
        print(f"  контрагентов: {len(cmap)}")
        ncontr += len(cmap)
        out = pull(OUT_DOCS, since, "out")
        inp = pull(IN_DOCS, since, "in")
        rec = pull(RECEIPT_DOCS, since, "receipt")
        print(f"  оплат исход: {len(out)} · вход: {len(inp)} · поступлений: {len(rec)}")
        for rows in (out, inp, rec):
            for p in rows:
                info = cmap.get(p["cref"], {})
                p["bin"] = info.get("bin", "")
                p["name"] = info.get("name", "") or "(нет в справочнике)"
                p["company"] = company
        for p in out:
            p["supplier"] = "поставщик" in (p["vidop"] or "").lower()
        out_all += out; in_all += inp; rec_all += rec

    sup = [p for p in out_all if p.get("supplier")]

    # ---- СВОДКА по холдингу ----
    print("\n" + "=" * 60)
    print(f"===== СВОДКА ПО ХОЛДИНГУ (за {months} мес, баз: {len(bases)}) =====")
    print(f"  Контрагентов (сумма по базам):   {ncontr}")
    print(f"  Исходящие (отток):               {money(sum(p['amount'] for p in out_all)):>16} ₸   поставщикам: {money(sum(p['amount'] for p in sup))} ({len(sup)} плат.)")
    print(f"  Входящие (приток):               {money(sum(p['amount'] for p in in_all)):>16} ₸")
    print(f"  Выполнено (поступление/АВР):     {money(sum(p['amount'] for p in rec_all)):>16} ₸")

    # разрез по компаниям
    byco = defaultdict(lambda: {"out": 0.0, "in": 0.0})
    for p in out_all: byco[p["company"]]["out"] += p["amount"]
    for p in in_all:  byco[p["company"]]["in"]  += p["amount"]
    print("  ---- по компаниям (отток / приток) ----")
    for co, d in sorted(byco.items(), key=lambda x: -x[1]["out"]):
        print(f"    {co[:30]:30} отток {money(d['out']):>15}   приток {money(d['in']):>15}")

    # ---- SQLite (одно ядро, помечено компанией) ----
    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS flow")   # пересоздаём под актуальную схему (снимок за период)
    con.execute("""CREATE TABLE flow(
        company TEXT, kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    rows = []
    for kind, lst in (("out", out_all), ("in", in_all), ("receipt", rec_all)):
        for p in lst:
            rows.append((p["company"], kind, p["doc"], p["number"], p["date"], p["bin"], p["name"],
                         p["amount"], p["vidop"], 1 if p.get("supplier") else 0, p["purpose"], p["comment"], p["dogovor_key"]))
    con.executemany("INSERT INTO flow VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()

    # ---- ДУБЛИ (в рамках одной компании: та же компания+БИН+сумма+дата) ----
    groups = defaultdict(list)
    for p in sup:
        if p["bin"]:
            groups[(p["company"], p["bin"], round(p["amount"], 2), p["date"])].append(p)
    dubs = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n===== КАНДИДАТЫ В ДУБЛИ (оплаты поставщикам): {len(dubs)} =====")
    for (co, bin_, amt, date), items in sorted(dubs.items(), key=lambda x: -x[0][2]):
        nums = ", ".join(str(i["number"]) for i in items)
        print(f"  🔴 [{co[:18]}] {items[0]['name'][:24]:24} {money(amt):>13} ₸ · {date} · платежей {len(items)} (№ {nums})")
    if not dubs:
        print("  чисто — задвоений среди оплат поставщикам не найдено")

    # ---- РАСХОЖДЕНИЯ (оплачено vs поступило) — сырой сигнал ----
    byc = defaultdict(lambda: {"done": 0.0, "paid": 0.0, "name": "", "co": ""})
    for p in sup:
        if p["bin"]:
            byc[(p["company"], p["bin"])]["paid"] += p["amount"]; byc[(p["company"], p["bin"])]["name"] = p["name"]; byc[(p["company"], p["bin"])]["co"] = p["company"]
    for p in rec_all:
        if p["bin"]:
            g = byc[(p["company"], p["bin"])]; g["done"] += p["amount"]
            if not g["name"]: g["name"] = p["name"]; g["co"] = p["company"]
    three = sorted(((abs(d["done"] - d["paid"]), d["co"], d["name"], d["done"], d["paid"], d["done"] - d["paid"])
                    for d in byc.values()), reverse=True)
    print("\n===== РАСХОЖДЕНИЯ: оплачено vs поступило (СЫРОЙ сигнал — вердикт только с договором) =====")
    for _, co, name, done, paid, diff in three[:12]:
        flag = "оплата>поступл (аванс?/переплата?)" if diff < -1 else ("поступл>оплата (акт раньше/недоплата?)" if diff > 1 else "≈ сходится")
        print(f"  [{co[:14]:14}] {name[:22]:22} поступ {money(done):>12} оплач {money(paid):>12} Δ {money(diff):>12} ⚠ {flag}")
    print("  (это НЕ переплата/долг по договору — норму без договора не знаем: аванс/бартер/удержания.)")

    # ---- ТОП поставщиков по оплате (холдинг) ----
    tot, nm = defaultdict(float), {}
    for p in sup:
        key = p["bin"] or p["name"]
        tot[key] += p["amount"]; nm[key] = p["name"]
    print("\n===== ТОП-10 ПОСТАВЩИКОВ ПО ОПЛАТЕ (холдинг) =====")
    for key, s in sorted(tot.items(), key=lambda x: -x[1])[:10]:
        print(f"  {money(s):>16} ₸  {nm[key]}")

    print(f"\nГотово. Ядро: {DB} (таблица flow — все базы вместе, поле company).")

    # ---- ОТПРАВКА среза на веб-ЛК (push), если задан push_url в bases.json ----
    if push.get("url"):
        payload = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "months": months,
            "flow": [{
                "company": p["company"], "kind": kind, "doc": p["doc"], "number": p["number"],
                "date": p["date"], "bin": p["bin"], "name": p["name"], "amount": p["amount"],
                "vidop": p["vidop"], "supplier": 1 if p.get("supplier") else 0,
                "purpose": p["purpose"], "comment": p["comment"], "dogovor_key": p["dogovor_key"],
            } for kind, lst in (("out", out_all), ("in", in_all), ("receipt", rec_all)) for p in lst],
        }
        try:
            req = urllib.request.Request(push["url"], data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Service-Key": push.get("key", "")})
            with urllib.request.urlopen(req, timeout=90) as r:
                print(f"✅ Отправлено на {push['url']}: {json.load(r)}")
        except Exception as e:
            print(f"⚠ Push не удался: {e}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}. 401 → проверь user/pass в bases.json.")
    except Exception as e:
        print(f"Ошибка: {e}")
