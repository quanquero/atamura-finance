# -*- coding: utf-8 -*-
"""Демонстрация чтения ОДНОЙ заявки end-to-end (доказать, что реально анализируется и ложится).

Показывает по шагам: какие документы прикреплены (по слотам-ярлыкам) → что ИИ извлёк из каждого →
что РЕАЛЬНО сохранилось в накопитель (перечитываем из базы) → куда это село в БДДС (объект → статья).

    python3 tools/read_one.py                 # список топ оплаченных заявок — выбрать №
    python3 tools/read_one.py 15871           # прочитать одну заявку целиком (все доки)
    python3 tools/read_one.py 15871 --cheap   # дёшево: тех.требование + счёт + АВР (без тяжёлого договора)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


def _m(n):
    return ("{:,.0f}".format(n or 0)).replace(",", " ")


def top_candidates(limit=20):
    c = S._db()
    z = {}
    for num, title in c.execute("SELECT number,title FROM zayavka").fetchall():
        n = str(num or "").strip() or S._num_from(title or "")
        if n:
            z[n] = title or ""
    agg = {}
    for amt, purpose in c.execute("SELECT amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall():
        n = S._num_from(purpose)
        if n and n in z:
            agg[n] = agg.get(n, 0) + (amt or 0)
    have = {str(r[0]) for r in c.execute(
        "SELECT num FROM nakopitel WHERE article!='' OR total>0 OR vypolneno>0").fetchall()}
    c.close()
    items = sorted(agg.items(), key=lambda kv: -kv[1])[:limit]
    print("Топ оплаченных заявок (по сумме 1С) — есть № в названии заявки:\n")
    print("  №          Оплачено 1С       Прочитано   Объект")
    print("  " + "-" * 58)
    for n, amt in items:
        mark = "✓ да" if n in have else "— нет"
        obj = S._object_from(z.get(n, "")) or "—"
        print("  %-9s %16s   %-9s  %s" % (n, _m(amt), mark, obj))
    print("\nЗапусти чтение одной:  python3 tools/read_one.py <№>")


def read_one(num, cheap=False):
    import bx_reader as R
    c = S._db()
    before = c.execute("SELECT article,total,vypolneno,contract_no FROM nakopitel WHERE num=?",
                       (str(num),)).fetchone()
    c.close()
    print("=" * 66)
    print("ЗАЯВКА #%s — чтение всех документов%s" % (num, " (дёшево, без договора)" if cheap else ""))
    print("=" * 66)
    item = R.item_by_num(num)
    if not item:
        print("x заявка не найдена в Bitrix")
        return
    print("Название:", (item.get("title", "") or "")[:92])
    docs = R.item_documents(item)
    if docs:
        print("\nВложения (ярлык поля -> файл -> ЧТО ЭТО по имени файла):")
        for d in docs:
            kind = R.classify_doc(d.get("label", ""), d.get("name", "")) or "?"
            print("   . [%s] %s  ==>  %s" % (d.get("label", "?"), (d.get("name", "?") or "")[:60], kind))
    else:
        print("\n! вложений в карточке не видно")
    print("\nЧитаю через Claude API (по СОДЕРЖИМОМУ, не по названию поля):")
    res = S.read_zayavka_docs(num, read_contract=not cheap,
                              progress=lambda st, m: print("   ->", m))
    if res.get("error"):
        print("\nx РЕЗУЛЬТАТ: в базу НЕ записано —", res["error"])
        if res.get("doc_kinds"):
            print("  ИИ увидел типы документов:", res["doc_kinds"])
        print("  (это не баг: пустую строку в накопитель мы намеренно не пишем)")
        return
    if res.get("doc_kinds"):
        print("\nИИ увидел в документах:", res["doc_kinds"])
    c = S._db()
    row = c.execute("""SELECT article,total,vypolneno,contract_no,avans_sum,retention_pct,
                       retention_sum,barter,object,account,bin,read_ts FROM nakopitel WHERE num=?""",
                    (str(num),)).fetchone()
    c.close()
    print("\n" + "-" * 66)
    print("СОХРАНЕНО В НАКОПИТЕЛЬ (перечитано из базы — вот что реально легло):")
    if not row:
        print("  !!! строки нет — что-то не записалось (сообщи мне вывод целиком)")
        return
    art, total, vyp, cno, av, rp, rs, bar, obj, acc, bn, ts = row
    canon = S._canon_article(art or "") or "—"
    cobj = S._object_from(obj or "") or S._object_from(item.get("title", "")) or "—"
    print("  Статья (ИИ):      %s   -> канон БДДС: %s" % (art or "—", canon))
    print("  Объект:           %s   -> канон БДДС: %s" % ((obj or "—")[:50], cobj))
    print("  Сумма (счёт/дог): %s тг" % _m(total))
    print("  Выполнено (АВР):  %s тг" % _m(vyp))
    print("  Договор №:        %s  | счёт: %s" % (cno or "—", acc or "—"))
    print("  Аванс: %s | удержание %s%% = %s | бартер: %s" % (_m(av), rp or 0, _m(rs), "да" if bar else "нет"))
    print("  БИН: %s | записано: %s" % (bn or "—", ts or "—"))
    print("-" * 66)
    if before:
        print("Было до чтения: статья=%r сумма=%s выполнено=%s" % (before[0] or "", _m(before[1]), _m(before[2])))
    else:
        print("Было до чтения: строки не существовало (новая).")
    print("\nВ БДДС это ложится: объект «%s» -> статья «%s»." % (cobj, canon))
    print("Проверь: вкладка БДДС -> клик по объекту «%s» -> окно-разбор (пончик/статьи)." % cobj)


if __name__ == "__main__":
    args = sys.argv[1:]
    nums = [a for a in args if a.isdigit()]
    if not nums:
        top_candidates()
    else:
        read_one(nums[0], cheap=("--cheap" in args))
