# -*- coding: utf-8 -*-
"""Анализ заявок в стадии «Оплата»: сверка данных Bitrix (заявка) vs 1С (платежи).
Категоризирует и показывает проблемные — дубли, переплаты, уже оплаченные (можно закрывать),
без №, без матча. Без записи, только чтение.

    python3 tools/oplata_audit.py
"""
import sys, os
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S
import precheck as P


def _m(n):
    return ("{:,.0f}".format(n or 0)).replace(",", " ")


def main():
    if not S.BITRIX:
        print("BITRIX_WEBHOOK не задан"); return
    stages = {sid: "(закреплён)" for sid in P.PAY_STAGE_IDS} if P.PAY_STAGE_IDS else P.pay_stage_ids()
    if not stages:
        print("Стадии «Оплата» не нашёл (PAY_STAGE_NAMES=%s)" % P.PAY_STAGE_NAMES); return
    items = P.items_in_stages(list(stages.keys()))
    pays = P._pays()
    loaded = {P._canon(p[0]) for p in pays if p[0]}

    cat = defaultdict(list)
    sum_amount = 0
    for it in items:
        num, amount = it["num"], it["amount"] or 0
        sum_amount += amount
        if not num:
            cat["no_num"].append((it, 0, 0)); continue
        already = [p for p in pays if S._num_from(p[4]) == num]
        fact = sum(p[2] or 0 for p in already)
        nk = P._nakopitel(num)
        binf = nk[0] if nk else ""
        # дубль по контрагенту (тот же БИН+сумма ≥2 раз)
        dup_bin = binf and len([p for p in pays if p[6] == binf and abs((p[2] or 0) - amount) < 1]) >= 2
        company = S._company_from(it.get("title", ""))
        not_loaded = company and P._canon(company) not in loaded
        if len(already) >= 2:
            cat["dup_pay"].append((it, fact, len(already)))
        elif fact > amount + 1:
            cat["overpay"].append((it, fact, len(already)))
        elif fact >= amount - 1 and fact > 0:
            cat["paid_full"].append((it, fact, len(already)))
        elif fact > 0:
            cat["partial"].append((it, fact, len(already)))
        elif dup_bin:
            cat["dup_bin"].append((it, fact, 0))
        elif not_loaded:
            cat["no_1c_base"].append((it, fact, 0))
        else:
            cat["awaiting"].append((it, fact, 0))

    print("=" * 64)
    print("ЗАЯВКИ В «ОПЛАТЕ»: %d · на сумму %s ₸" % (len(items), _m(sum_amount)))
    print("Стадии: %s" % ", ".join(stages.keys()))
    print("=" * 64)
    LBL = [
        ("dup_pay",    "🔴 ДУБЛЬ: уже 2+ оплаты в 1С по заявке — деньги могли уйти дважды"),
        ("overpay",    "🔴 ПЕРЕПЛАТА: в 1С оплачено БОЛЬШЕ суммы заявки"),
        ("dup_bin",    "🟠 ВОЗМОЖНЫЙ ДУБЛЬ: тот же БИН+сумма встречались ≥2 раз"),
        ("paid_full",  "🟡 УЖЕ ОПЛАЧЕНО ПОЛНОСТЬЮ — почему всё ещё в «Оплате»? (закрыть/дубль)"),
        ("partial",    "🔵 ЧАСТИЧНО ОПЛАЧЕНО — доплата (проверить остаток)"),
        ("no_1c_base", "⚪ ЖДЁТ ОПЛАТЫ, но база 1С компании НЕ загружена — «0» неточно"),
        ("awaiting",   "⚪ ЖДЁТ ПЕРВОЙ ОПЛАТЫ (в 1С пусто — норма для «Оплаты»)"),
        ("no_num",     "⚠ БЕЗ № заявки — не сматчить с 1С"),
    ]
    print("\nСВОДКА:")
    for key, label in LBL:
        rows = cat.get(key, [])
        if rows:
            s = sum((it["amount"] or 0) for it, _, _ in rows)
            print("  %-4d · %s  (%s ₸)" % (len(rows), label, _m(s)))

    # детально — проблемные категории
    for key, label in LBL:
        rows = cat.get(key, [])
        if not rows or key in ("awaiting",):
            continue
        print("\n" + "-" * 64 + "\n%s\n" % label)
        for it, fact, n in sorted(rows, key=lambda r: -(r[0]["amount"] or 0))[:25]:
            extra = ""
            if fact:
                extra = " · 1С: %s ₸%s" % (_m(fact), (" в %d платежах" % n) if n else "")
            print("  №%-7s %-26s заявка %s ₸%s" % (
                it["num"], (it["supplier"] or "—")[:26], _m(it["amount"]), extra))


if __name__ == "__main__":
    main()
