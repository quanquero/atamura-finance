# -*- coding: utf-8 -*-
"""Проверка перед оплатой (Шерлок + Баффет) для заявок воронки 178 в стадии «Оплата».
Считает вердикт по одной заявке и формирует ОДИН понятный комментарий в карточку Bitrix.

  Шерлок (сведение): уже проходили оплаты по заявке; возможный дубль (тот же БИН+сумма в истории 1С);
                     сматчена ли заявка с договором/оплатами.
  Баффет (накопитель): договор/остаток/удержание/бартер/акты — из накопителя (читает договор через
                     Claude API, если ещё не прочитан). Замечания: превышение остатка, неудержанное
                     гар.удержание, неучтённый бартер, счёт без КС-актов.

Запись в Bitrix идемпотентна (по хэшу текста): повторно тот же комментарий не постим.
CLI — tools/precheck_run.py (--dry печатает, --post пишет)."""
import os
import re
import json
from datetime import datetime
import server as S
import bx_reader as R

# Справочник алиасов компаний: одна дочка под разными именами (заявки Bitrix vs 1С-база vs имя-поставщик).
# key = нормализованное имя (без ТОО/кавычек) → канон-ключ. Подтверждено: Orion=EuroStroy=Eurotest.
COMPANY_ALIASES = {
    "orionengineering": "eurotest", "eurostroycompany": "eurotest",
    "eurostroy": "eurotest", "еврострой": "eurotest",
}


def _canon(name):
    """Канон-ключ компании: убрать ТОО/ИП/кавычки/регистр + свести алиасы к одной дочке."""
    s = str(name or "").lower()
    for ch in '«»"“”':
        s = s.replace(ch, "")
    s = re.sub(r"\b(тоо|too|ип|ао|тд|оао|зао)\b", " ", s)
    key = re.sub(r"[^0-9a-zа-яё]", "", s)
    return COMPANY_ALIASES.get(key, key)

PAY_STAGE_NAMES = [n.strip() for n in os.environ.get("PAY_STAGE_NAMES", "Оплата").split(";") if n.strip()]
# точный список stageId (приоритет над резолвом по названию) — воронка фин.дира, где сидит «Оплата»
PAY_STAGE_IDS = [s.strip() for s in os.environ.get("PAY_STAGE_IDS", "").split(";") if s.strip()]
NDS_LIMIT_IP = int(os.environ.get("NDS_LIMIT_IP", "43250000"))   # порог НДС для ИП (Казахстан, ₸/год)


def _adata(bin_):
    c = S._db()
    r = c.execute("SELECT json FROM adata_cache WHERE bin=?", (str(bin_),)).fetchone()
    c.close()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except Exception:
        return {}


