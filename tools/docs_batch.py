# -*- coding: utf-8 -*-
"""Массовое field-aware чтение документов заявок → накопитель (статья / условия / выполнено).
Читает НУЖНЫЙ документ под задачу: Тех.требование→статья, АВР→выполнено, Договор→условия.
Активные заявки = те, по которым есть оплаты 1С (крупные первыми). Идемпотентно (со статьёй — пропуск).

    python3 tools/docs_batch.py [лимит]             # дёшево: тех.требование→статья + АВР→выполнено
    python3 tools/docs_batch.py [лимит] --contract  # + читать договор (условия/удержание) — дороже

Запуск НА СЕРВЕРЕ (BITRIX_WEBHOOK + ANTHROPIC_API_KEY).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


def active_nums():
    """[(num, сумма_оплат), …] по убыванию суммы — приоритет чтения."""
    c = S._db()
    rows = c.execute("SELECT purpose,amount FROM flow WHERE kind='out' AND supplier=1").fetchall()
    c.close()
    agg = {}
    for purpose, amt in rows:
        n = S._num_from(purpose)
        if n:
            agg[n] = agg.get(n, 0.0) + (amt or 0)
    return sorted(agg.items(), key=lambda x: -x[1])


def _m(n):
    return ("{:,.0f}".format(n or 0)).replace(",", " ")


def main():
    limit = int(next((a for a in sys.argv[1:] if a.isdigit()), 10))
    read_contract = "--contract" in sys.argv
    if not S.BITRIX:
        print("BITRIX_WEBHOOK не задан"); return
    nums = active_nums()
    c = S._db()
    done = {r[0] for r in c.execute(
        "SELECT num FROM nakopitel WHERE article IS NOT NULL AND article!=''").fetchall()}
    c.close()
    print("Активных заявок: %d · уже со статьёй: %d · читаю до %d (%s)" % (
        len(nums), len(done), limit, "с договором" if read_contract else "тех.треб+АВР"), file=sys.stderr)
    processed = 0
    for num, amt in nums:
        if processed >= limit:
            break
        if num in done:
            continue
        print("[%d/%d] №%s (%s ₸) …" % (processed + 1, limit, num, _m(amt)), file=sys.stderr)
        try:
            res = S.read_zayavka_docs(num, read_contract=read_contract)
        except Exception as e:
            print("   ⚠ ошибка: %s" % str(e)[:160], file=sys.stderr); processed += 1; continue
        if res.get("error"):
            print("   ⚠ %s" % res["error"], file=sys.stderr)
        else:
            print("   ✓ статья: %s · договор %s · выполнено %s" % (
                res.get("article", "—") or "—", _m(res.get("total", 0)), _m(res.get("vypolneno", 0))), file=sys.stderr)
        processed += 1
    print("Готово: обработано %d заявок." % processed)


if __name__ == "__main__":
    main()
