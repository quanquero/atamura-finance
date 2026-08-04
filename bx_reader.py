# -*- coding: utf-8 -*-
"""Чтение вложений заявки Bitrix (воронка оплат 178) внутри фин.блока.
Тянет заявку по № → находит файловые поля → скачивает договор/КП/счёт →
читает через `claude -p` (на подписке) → извлекает условия для накопителя.

Куски перенесены из бота договоров (bitrix/attachments/ai), адаптированы под воронку 178.
Env: BITRIX_WEBHOOK (тот же, что у sync_bitrix), CLAUDE_BIN / claude в PATH."""
import os, re, ssl, json, glob, shutil, hashlib, subprocess, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE
CACHE = os.path.join(BASE, "data", "att_cache")


def _load_env():
    for name in (".env",):
        p = os.path.join(BASE, name)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()
BITRIX = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
BX_ENTITY = 178
BX_NUM_FIELD = "ufCrm4_1644310716"   # № заявки


def _bx(method, params):
    req = urllib.request.Request(f"{BITRIX}/{method}.json",
                                 data=json.dumps(params).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=_CTX, timeout=40) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(f"{d['error']}: {d.get('error_description')}")
    return d["result"]


def item_by_num(num):
    r = _bx("crm.item.list", {"entityTypeId": BX_ENTITY, "filter": {BX_NUM_FIELD: str(num)}})
    items = r.get("items", [])
    return items[0] if items else None


def _file_entries(v):
    """Файловое UF-поле (dict или list of {urlMachine,url,downloadUrl,name}) → [(url,name)]."""
    entries = v if isinstance(v, list) else ([v] if v else [])
    out = []
    for e in entries:
        if isinstance(e, dict):
            u = e.get("urlMachine") or e.get("url") or e.get("downloadUrl")
            if u:
                out.append((u, e.get("name", "")))
    return out


def file_fields(item):
    """Все вложения карточки → [(field_code, url, name)] (авто-детект, без хардкода UF-кодов 178)."""
    out = []
    for k, v in (item or {}).items():
        for u, name in _file_entries(v):
            out.append((k, u, name))
    return out


def download(url):
    """Скачать вложение → путь. Тип по magic-bytes. Кэш по md5 (повторно не качаем)."""
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.md5(url.encode()).hexdigest()[:16]
    hit = glob.glob(os.path.join(CACHE, key + ".*"))
    if hit:
        return hit[0]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=60) as r:
        data = r.read()
    head = data[:8]
    if head[:4] == b"%PDF":
        ext = "pdf"
    elif head[:2] == b"PK":
        ext = "docx" if (b"word/" in data[:6000] or b"word/document" in data) else "xlsx"
    elif head[:3] == b"\xff\xd8\xff":
        ext = "jpg"                        # Read определяет тип по расширению — даём настоящее
    elif head[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    else:
        ext = "bin"
    p = os.path.join(CACHE, f"{key}.{ext}")
    open(p, "wb").write(data)
    return p


# ---------- чтение через claude -p ----------
def _claude_bin():
    b = os.environ.get("CLAUDE_BIN")
    if b and os.path.exists(b):
        return b
    return shutil.which("claude")


def _docx_text(path):
    try:
        from docx import Document
        d = Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts += [c.text.strip() for c in row.cells if c.text.strip()]
        return "\n".join(parts)
    except Exception:
        return ""


def _xlsx_text(path):
    try:
        import openpyxl
        ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
        return "\n".join(" ".join(str(x) for x in row if x is not None)
                         for row in ws.iter_rows(values_only=True))
    except Exception:
        return ""


def _extract_json(text):
    """Выцепить JSON: снять ```-заборы, взять последнюю сбалансированную {…}."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    depth, start, best = 0, -1, None
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    best = t[start:i + 1]
    for cand in (best, t):
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            pass
    return None


def read_docs(paths, instruction, schema_hint):
    """Прочитать документы (pdf/img читает Read сам — многостранично; docx/xlsx подаём текстом).
    Backend: claude -p (подписка). Возвращает dict по schema_hint или None."""
    cli = _claude_bin()
    if not cli:
        return {"error": "claude CLI не найден"}
    read_paths, dirs, extra = [], set(), ""
    for p in paths:
        low = p.lower()
        if low.endswith((".pdf", ".png", ".jpg", ".jpeg")):
            read_paths.append(p); dirs.add(os.path.dirname(os.path.abspath(p)))
        elif low.endswith(".docx"):
            extra += f"\n[docx {os.path.basename(p)}]\n{_docx_text(p)[:12000]}"
        elif low.endswith(".xlsx"):
            extra += f"\n[xlsx {os.path.basename(p)}]\n{_xlsx_text(p)[:12000]}"
    prompt = instruction + "\n"
    for rp in read_paths:
        prompt += f"Прочитай файл целиком (все страницы): {rp}\n"
    if extra:
        prompt += "Содержимое приложенных документов:\n" + extra + "\n"
    prompt += f"Верни СТРОГО один JSON по форме {schema_hint}. Только JSON, без текста вокруг."
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # иначе claude берёт API-ключ вместо OAuth и падает
    if not (env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    cmd = [cli, "-p", "--output-format", "json", "--allowed-tools", "Read"]
    for d in dirs:
        cmd += ["--add-dir", d]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=600, encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return {"error": str(e)}
    text = r.stdout or ""
    try:                                   # --output-format json → конверт {type:result, result:"…"}
        envj = json.loads(text)
        if isinstance(envj, dict) and "result" in envj:
            text = envj.get("result") or ""
    except Exception:
        pass
    j = _extract_json(text)
    if j:
        return j
    return {"error": "не распознал JSON", "raw": (text or r.stderr or "")[:800]}


# схема условий накопителя (что достаём из договора/КП)
NAKOPITEL_SCHEMA = ('{"contract_no":"№ договора","contract_date":"дата","total":число_сумма_договора,'
                    '"currency":"KZT","nds":"с НДС|без НДС","avans_pct":число,"avans_sum":число,'
                    '"retention_pct":число_гарантийное_удержание,"retention_sum":число,'
                    '"barter":true_или_false,"barter_sum":число,"ochered":"очередь/блок",'
                    '"object":"ЖК/объект","notes":"важные условия одной строкой"}')
NAKOPITEL_INSTRUCTION = (
    "Ты финансовый контролёр стройхолдинга. Перед тобой договор подряда/поставки и/или КП (Казахстан). "
    "Извлеки финансовые условия: точную сумму договора, аванс (% и сумму), гарантийное удержание (%), "
    "бартер (есть ли, сумма), очередь/блок и объект (ЖК). Если чего-то нет — ставь 0 или пустую строку.")
