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
import json
from datetime import datetime
import server as S
import bx_reader as R

PAY_STAGE_NAMES = [n.strip() for n in os.environ.get("PAY_STAGE_NAMES", "Оплата").split(";") if n.strip()]
NDS_LIMIT_IP = int(os.environ.get("NDS_LIMIT_IP", "43250000"))   # порог НДС для ИП (Казахстан, ₸/год)


def _adata(bin_):
    c = S._db()
    r = c.execute("SELECT json FROM adata_cache WHERE bin=?", (str(bin_),)).fetchone()
    c.close()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except Exception:
        return {}


def _year_paid(bin_, pays, year):
    """Сумма оплат 1С контрагенту (по БИН) за календарный год (по данным ядра)."""
    return sum((p[2] or 0) for p in pays if p[6] == bin_ and str(p[3] or "")[:4] == str(year))


def _ensure_table():
    c = S._db()
    c.execute("CREATE TABLE IF NOT EXISTS precheck(num TEXT PRIMARY KEY, hash TEXT, ts TEXT)")
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
                     barter,barter_sum,account,notes FROM nakopitel WHERE num=?""", (str(num),)).fetchone()
    c.close()
    return r


def verdict(item, pays, read=True):
    """Вердикт Шерлок+Баффет по заявке. read=True — прочитать договор, если накопителя ещё нет."""
    num, supplier, amount = item["num"], item["supplier"], item["amount"]
    nk = _nakopitel(num)
    if not nk and read:
        S.read_nakopitel(num)          # прочитать договор (Claude API) + сохранить
        nk = _nakopitel(num)
    buff, remarks, sher = {}, [], []
    binf = nk[0] if nk else ""
    fact = sum((p[2] or 0) for p in pays if S._num_from(p[4]) == num)
    # --- Баффет ---
    if nk:
        _bin, cno, total, avans, rpct, rsum, barter, bsum, account, notes = nk
        ostatok = (total or 0) - (avans or 0) - (rsum or 0) - (bsum or 0) - fact
        buff = {"contract_no": cno, "total": total or 0, "fact": fact, "ostatok": ostatok,
                "retention_pct": rpct or 0, "retention_sum": rsum or 0, "barter": bool(barter)}
        if amount > ostatok + 1:
            remarks.append("Сумма к оплате (%s) превышает остаток по договору (%s)" % (S.money(amount), S.money(ostatok)))
        if (rpct or 0) > 0 and (rsum or 0) <= 0:
            remarks.append("Гарантийное удержание %d%% не отражено в сумме счёта" % int(rpct))
        if barter and (bsum or 0) <= 0:
            remarks.append("Указан бартер, но сумма бартера не учтена")
        low = (notes or "").lower()
        ctx = (str(item.get("title", "")) + " " + (notes or "")).lower()
        is_works = any(w in ctx for w in ("подряд", "работ", "смр", "отделк", "монолит",
                                          "кладк", "гидроизол", "устройств", "монтаж"))
        if is_works and "кс-2" not in low and "кс-3" not in low and "акт" not in low:
            remarks.append("Счёт без подтверждающих актов (КС-2/КС-3)")
    else:
        remarks.append("Договор не прочитан — накопитель не построен")
    # --- Adata: аресты счетов, налоговый долг, лимит НДС для ИП ---
    ad = _adata(binf) if binf else {}
    rf = ad.get("riskFactor", {}).get("company", {})
    stt = ad.get("status", {})
    bsc = ad.get("basic", {})
    if rf.get("seized_bank_account") or rf.get("seized_property"):
        remarks.append("🔴 Аресты счетов/имущества у контрагента (Adata)")
    if stt.get("tax_debt"):
        remarks.append("Налоговая задолженность контрагента %s ₸ (Adata)" % S.money(stt.get("tax_debt")))
    is_ip = str(supplier).strip().lower().startswith("ип") or "индивид" in str(bsc.get("legal_form", "")).lower()
    if is_ip and not bsc.get("is_nds_payer") and binf:
        projected = _year_paid(binf, pays, datetime.now().year) + amount
        if projected > NDS_LIMIT_IP:
            remarks.append("ИП превышает годовой лимит НДС: оборот ~%s ₸ (с этой оплатой) > %s ₸ — обязан встать на НДС" % (S.money(projected), S.money(NDS_LIMIT_IP)))
        elif projected > NDS_LIMIT_IP * 0.85:
            remarks.append("ИП близок к лимиту НДС: ~%s из %s ₸/год" % (S.money(projected), S.money(NDS_LIMIT_IP)))
    # --- Шерлок ---
    already = [p for p in pays if S._num_from(p[4]) == num]
    if already:
        sher.append("По заявке уже проходили оплаты 1С: %d шт на %s ₸" % (len(already), S.money(sum(p[2] or 0 for p in already))))
    if binf:
        dups = [p for p in pays if p[6] == binf and abs((p[2] or 0) - amount) < 1]
        if dups:
            sher.append("Возможный дубль: тот же БИН и сумма уже встречались в 1С (%d)" % len(dups))
    if not already and not binf:
        sher.append("Заявка не сматчена с оплатами/договором — проверить вручную")
    return {"id": item["id"], "num": num, "supplier": supplier, "amount": amount,
            "buffett": buff, "sherlock": sher, "remarks": remarks}


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
        L.append("  • Гар. удержание: %d%% (%s ₸) · Бартер: %s" % (
            int(b.get("retention_pct") or 0), S.money(b.get("retention_sum") or 0), "да" if b.get("barter") else "нет"))
    else:
        L.append("  • договор не прочитан")
    L.append("")
    if v["remarks"]:
        L.append("⚠️ ЗАМЕЧАНИЯ (%d)" % len(v["remarks"]))
        L += ["  %d. %s" % (i, r) for i, r in enumerate(v["remarks"], 1)]
    else:
        L.append("✅ Без замечаний — можно к оплате")
    return "\n".join(L)


def _already_posted(num, h):
    c = S._db()
    r = c.execute("SELECT hash FROM precheck WHERE num=?", (str(num),)).fetchone()
    c.close()
    return bool(r and r[0] == h)


def _mark_posted(num, h):
    from datetime import datetime
    c = S._db()
    c.execute("INSERT OR REPLACE INTO precheck VALUES(?,?,?)",
              (str(num), h, datetime.now().strftime("%Y-%m-%d %H:%M")))
    c.commit(); c.close()


def post_comment(item_id, text):
    """Комментарий в таймлайн карточки smart-process (DYNAMIC_178)."""
    return R._bx("crm.timeline.comment.add",
                 {"fields": {"ENTITY_ID": item_id, "ENTITY_TYPE": "DYNAMIC_%d" % R.BX_ENTITY, "COMMENT": text}})
