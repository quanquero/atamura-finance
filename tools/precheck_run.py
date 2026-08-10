# -*- coding: utf-8 -*-
"""Проверка перед оплатой — прогон по заявкам в стадии «Оплата» воронки 178.

    python3 tools/precheck_run.py --dry [лимит]    # печать комментариев, БЕЗ записи в Bitrix (по умолч.)
    python3 tools/precheck_run.py --post [лимит]    # писать комментарии в Bitrix (идемпотентно по хэшу)

--dry по умолчанию НЕ читает недостающие договоры (быстро/бесплатно) — показывает формат на уже
прочитанных. Добавь --read, чтобы дочитать недостающие через Claude API.
"""
import sys, os, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S       # noqa: E402
import precheck as P     # noqa: E402


def main():
    args = sys.argv[1:]
    post = "--post" in args
    read = "--read" in args or post          # при постинге договор дочитываем всегда
    only_remarks = "--only-remarks" in args  # постить/печатать только заявки с замечаниями
    nums = [a for a in args if a.isdigit()]
    limit = int(nums[0]) if nums else 10
    if not S.BITRIX:
        print("BITRIX_WEBHOOK не задан"); return

    P._ensure_table()
    stages = {sid: "(закреплён)" for sid in P.PAY_STAGE_IDS} if P.PAY_STAGE_IDS else P.pay_stage_ids()
    if not stages:
        print("Стадии «Оплата» не нашёл. Проверь PAY_STAGE_NAMES (сейчас: %s)." % P.PAY_STAGE_NAMES)
        print("Подсказка: скорее всего название стадии другое — вывожу все стадии воронки ниже:")
        try:
            import bx_reader as R
            cats = R._bx("crm.category.list", {"entityTypeId": R.BX_ENTITY}).get("categories", [{"id": 0}])
            for cat in cats:
                st = R._bx("crm.status.list", {"filter": {"ENTITY_ID": "DYNAMIC_%d_STAGE_%s" % (R.BX_ENTITY, cat.get("id"))}})
                for s in (st or []):
                    print("   [%s] %s" % (s.get("STATUS_ID"), s.get("NAME")))
        except Exception as e:
            print("   не удалось получить список стадий:", e)
        return
    print("Стадии «Оплата»: %s" % ", ".join("%s=%s" % (k, v) for k, v in stages.items()))
    items = P.items_in_stages(list(stages.keys()))
    print("Заявок в этих стадиях: %d · обрабатываю до %d · режим: %s\n" % (
        len(items), limit, "ЗАПИСЬ В BITRIX" if post else "DRY-RUN (без записи)"))

    pays = P._pays()
    done = 0
    for it in items:
        if done >= limit:
            break
        if not it["num"]:
            continue
        v = P.verdict(it, pays, read=read)
        if only_remarks and not v["remarks"]:
            continue
        text = P.comment_text(v)
        h = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
        print("=" * 70)
        print(text)
        if post:
            ph, pcid = P._posted(v["num"])
            if ph == h:
                print("\n[ вердикт не изменился — пропуск ]")
            else:
                try:
                    if pcid:
                        P.update_comment(pcid, text); cid, act = pcid, "обновлён"
                    else:
                        cid, act = P.post_comment(it["id"], text), "запостен"
                    P._mark_posted(v["num"], h, cid)
                    print("\n[ ✓ комментарий %s (id=%s) ]" % (act, cid))
                except Exception as e:
                    print("\n[ ✕ ошибка постинга: %s ]" % str(e)[:200])
        print()
        done += 1
    print("Готово: обработано %d заявок." % done)


if __name__ == "__main__":
    main()
