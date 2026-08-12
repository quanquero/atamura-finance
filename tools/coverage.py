# -*- coding: utf-8 -*-
"""Сверка полноты: сколько заявок/оплат в Bitrix vs сколько у нас в базе.
Отвечает на «~10к успешных оплат — они у нас есть?».

    python3 tools/coverage.py
"""
import sys, os, json, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bx_reader as R, server as S


def _m(n):
    return ("{:,.0f}".format(n or 0)).replace(",", " ")


def bx_total(flt=None):
    body = {"entityTypeId": R.BX_ENTITY, "select": ["id"]}
    if flt:
        body["filter"] = flt
    req = urllib.request.Request(R.BITRIX + "/crm.item.list.json",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=R._CTX, timeout=40) as r:
        d = json.load(r)
    return d.get("total")


def bx_stage_breakdown():
    """Разбивка по воронкам/стадиям: категории воронки 178 → total, и SUCCESS по каждой."""
    try:
        cats = R._bx("crm.category.list", {"entityTypeId": R.BX_ENTITY}).get("categories", [])
    except Exception:
        cats = []
    out = []
    for c in cats:
        cid = c.get("id")
        try:
            tot = bx_total({"categoryId": cid})
            suc = bx_total({"categoryId": cid, "stageId": "DT%s_%s:SUCCESS" % (R.BX_ENTITY, cid)})
        except Exception:
            tot = suc = None
        out.append((cid, c.get("name", ""), tot, suc))
    return out


def main():
    print("=== BITRIX (воронка %s) ===" % R.BX_ENTITY)
    try:
        print("Всего заявок в 178:", bx_total())
    except Exception as e:
        print("Bitrix недоступен:", e); return
    for cid, name, tot, suc in bx_stage_breakdown():
        print("  воронка %s «%s»: всего %s · успешно(SUCCESS) %s" % (cid, name[:30], tot, suc))

    print("\n=== У НАС В БАЗЕ ===")
    c = S._db()
    zc = c.execute("SELECT COUNT(*) FROM zayavka").fetchone()[0]
    fc = c.execute("SELECT COUNT(*) FROM flow WHERE kind='out' AND supplier=1").fetchone()[0]
    fsum = c.execute("SELECT COALESCE(SUM(amount),0) FROM flow WHERE kind='out' AND supplier=1").fetchone()[0]
    comp = c.execute("SELECT COUNT(DISTINCT company) FROM flow").fetchone()[0]
    dmin, dmax = c.execute("SELECT MIN(date),MAX(date) FROM flow").fetchone()
    nk = c.execute("SELECT COUNT(*) FROM nakopitel WHERE article!='' OR total>0 OR vypolneno>0").fetchone()[0]
    # платежи, у которых № заявки распознан и есть в zayavka
    znums = {str(n or "").strip() or S._num_from(t or "")
             for n, t in c.execute("SELECT number,title FROM zayavka").fetchall()}
    znums.discard("")
    matched = miss = 0
    for (purpose,) in c.execute("SELECT purpose FROM flow WHERE kind='out' AND supplier=1").fetchall():
        n = S._num_from(purpose)
        if n and n in znums:
            matched += 1
        else:
            miss += 1
    c.close()
    print("Заявок синхронизировано (zayavka):", zc)
    print("Платежей 1С (out, поставщикам):  ", fc, "на сумму", _m(fsum), "тг")
    print("  из них с распознанным № заявки: ", matched, "· без матча:", miss)
    print("Компаний (дочек) в платежах:      ", comp)
    print("Период платежей 1С:               ", dmin, "→", dmax)
    print("Заявок прочитано ИИ (накопитель): ", nk)
    print("\nВывод: заявки Bitrix (все время) и платежи 1С (окно/часть баз) — разные множества.")
    print("Если 'успешно' в Bitrix сильно больше наших платежей — не догружены базы 1С или окно короче.")


if __name__ == "__main__":
    main()
