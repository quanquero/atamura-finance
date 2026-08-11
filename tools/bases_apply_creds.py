# -*- coding: utf-8 -*-
"""Впарить креды в bases.json без ручной правки JSON и без BOM.

    python bases_apply_creds.py <путь\bases.json> <путь\creds.txt>
    # пример: python bases_apply_creds.py C:\atamura-1c\bases.json creds.txt

creds.txt — по строке на базу, поля через | :
    Компания | user | pass | base_url(необяз.)
Примеры:
    Med One | Администратор |  |
    Atamura Constraction | svd | @!0204!@ |
    Atree |  |  |                                  # аноним (пустой логин/пароль)
    Origo | Администратор |  | http://localhost/Origo/odata/standard.odata

Логика: матч по имени компании (нормализованный — без ТОО/ИП/кавычек/регистра).
- нашли запись → обновляем user/pass (и base, если он задан в строке);
- не нашли → добавляем НОВУЮ запись; base берём из строки, иначе ставим плейсхолдер
  http://localhost/<company>/odata/standard.odata (его надо будет поправить на реальную публикацию).
Сохраняет bases.json как UTF-8 БЕЗ BOM. creds.txt в git НЕ коммитить (пароли)."""
import json, sys, re, io


def norm(s):
    s = str(s or "").lower()
    for ch in '«»"“”':
        s = s.replace(ch, "")
    s = re.sub(r"\b(тоо|too|ип|ао|оао|зао)\b", " ", s)
    return re.sub(r"[^0-9a-zа-яё]", "", s)


def main():
    if len(sys.argv) < 3:
        print("Использование: python bases_apply_creds.py <bases.json> <creds.txt>")
        return
    bpath, cpath = sys.argv[1], sys.argv[2]
    cfg = json.load(open(bpath, encoding="utf-8-sig"))
    bases = cfg.setdefault("bases", [])
    idx = {norm(b.get("company")): b for b in bases}
    upd, add, placeholders = [], [], []
    for line in open(cpath, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        company = parts[0]
        user = parts[1] if len(parts) > 1 else ""
        pw = parts[2] if len(parts) > 2 else ""
        base = parts[3] if len(parts) > 3 else ""
        b = idx.get(norm(company))
        if b:
            b["user"], b["pass"] = user, pw
            if base:
                b["base"] = base
            upd.append(company)
        else:
            if not base:
                base = "http://localhost/%s/odata/standard.odata" % company.replace(" ", "")
                placeholders.append(company)
            entry = {"company": company, "base": base, "user": user, "pass": pw}
            bases.append(entry)
            idx[norm(company)] = entry
            add.append(company)
    with io.open(bpath, "w", encoding="utf-8") as f:      # utf-8 без BOM
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("Обновлено записей: %d · добавлено новых: %d · всего баз: %d" % (len(upd), len(add), len(bases)))
    for c in upd:
        print("  ~ обновлён:", c)
    for c in add:
        print("  + добавлен:", c)
    if placeholders:
        print("\n⚠ У этих новых баз base-URL — ПЛЕЙСХОЛДЕР, поправь на реальную публикацию:")
        for c in placeholders:
            print("   ", c)


if __name__ == "__main__":
    main()
