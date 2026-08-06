# -*- coding: utf-8 -*-
"""Ядро накопителя: прочитать условия договора по одной заявке Bitrix.
Использует bx_reader (Bitrix + Claude API). Отдельно от server.py, чтобы дёргать и из CLI/батча."""
import os, re
import bx_reader as R


def _find_bin(item, title):
    for v in list((item or {}).values()) + [title]:
        m = re.search(r"\b(\d{12})\b", str(v))
        if m:
            return m.group(1)
    return ""


def analyze(num, bin_override=""):
    """№ заявки → {num,id,title,bin,terms,attachments}. terms — из договора/КП (Claude API)."""
    item = R.item_by_num(num)
    if not item:
        return {"num": num, "error": "заявка не найдена в Bitrix"}
    title = item.get("title", "")
    files = R.file_fields(item)
    paths = []
    for _, url, _ in files:
        try:
            paths.append(R.download(url))
        except Exception:
            pass
    if paths:
        terms = R.read_docs(paths, R.NAKOPITEL_INSTRUCTION, R.NAKOPITEL_SCHEMA)
    else:
        terms = {"error": "нет вложений в заявке"}
    return {"num": str(num), "id": item.get("id"), "title": title,
            "bin": bin_override or _find_bin(item, title), "terms": terms,
            "attachments": [os.path.basename(p) for p in paths]}
