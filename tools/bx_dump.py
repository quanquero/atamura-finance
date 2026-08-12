# -*- coding: utf-8 -*-
"""Дамп сырой карточки заявки Bitrix — увидеть ВСЕ поля и где реально лежат вложения (АВР и пр.).
Сравнивает crm.item.list (как мы читаем сейчас) и crm.item.get (часто отдаёт файлы полнее).

    python3 tools/bx_dump.py 15871
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bx_reader as R


def looks_file(v):
    def one(e):
        return isinstance(e, dict) and any(k in e for k in ("urlMachine", "url", "downloadUrl", "id"))
    if one(v):
        return True
    if isinstance(v, list) and v and one(v[0]):
        return True
    return False


def _short(v, n=220):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def dump_fields(item, labels, tag):
    print("\n=== %s: непустые поля (label · code · value) ===" % tag)
    fileish = []
    for k in sorted(item.keys()):
        v = item[k]
        if v in (None, "", [], {}, 0, "0"):
            continue
        lbl = labels.get(k, "")
        ff = looks_file(v)
        if ff:
            fileish.append((lbl, k, v))
        print("  [%s] %s = %s%s" % (lbl or "?", k, _short(v), "   <== ВЛОЖЕНИЕ" if ff else ""))
    print("\n--- %s: что распарсил наш file_fields() ---" % tag)
    got = R.file_fields(item)
    if got:
        for f, u, nm in got:
            print("  %s [%s] %s" % (f, labels.get(f, ""), nm or ""))
    else:
        print("  (ничего)")
    return fileish


def main(num):
    item = R.item_by_num(num)
    if not item:
        print("заявка не найдена"); return
    labels = R.field_labels()
    iid = item.get("id")
    print("Заявка #%s · id=%s" % (num, iid))
    print("Название:", item.get("title", ""))

    dump_fields(item, labels, "crm.item.list")

    # crm.item.get — обычно раскрывает файловые поля полнее
    try:
        got = R._bx("crm.item.get", {"entityTypeId": R.BX_ENTITY, "id": iid})
        gi = got.get("item", got) if isinstance(got, dict) else {}
        if gi and gi != item:
            dump_fields(gi, labels, "crm.item.get")
        else:
            print("\n(crm.item.get вернул то же самое)")
    except Exception as e:
        print("\ncrm.item.get ошибка:", e)

    print("\n=== слоты по назначению (по ярлыкам полей) ===")
    for purp in ("article", "contract", "avr", "invoice"):
        hit = []
        for field, url, name in R.file_fields(item):
            lbl = (labels.get(field, "") or "").lower()
            if any(k in lbl for k in R.DOC_PURPOSE.get(purp, ())):
                hit.append(labels.get(field, "") + (" / " + name if name else ""))
        print("  %-9s -> %s" % (purp, hit or "нет файла"))

    print("\n=== ВСЕ поля с ярлыком, где встречается 'авр'/'выполн'/'акт'/'кс' (ищем АВР) ===")
    for code, lbl in sorted(labels.items()):
        ll = (lbl or "").lower()
        if any(w in ll for w in ("авр", "выполн", "акт", "кс-2", "кс-3", "накладн")):
            val = item.get(code)
            print("  [%s] %s = %s" % (lbl, code, _short(val) if val not in (None, "", [], {}) else "— пусто в этой заявке"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "15871")
