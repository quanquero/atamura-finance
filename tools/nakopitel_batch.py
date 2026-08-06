# -*- coding: utf-8 -*-
"""Батч-чтение договоров активных заявок → кеш накопителя (condition + Adata).
Активные = заявки, по которым есть оплаты 1С (сортируем по сумме — крупные первыми).
Идемпотентно: уже прочитанные пропускает (договор не меняется).

Запуск НА СЕРВЕРЕ (BITRIX_WEBHOOK + ANTHROPIC_API_KEY):
    python3 tools/nakopitel_batch.py [лимит]     # по умолчанию 10
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
import nakopitel as N
try:
    import adata as A
except Exception:
    A = None


def active_nums():
    """[(num, {amt, bin}), ...] по убыванию суммы оплат."""
    c = server._db()
    rows = c.execute("SELECT purpose,amount,bin FROM flow WHERE kind='out' AND supplier=1").fetchall()
    c.close()
    agg = {}
    for purpose, amt, b in rows:
        num = server._num_from(purpose)
        if not num:
            continue
        a = agg.setdefault(num, {"amt": 0.0, "bin": ""})
        a["amt"] += amt or 0
        if b:
            a["bin"] = b
    return sorted(agg.items(), key=lambda x: -x[1]["amt"])


def _read_set(table, col="num"):
    c = server._db()
    r = {row[0] for row in c.execute(f"SELECT {col} FROM {table}").fetchall()}
    c.close()
    return r


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    if not server.BITRIX:
        print("BITRIX_WEBHOOK не задан"); return
    nums = active_nums()
    # «готово» — только успешно прочитанные (есть № договора или сумма); ошибки повторяем
    c = server._db()
    done = {r[0] for r in c.execute("SELECT num FROM nakopitel WHERE contract_no!='' OR total>0").fetchall()}
    c.close()
    adone = _read_set("adata_cache", "bin")
    print(f"Активных заявок: {len(nums)} · уже прочитано: {len(done)} · читаю до {limit} новых", file=sys.stderr)
    processed = 0
    for num, meta in nums:
        if processed >= limit:
            break
        if num in done:
            continue
        print(f"[{processed+1}/{limit}] №{num} ({meta['amt']:,.0f} ₸) …", file=sys.stderr)
        res = N.analyze(num, bin_override=meta.get("bin", ""))
        terms = res.get("terms") or {}
        if terms.get("error"):
            print(f"   ⚠ договор: {terms['error']}", file=sys.stderr)
        else:
            print(f"   ✓ {terms.get('contract_no','')} · сумма {terms.get('total',0)} · удерж {terms.get('retention_sum',0)}", file=sys.stderr)
        server.store_nakopitel(num, res.get("bin", ""), terms, res.get("title", ""))
        b = res.get("bin", "")
        if A and b and b not in adone:
            try:
                server.store_adata(b, A.fetch(b))
                adone.add(b)
                print(f"   ✓ Adata {b}", file=sys.stderr)
            except Exception as e:
                print(f"   Adata {b}: {e}", file=sys.stderr)
        processed += 1
    print(f"Готово: прочитано новых {processed}, всего в накопителе {len(_read_set('nakopitel'))}, Adata {len(_read_set('adata_cache','bin'))}")


if __name__ == "__main__":
    main()