def _sb_flags(binf):
    """Красные флаги контрагента из модулей благонадёжности Adata (riskfactor/реестры/суды).
    Кэш в adata_sb по БИН — тянем один раз, дальше из кэша (Adata медленная/платная)."""
    if not binf:
        return []
    c = S._db()
    c.execute("CREATE TABLE IF NOT EXISTS adata_sb(bin TEXT PRIMARY KEY, json TEXT, ts TEXT)")
    r = c.execute("SELECT json FROM adata_sb WHERE bin=?", (str(binf),)).fetchone()
    c.close()
    if r and r[0]:
        try:
            card = json.loads(r[0])
        except Exception:
            card = {}
    else:
        try:
            import adata as A
            card = A.sb_card(binf)
        except Exception:
            return []
        c = S._db()
        c.execute("INSERT OR REPLACE INTO adata_sb VALUES(?,?,?)",
                  (str(binf), json.dumps(card, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M")))
        c.commit(); c.close()
    return [f for f in card.get("flags", []) if f.get("bad")]


def _year_paid(bin_, pays, year):
    """Сумма оплат 1С контрагенту (по БИН) за календарный год (по данным ядра)."""
    return sum((p[2] or 0) for p in pays if p[6] == bin_ and str(p[3] or "")[:4] == str(year))


def _days_ago(d):
    try:
        return " (%d дн назад)" % (datetime.now() - datetime.strptime(str(d)[:10], "%Y-%m-%d")).days
    except Exception:
        return ""


def _days_between(d1, d2):
    try:
        return abs((datetime.strptime(str(d2)[:10], "%Y-%m-%d") - datetime.strptime(str(d1)[:10], "%Y-%m-%d")).days)
    except Exception:
        return None


BOT_MARK = "Проверка перед оплатой"        # наши комментарии-вердикты — не путать с сотрудничьими


def _ensure_table():
    c = S._db()
    c.execute("CREATE TABLE IF NOT EXISTS precheck(num TEXT PRIMARY KEY, hash TEXT, comment_id TEXT, ts TEXT)")
    cols = {r[1] for r in c.execute("PRAGMA table_info(precheck)").fetchall()}
    if "comment_id" not in cols:                       # миграция со старой схемы (num,hash,ts)
        c.execute("ALTER TABLE precheck ADD COLUMN comment_id TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS bx_comment(
        cid TEXT PRIMARY KEY, num TEXT, item_id INTEGER, author TEXT, created TEXT, text TEXT)""")
    c.commit(); c.close()


def fetch_comments(item_id):
    """Комментарии таймлайна заявки (КРОМЕ наших вердиктов) → [{cid,author,created,text}].
    Тут сотрудники оставляют важный контекст (аванс согласован, оплата частями и т.п.)."""
    try:
        r = R._bx("crm.timeline.comment.list",
                  {"filter": {"ENTITY_ID": item_id, "ENTITY_TYPE": "DYNAMIC_%d" % R.BX_ENTITY},
                   "order": {"CREATED": "ASC"}})
    except Exception:
        return []
    rows = r if isinstance(r, list) else (r.get("comments") or r.get("result") or [])
    out = []
    for it in rows:
        txt = str(it.get("COMMENT") or "").strip()
        if not txt or BOT_MARK in txt:                 # свои вердикты пропускаем
            continue
        out.append({"cid": str(it.get("ID")), "author": str(it.get("AUTHOR_ID") or ""),
                    "created": str(it.get("CREATED") or "")[:19], "text": txt})
    return out


def store_comments(num, item_id, comments):
    """Сохранить сотрудничьи комментарии в ядро (аудит + показ в карточке ЛК)."""
    _ensure_table()
    c = S._db()
    for cm in comments:
        c.execute("INSERT OR REPLACE INTO bx_comment VALUES(?,?,?,?,?,?)",
                  (cm["cid"], str(num), item_id, cm["author"], cm["created"], cm["text"]))
    c.commit(); c.close()


def pay_stage_ids():
    """stageId стадий воронки 178 с названием из PAY_STAGE_NAMES (по всем категориям) → {stageId: name}."""
    try:
        cats = R._bx("crm.category.list", {"entityTypeId": R.BX_ENTITY}).get("categories", [{"id": 0}])
    except Exception:
        cats = [{"id": 0}]
    ids = {}
    for cat in cats:
        eid = "DYNAMIC_%d_STAGE_%s" % (R.BX_ENTITY, cat.get("id"))
        try:
            stages = R._bx("crm.status.list", {"filter": {"ENTITY_ID": eid}})
        except Exception:
            stages = []
        for st in (stages or []):
            nm = str(st.get("NAME", ""))
            if any(nm.lower() == n.lower() for n in PAY_STAGE_NAMES):
                ids[st.get("STATUS_ID")] = nm
    return ids


def items_in_stages(stage_ids):
    """Заявки в указанных стадиях → [{id,num,title,supplier,amount,stage}]."""
    out = []
    for sid in stage_ids:
        start = 0
        while True:
            r = R._bx("crm.item.list", {"entityTypeId": R.BX_ENTITY, "start": start,
                                        "filter": {"stageId": sid},
                                        "select": ["id", "title", "opportunity", "stageId",
                                                   "ufCrm4_1644310716", "ufCrm4_1762251054209"]})
            items = r.get("items", [])
            for it in items:
                out.append({"id": it.get("id"),
                            "num": str(it.get("ufCrm4_1644310716") or "").strip() or S._num_from(it.get("title", "")),
                            "title": it.get("title", ""),
                            "supplier": str(it.get("ufCrm4_1762251054209") or ""),
                            "amount": float(it.get("opportunity") or 0), "stage": it.get("stageId")})
            if len(items) < 50:
                break
            start += 50
    return out


def _pays():
    c = S._db()
    rows = c.execute("SELECT company,name,amount,date,purpose,doc,bin FROM flow WHERE kind='out' AND supplier=1").fetchall()
    c.close()
    return rows


def _nakopitel(num):
    c = S._db()
    r = c.execute("""SELECT bin,contract_no,total,avans_sum,retention_pct,retention_sum,
                     barter,barter_sum,account,notes,vypolneno FROM nakopitel WHERE num=?""", (str(num),)).fetchone()
    c.close()
    return r


def verdict(item, pays, read=True):
    """Вердикт Шерлок+Баффет по заявке. read=True — прочитать документы, если накопителя ещё нет."""
    num, supplier, amount = item["num"], item["supplier"], item["amount"]
    nk = _nakopitel(num)
    if not nk and read:
        try:
            S.read_zayavka_docs(num, read_contract=True)   # единый движок: договор+счёт+АВР по содержимому
        except Exception:
            pass
        nk = _nakopitel(num)
    buff, remarks, sher = {}, [], []
    # покрытие 1С: загружена ли база компании-плательщика (иначе «оплачено 0» — не факт)
    company = S._company_from(item.get("title", ""))
    loaded = {_canon(p[0]) for p in pays if p[0]}
    if company and _canon(company) not in loaded:
        remarks.append("⚠ 1С по компании «%s» не загружена в ядро — оплаты/остаток могут быть неполны" % company)
    binf = nk[0] if nk else ""
    # 2-я нога матча: платёж привязываем по № заявки И по № счёта из накопителя (ловим платежи без № заявки)
    acc_digits = re.sub(r"\D", "", str(nk[8] or "")) if nk else ""

    def _match_pay(p):
        if S._num_from(p[4]) == num:                     # основной ключ — № заявки
            return True
        if len(acc_digits) >= 6:                         # запасной — № счёта в назначении платежа
            pd = re.sub(r"\D", "", p[4] or "")
            if pd and (acc_digits in pd or pd in acc_digits):
                return True
        return False
    already = [p for p in pays if _match_pay(p)]
    fact = sum((p[2] or 0) for p in already)
    # --- Баффет ---
    if nk:
        _bin, cno, total, avans, rpct, rsum, barter, bsum, account, notes, vyp = nk
        vyp = vyp or 0
        # остаток к оплате = договор − удержание − бартер − уже оплачено (аванс уже входит в оплаты 1С, НЕ вычитаем повторно)
        ostatok = (total or 0) - (rsum or 0) - (bsum or 0) - fact
        buff = {"contract_no": cno, "total": total or 0, "fact": fact, "ostatok": ostatok,
                "retention_pct": rpct or 0, "retention_sum": rsum or 0, "barter": bool(barter),
                "vypolneno": vyp}
        if amount > ostatok + 1:
            remarks.append("Сумма к оплате (%s) превышает остаток по договору (%s)" % (S.money(amount), S.money(ostatok)))
        if (rpct or 0) > 0 and (rsum or 0) <= 0:
            remarks.append("Гарантийное удержание %d%% не отражено в сумме счёта" % int(rpct))
        if barter and (bsum or 0) <= 0:
            remarks.append("Указан бартер, но сумма бартера не учтена")
        low = (notes or "").lower()
        ctx = (str(item.get("title", "")) + " " + (notes or "")).lower()
        services = any(w in ctx for w in ("услуг", "обслуживан", "аренд", "кран", "экскаватор",
                                          "техник", "поставк", "товар", "материал"))
        is_works = (not services) and any(w in ctx for w in ("подряд", "работ", "смр", "отделк",
                                          "монолит", "кладк", "гидроизол", "устройств", "монтаж"))
        # ⭐ главная защита от переплаты: не платить больше, чем принято актами (АВР/КС-2/КС-3)
        if vyp > 0:
            if fact + amount > vyp + 1:
                remarks.append("🔴 Оплата превышает принятые работы (АВР): к оплате %s + уже оплачено %s = %s > выполнено %s" %
                               (S.money(amount), S.money(fact), S.money(fact + amount), S.money(vyp)))
        elif is_works and cno and "кс-2" not in low and "кс-3" not in low and "акт" not in low:
            remarks.append("🔴 Работы: актов выполнения (АВР/КС-2/КС-3) нет — оплата авансовая, не за принятые работы")
    else:
        remarks.append("Документы не прочитаны — включи «дочитывать документы», чтобы посчитать остаток/выполнение/условия")
    # --- Adata: контрагент-риски (модули благонадёжности) + лимит НДС для ИП ---
    ad = _adata(binf) if binf else {}
    bsc = ad.get("basic", {})
    for f in _sb_flags(binf):        # аресты/фиктив.сделки/розыск/налог.долг/суды — из riskfactor+реестры
        remarks.append("🔴 Контрагент: %s%s (Adata)" % (f["name"], (" · " + f["extra"]) if f.get("extra") else ""))
    is_ip = str(supplier).strip().lower().startswith("ип") or "индивид" in str(bsc.get("legal_form", "")).lower()
    if is_ip and not bsc.get("is_nds_payer") and binf:
        projected = _year_paid(binf, pays, datetime.now().year) + amount
        if projected > NDS_LIMIT_IP:
            remarks.append("ИП превышает годовой лимит НДС: оборот ~%s ₸ (с этой оплатой) > %s ₸ — обязан встать на НДС" % (S.money(projected), S.money(NDS_LIMIT_IP)))
        elif projected > NDS_LIMIT_IP * 0.85:
            remarks.append("ИП близок к лимиту НДС: ~%s из %s ₸/год" % (S.money(projected), S.money(NDS_LIMIT_IP)))
    # --- Шерлок: задвоение vs этапы (аванс+доплата) vs регулярная услуга ---
    from collections import Counter
    if already:
        dates = sorted(p[3] for p in already if p[3])
        # одинаковая сумма 2+ раз по одной заявке = вероятное задвоение (одно и то же оплачено дважды)
        amt_cnt = Counter(round(p[2] or 0) for p in already if (p[2] or 0) > 0)
        same = [(a, cnt) for a, cnt in amt_cnt.items() if cnt >= 2]
        if same:
            a, cnt = max(same)
            sher.append("🔴 Одинаковая сумма %s ₸ оплачена %d раз по заявке (%s) — вероятное ЗАДВОЕНИЕ" %
                        (S.money(a), cnt, ", ".join(dates)))
        elif len(already) >= 2:                          # разные суммы = скорее этапы, не красим
            sher.append("По заявке %d оплаты на %s ₸ (%s) — если этапы (аванс+доплата), ок; повтор той же суммы = задвоение" %
                        (len(already), S.money(fact), ", ".join(dates)))
        else:
            last = dates[-1] if dates else ""
            sher.append("Оплачено %s ₸ от %s%s. Если это ОНА — заявку в «Закрыто»; новая оплата = задвоение" %
                        (S.money(fact), last, _days_ago(last)))
    # запасная эвристика по БИН+сумма: дубль ТОЛЬКО в узком окне; растянутое = регулярная услуга (не красим)
    if binf:
        dups = [p for p in pays if p[6] == binf and abs((p[2] or 0) - amount) < 1 and p[3]]
        if len(dups) >= 2:
            dd = sorted(p[3] for p in dups)
            span = _days_between(dd[0], dd[-1])
            if span is not None and span <= 10:
                sher.append("🔴 Тот же БИН+сумма %d раз за %d дн (%s) — вероятный дубль" %
                            (len(dups), span, ", ".join(dd)))
            else:
                sher.append("Тот же БИН+сумма %d раз, растянуто на %s дн (%s) — похоже на регулярную услугу (аренда и т.п.), не дубль" %
                            (len(dups), span if span is not None else "?", ", ".join(dd)))
    if not already and not binf:
        sher.append("Заявка не сматчена ни с оплатами, ни с договором — проверить вручную")
    # сотрудничьи комментарии из карточки — читаем и храним (там бывает важный контекст)
    emp = fetch_comments(item["id"]) if item.get("id") else []
    if emp:
        store_comments(num, item["id"], emp)
    # уровень вердикта: красный (🔴 в замечаниях/Шерлоке) → жёлтый (любые замечания/находки) → зелёный
    red = any(str(x).startswith("🔴") for x in remarks + sher)
    level = "red" if red else ("warn" if (remarks or sher) else "ok")
    return {"id": item["id"], "num": num, "supplier": supplier, "amount": amount, "level": level,
            "buffett": buff, "sherlock": sher, "remarks": remarks, "comments": emp}


def comment_text(v):
    L = ["📋 Проверка перед оплатой · заявка №%s · %s" % (v["num"], v["supplier"] or "—"),
         "Сумма к оплате: %s ₸" % S.money(v["amount"]), "",
         "🔍 ШЕРЛОК (сведение)"]
    L += ["  • " + s for s in v["sherlock"]] if v["sherlock"] else ["  • дублей/расхождений не найдено"]
    L += ["", "💰 БАФФЕТ (накопитель)"]
    b = v["buffett"]
    if b:
        L.append("  • Договор %s: %s ₸ · Оплачено 1С: %s · Остаток: %s" % (
            b.get("contract_no") or "—", S.money(b.get("total") or 0), S.money(b.get("fact") or 0), S.money(b.get("ostatok") or 0)))
        L.append("  • Выполнено (АВР/КС): %s ₸%s" % (
            S.money(b.get("vypolneno") or 0),
            "  ⚠ актов нет" if not (b.get("vypolneno") or 0) else ""))
        L.append("  • Гар. удержание: %d%% (%s ₸) · Бартер: %s" % (
            int(b.get("retention_pct") or 0), S.money(b.get("retention_sum") or 0), "да" if b.get("barter") else "нет"))
    else:
        L.append("  • документы не прочитаны")
    L.append("")
    if v["remarks"]:
        L.append("⚠️ ЗАМЕЧАНИЯ (%d)" % len(v["remarks"]))
        L += ["  %d. %s" % (i, r) for i, r in enumerate(v["remarks"], 1)]
        L.append("")
    lvl = v.get("level", "warn" if v["remarks"] else "ok")
    L.append({"red":  "⛔ СТОП: красные флаги — не платить без разбора",
              "warn": "⚠️ Есть на что посмотреть перед оплатой",
              "ok":   "✅ Без замечаний — можно к оплате"}[lvl])
    if v.get("comments"):
        L += ["", "💬 В карточке %d заметок сотрудников — учтите при решении" % len(v["comments"])]
    return "\n".join(L)


def _posted(num):
    """(hash, comment_id) последнего запощенного вердикта по заявке, или (None, None)."""
    c = S._db()
    r = c.execute("SELECT hash, comment_id FROM precheck WHERE num=?", (str(num),)).fetchone()
    c.close()
    return (r[0], r[1]) if r else (None, None)


def _mark_posted(num, h, cid):
    c = S._db()
    c.execute("INSERT OR REPLACE INTO precheck(num,hash,comment_id,ts) VALUES(?,?,?,?)",
              (str(num), h, str(cid or ""), datetime.now().strftime("%Y-%m-%d %H:%M")))
    c.commit(); c.close()


def post_comment(item_id, text):
    """Добавить комментарий в таймлайн карточки smart-process (DYNAMIC_178). Возвращает id комментария."""
    return R._bx("crm.timeline.comment.add",
                 {"fields": {"ENTITY_ID": item_id, "ENTITY_TYPE": "DYNAMIC_%d" % R.BX_ENTITY, "COMMENT": text}})


def update_comment(comment_id, text):
    """Обновить существующий комментарий (чтобы не плодить дубли при повторной проверке)."""
    return R._bx("crm.timeline.comment.update", {"id": comment_id, "fields": {"COMMENT": text}})
