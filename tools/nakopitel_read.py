# -*- coding: utf-8 -*-
"""Накопитель по одной заявке: Bitrix (договор/КП) → claude -p (условия) → Adata (контрагент).
Запуск НА СЕРВЕРЕ (там есть BITRIX_WEBHOOK + claude -p):
    ADATA_TOKEN=... python tools/nakopitel_read.py 15871
Выводит JSON с условиями договора + карточкой контрагента + флагами.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bx_reader as R

try:
    import adata as A
except Exception:
    A = None


def main():
    if len(sys.argv) < 2:
        print("Укажи № заявки, напр.: python tools/nakopitel_read.py 15871"); sys.exit(1)
    num = sys.argv[1]
    if not R.BITRIX:
        print(json.dumps({"error": "BITRIX_WEBHOOK не задан в окружении финсервера"}, ensure_ascii=False)); return

    item = R.item_by_num(num)
    if not item:
        print(json.dumps({"error": f"заявка №{num} не найдена в Bitrix"}, ensure_ascii=False)); return
    title = item.get("title", "")
    print(f"# Заявка №{num} (id {item.get('id')}): {title[:90]}", file=sys.stderr)

    # вложения
    files = R.file_fields(item)
    print(f"# Вложений найдено: {len(files)}", file=sys.stderr)
    paths = []
    for field, url, name in files:
        try:
            p = R.download(url)
            paths.append(p)
            print(f"#   {field}: {name or ''} → {os.path.basename(p)}", file=sys.stderr)
        except Exception as e:
            print(f"#   {field}: ошибка скачивания {e}", file=sys.stderr)

    # чтение договора/КП
    terms = None
    if paths:
        print("# Читаю договор/КП через claude -p …", file=sys.stderr)
        terms = R.read_docs(paths, R.NAKOPITEL_INSTRUCTION, R.NAKOPITEL_SCHEMA)
    else:
        terms = {"error": "нет вложений в заявке"}

    # Adata по БИН из карточки (double-поле) — если есть
    bin_ = ""
    for k, v in item.items():
        s = str(v)
        m = __import__("re").search(r"\b(\d{12})\b", s)
        if m and k.lower().startswith("ufcrm") and len(s) < 20:
            bin_ = m.group(1); break
    adata_card = None
    if A and bin_:
        try:
            adata_card = A.fetch(bin_)
        except Exception as e:
            adata_card = {"error": str(e)}

    out = {"num": num, "id": item.get("id"), "title": title,
           "attachments": [os.path.basename(p) for p in paths],
           "terms": terms, "bin": bin_,
           "adata_short": (adata_card or {}).get("basic", {}).get("short_name") if adata_card else None}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
