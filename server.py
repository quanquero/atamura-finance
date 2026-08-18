# -*- coding: utf-8 -*-
"""
ATAMŪRA Finance — веб-ЛК финдира (приёмник + дашборд).
Принимает срез от парсера (POST /api/ingest, X-Service-Key), кладёт в своё ядро (SQLite),
показывает дашборд: сводка по холдингу / дубли / расхождения / топ поставщиков.
Только стандартная библиотека Python 3. Запуск:  python server.py

Env:  SERVICE_KEY (ключ для /api/ingest),  PORT (по умолч. 8013),  HOST (0.0.0.0 в проде).
"""
import base64, hashlib, hmac, http.server, json, math, os, re, socketserver, sqlite3, threading, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env_file():
    """Подтянуть .env фин.блока в окружение (для запусков из шелла: батч/инструменты).
    setdefault — systemd-переменные имеют приоритет, не перетираем."""
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

DB   = os.path.join(HERE, "finance_core.sqlite3")
PORT = int(os.environ.get("PORT", "8013"))
HOST = os.environ.get("HOST", "127.0.0.1")
KEY  = os.environ.get("SERVICE_KEY", "dev-finance-key")   # в проде задать через env
# Вебхук Bitrix (crm) — финсервер сам тянет Служебные записки (воронка оплат, entityTypeId 178)
BITRIX = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
BX_PORTAL = BITRIX.split("/rest/")[0] if "/rest/" in BITRIX else ""   # для ссылок на карточки заявок
BX_ENTITY = 178
BX_MONTHS = int(os.environ.get("BX_MONTHS", "6"))   # окно заявок шире среза 1С: платёж может ссылаться на старую/продлённую заявку

# --- Простой вход логин/пароль (V1.0.0). AUTH_USERS="login:sha256hex;login2:sha256hex" ---
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or KEY   # подпись куки сессии
AUTH_USERS = {}
for _pair in os.environ.get("AUTH_USERS", "").split(";"):
    if ":" in _pair:
        _u, _h = _pair.split(":", 1)
        AUTH_USERS[_u.strip()] = _h.strip().lower()
AUTH_ON = bool(AUTH_USERS)                            # нет пользователей → вход выключен (как раньше)


def _pw_hash(pw):
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _make_session(user, days=7):
    exp = int(datetime.now(timezone.utc).timestamp()) + days * 86400
    sig = hmac.new(SESSION_SECRET.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{user}|{exp}|{sig}".encode()).decode()


def _check_session(cookie):
    try:
        raw = base64.urlsafe_b64decode(cookie.encode()).decode()
        user, exp, sig = raw.rsplit("|", 2)
        if int(exp) < int(datetime.now(timezone.utc).timestamp()):
            return None
        good = hmac.new(SESSION_SECRET.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return user if hmac.compare_digest(good, sig) else None
    except Exception:
        return None


def _db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")           # параллельные воркеры чтения — переживём блокировку записи
    c.execute("""CREATE TABLE IF NOT EXISTS flow(
        company TEXT, kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS zayavka(
        id INTEGER, number TEXT, title TEXT, supplier TEXT, amount REAL, stage TEXT,
        company TEXT, bx_object TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reestr(
        src TEXT, num TEXT, name TEXT, bin TEXT, iban TEXT, amount REAL, purpose TEXT, invoice TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS zayavka_idx(num TEXT PRIMARY KEY, id INTEGER)")
    # накопитель: условия договора, прочитанные ИИ из PDF (кеш — договор не меняется)
    c.execute("""CREATE TABLE IF NOT EXISTS nakopitel(
        num TEXT PRIMARY KEY, bin TEXT, contract_no TEXT, contract_date TEXT,
        total REAL, avans_sum REAL, retention_pct REAL, retention_sum REAL,
        barter INT, barter_sum REAL, object TEXT, ochered TEXT, account TEXT,
        notes TEXT, title TEXT, read_ts TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS adata_cache(bin TEXT PRIMARY KEY, short TEXT, json TEXT, ts TEXT)")
    # смета объекта (для БДДС): статья × блок × бюджет × помесячный план (plan — JSON month→сумма)
    c.execute("""CREATE TABLE IF NOT EXISTS smeta(
        object TEXT, ochered TEXT, block TEXT, code TEXT, article TEXT,
        budget REAL, plan TEXT, canon TEXT)""")
    # миграция: старая zayavka без company (создана прошлой версией)
    zcols = {r[1] for r in c.execute("PRAGMA table_info(zayavka)").fetchall()}
    if "company" not in zcols:
        c.execute("ALTER TABLE zayavka ADD COLUMN company TEXT")
    if "bx_object" not in zcols:
        c.execute("ALTER TABLE zayavka ADD COLUMN bx_object TEXT")
    nkcols = {r[1] for r in c.execute("PRAGMA table_info(nakopitel)").fetchall()}
    if "article" not in nkcols:                    # вид работ (статья) — ИИ извлекает из договора
        c.execute("ALTER TABLE nakopitel ADD COLUMN article TEXT")
    if "vypolneno" not in nkcols:                  # выполнено по АВР (принятые работы, per заявка)
        c.execute("ALTER TABLE nakopitel ADD COLUMN vypolneno REAL")
    return c


def _company_from(title):
    """Компания-плательщик из названия заявки: «№X / КОМПАНИЯ / поставщик / …»."""
    parts = [p.strip() for p in str(title or "").split("/")]
    return parts[1] if len(parts) > 1 else ""


# Классификатор типа расхода (эвристика по ключевым словам назначения/расшифровки).
# Порядок важен: сначала подряд (работы), потом поставка (материалы), потом услуга.
_CAT_RULES = [
    ("подряд",   ["подряд", "смр", "строительно-монтаж", "монтаж", "демонтаж", "устройство",
                  "кладк", "штукатур", "заливк", "каркас", "стяжк", "кровл", "фасад",
                  "бетонн", "земляны", "прораб", " работ", "выполнен работ"]),
    ("поставка", ["поставк", "материал", "товар", "арматур", "бетон", "кирпич", "песок",
                  "щебень", "цемент", "плит", "металл", "труб", "кабель", "светильник",
                  "оборудован", "закуп", "стальн", "профнастил", "утеплител", "гсм", "запчаст"]),
    ("услуга",   ["услуг", "обслуж", "аренд", "консультац", "экспертиз", "проектир", "надзор",
                  "охран", "клининг", "реклам", "юридич", "аудит", "транспорт", "доставк",
                  "связь", "подписк", "страхов", "госпошлин", "налог", "лицензи", "разработк"]),
]


def _category(*texts):
    t = " ".join(str(x or "") for x in texts).lower()
    for cat, kws in _CAT_RULES:
        if any(k in t for k in kws):
            return cat
    return "прочее"


def _bx(method, params):
    """Вызов Bitrix REST по вебхуку. params — dict (списки/вложенность через url-кодирование)."""
    q = urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(f"{BITRIX}/{method}.json?{q}", timeout=60) as r:
        return json.load(r)


def sync_bitrix(full=True):
    """Тянем Служебные записки (воронка оплат 178) → таблица zayavka (полные строки с названиями).
    full=True (по умолч.) — ВСЕ заявки: нужно БДДС и матчингу платёж→объект (платёж может ссылаться на
    старую заявку вне окна). full=False — только окно BX_MONTHS (быстро, если понадобится)."""
    if not BITRIX:
        return {"error": "BITRIX_WEBHOOK не задан"}
    since = (datetime.now() - timedelta(days=30 * BX_MONTHS)).strftime("%Y-%m-%dT00:00:00")
    cap = 40000 if full else 6000
    import bx_reader as R
    obj_field = R.OBJECT_FIELD
    enum = R.object_enum()                                   # id → название объекта (enum «Объект эксплуатации»)
    rows, start = [], 0
    while True:
        params = {
            "entityTypeId": BX_ENTITY, "start": start, "order[id]": "desc",
            "select[]": ["id", "title", "opportunity", "stageId", "ufCrm4_1644310716",
                         "ufCrm4_1762251054209", obj_field],
        }
        if not full:
            params["filter[>=createdTime]"] = since
        d = _bx("crm.item.list", params)
        items = d.get("result", {}).get("items", [])
        rows += items
        if len(items) < 50 or len(rows) >= cap:
            break
        start += 50
    c = _db()
    c.execute("DROP TABLE IF EXISTS zayavka")
    c.execute("""CREATE TABLE zayavka(
        id INTEGER, number TEXT, title TEXT, supplier TEXT, amount REAL, stage TEXT,
        company TEXT, bx_object TEXT)""")
    c.executemany("INSERT INTO zayavka VALUES(?,?,?,?,?,?,?,?)", [(
        it.get("id"),
        str(it.get("ufCrm4_1644310716") or "").strip() or _num_from(it.get("title")),
        it.get("title", ""), str(it.get("ufCrm4_1762251054209") or ""),
        float(it.get("opportunity") or 0), it.get("stageId", ""), _company_from(it.get("title")),
        enum.get(str(it.get(obj_field)), "") if it.get(obj_field) not in (None, "", 0, "0") else "",
    ) for it in rows])
    c.execute("INSERT INTO meta(k,v) VALUES('bx_sync',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (datetime.now().strftime("%Y-%m-%d %H:%M"),))
    c.commit(); n = len(rows); c.close()
    return {"ok": True, "заявок": n}


def sync_bitrix_full(rebuild=False):
    """Лёгкий индекс № → id по заявкам (чтобы матчить платежи вне окна). Маппинг № → id неизменен
    и только растёт, поэтому по умолчанию ИНКРЕМЕНТНО: тянем лишь заявки с id больше максимального
    уже известного (order[id] desc → как встретили известный id, дальше только старые — стоп).
    rebuild=True — полный пересбор с нуля (первый запуск/если индекс побит)."""
    if not BITRIX:
        return {"error": "BITRIX_WEBHOOK не задан"}
    c = _db()
    known = dict(c.execute("SELECT num,id FROM zayavka_idx").fetchall())
    max_id = 0 if (rebuild or not known) else max(int(v) for v in known.values() if v is not None)
    new, start, stop = {}, 0, False
    while not stop:
        d = _bx("crm.item.list", {"entityTypeId": BX_ENTITY, "start": start, "order[id]": "desc",
                                   "select[]": ["id", "ufCrm4_1644310716"]})
        items = d.get("result", {}).get("items", [])
        for it in items:
            iid = it.get("id")
            if max_id and iid is not None and int(iid) <= max_id:
                stop = True; break            # дошли до уже проиндексированных — ниже только старые
            n = str(it.get("ufCrm4_1644310716") or "").strip()
            if n:
                new.setdefault(n, iid)
        if len(items) < 50 or len(known) + len(new) >= 40000:
            break
        start += 50
    if rebuild:
        c.execute("DELETE FROM zayavka_idx")
    if new or rebuild:
        c.executemany("INSERT OR REPLACE INTO zayavka_idx VALUES(?,?)", list(new.items()))
    c.execute("INSERT INTO meta(k,v) VALUES('idx_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (datetime.now().strftime("%Y-%m-%d %H:%M"),))
    c.commit(); total = (0 if rebuild else len(known)) + len(new); c.close()
    return {"ok": True, "индекс_заявок": total, "новых": len(new)}


def _num_from(s):
    """№ заявки (1xxxx) из назначения 1С. Игнорирует № счёта (сч№0230000…):
    берёт число сразу после «Референс <реф>», либо любое 5-значное 1xxxx."""
    s = str(s or "")
    m = re.search(r"Референс\s+\d{6,}\s+(1\d{4})\b", s)     # надёжно: заявка сразу после реф
    if m:
        return m.group(1)
    m = re.search(r"\b(1\d{4})\b", s)                        # 5-значный № заявки (не № счёта 0…, не реф 10 цифр)
    if m:
        return m.group(1)
    m = re.search(r"№\s*(\d{4,6})", s)                       # запасной, но не № счёта (ведущий 0)
    if m and not m.group(1).startswith("0"):
        return m.group(1)
    return ""


def _account_from(s):
    """№ счёта из назначения (сч№0230000…) — ключ привязки платёж → справка/договор."""
    m = re.search(r"сч[.№\s]*?(0\d{6,10})", str(s or ""))
    return m.group(1) if m else ""


def _object_from(s):
    """Объект/ЖК из назначения платежа / названия заявки (латиница+кириллица, все ЖК холдинга)."""
    t = str(s or "").lower()
    if "атмо" in t or "atmo" in t: return "Атмосфера"
    if "aura" in t or "aура" in t or re.search(r"\bau\s*\d|аура", t): return "Аура"
    if "керуен" in t or "keruen" in t or re.search(r"\bkr\s*\d", t): return "Керуен"
    if "аксай" in t or "aksai" in t or "aqsai" in t: return "Аксай"
    if "bion" in t or "бион" in t: return "BION"
    if "amaia" in t or "амайа" in t or "амая" in t: return "AMAIA"
    if "браво" in t or "bravo" in t: return "Браво"
    if "неон" in t or "neon" in t: return "Неон"
    return ""


_ORG_WORDS = ("товарищество с ограниченной ответственностью", "тоо", "ип", "ао", "оао",
              "зао", "физ лицо", "физлицо", "иин", "бин")


def _sup_tokens(s):
    """Значимые слова названия поставщика (для фолбэк-матчинга, когда № не совпал)."""
    t = str(s or "").lower()
    for w in _ORG_WORDS:
        t = t.replace(w, " ")
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)
    return {w for w in t.split() if len(w) >= 4}


def _stage_kind(stage):
    """Категория стадии Bitrix: success (Успешно) / fail (Отказано) / progress (в работе)."""
    s = str(stage or "").upper()
    if ":SUCCESS" in s: return "success"
    if ":FAIL" in s:    return "fail"
    return "progress"


def _donut(pairs, colors, size=190, r=66, w=26):
    """SVG-пончик по [(label, value)]. colors: label→hex. Легенда с суммами — снаружи."""
    total = sum(v for _, v in pairs if v > 0) or 1
    cx = cy = size / 2
    segs, a0 = "", -math.pi / 2
    for label, v in pairs:
        if v <= 0: continue
        frac = v / total
        a1 = a0 + frac * 2 * math.pi
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if frac > 0.5 else 0
        col = colors.get(label, "#94a3b8")
        segs += (f"<path d='M {x0:.2f} {y0:.2f} A {r} {r} 0 {large} 1 {x1:.2f} {y1:.2f}' "
                 f"fill=none stroke='{col}' stroke-width={w}><title>{label}: {money(v)} ₸ · {frac*100:.0f}%</title></path>")
        a0 = a1
    return (f"<svg viewBox='0 0 {size} {size}' width={size} height={size} class=donut>"
            f"{segs}<text x={cx} y={cy-4} text-anchor=middle class=dc>{money(total)}</text>"
            f"<text x={cx} y={cy+14} text-anchor=middle class=dcl>₸ всего</text></svg>")


def _timeseries(flow):
    """Дневной ряд отток/приток + топ-платежи дня (для тултипа «из-за чего просело»)."""
    by_day = defaultdict(lambda: {"out": 0.0, "in": 0.0, "drv": []})
    for r in flow:
        d = str(r[2] or "")[:10]
        if not d: continue
        if r[1] == "out":
            by_day[d]["out"] += r[5]
            by_day[d]["drv"].append((r[5], (r[4] or r[0] or "")[:32]))
        elif r[1] == "in":
            by_day[d]["in"] += r[5]
    out = []
    for d in sorted(by_day):
        v = by_day[d]
        top = sorted(v["drv"], reverse=True)[:3]
        out.append({"d": d, "out": round(v["out"]), "in": round(v["in"]),
                    "top": [{"n": n, "a": round(a)} for a, n in top]})
    return out


def reconcile():
    """Сверка: заявки Bitrix ↔ платежи поставщикам 1С по № заявки (из назначения).
    Возвращает реестр заявок со статусом, разрез по компаниям, оплаты без заявки."""
    c = _db()
    pays = c.execute("SELECT company,name,amount,date,purpose,doc,bin FROM flow WHERE kind='out' AND supplier=1").fetchall()
    zs = c.execute("SELECT id,number,company,supplier,amount,stage FROM zayavka").fetchall()
    rs = c.execute("SELECT num,name,bin FROM reestr").fetchall()
    nk_acc = c.execute("SELECT num,account FROM nakopitel").fetchall()
    idx = dict(c.execute("SELECT num,id FROM zayavka_idx").fetchall()); c.close()
    znums_win = {z[1] for z in zs if z[1]}          # заявки в окне (для статуса)
    # для определения «сироты» — полный индекс всех заявок Bitrix, если загружен; иначе окно
    known_nums = set(idx) if idx else znums_win
    # реестр финотдела (из Excel) — 3-й источник: по № и по БИН
    reestr_nums = {r[0] for r in rs if r[0]}
    reestr_by_bin = {}
    for rnum, rname, rbin in rs:
        if rbin:
            reestr_by_bin.setdefault(rbin, (rnum, rname))
    # индекс поставщиков заявок: значимое слово → множество (id, №) — для фолбэка по имени
    z_by_tok = defaultdict(set)
    for zid, num, comp, sup, amt, stage in zs:
        for w in _sup_tokens(sup):
            z_by_tok[w].add((zid, num))

    def _cand_by_supplier(name):
        """Кандидат-заявка по совпадению слов поставщика (когда № не совпал)."""
        score = defaultdict(int)
        for w in _sup_tokens(name):
            for zc in z_by_tok.get(w, ()):
                score[zc] += 1
        if not score:
            return None
        return max(score.items(), key=lambda x: x[1])[0]   # (zid, num) с макс. пересечением

    # 2-я нога матча: № счёта из накопителя → № заявки; суммы заявок — для матча по поставщик+сумма
    acc_to_num = {}
    for nnum, acc in nk_acc:
        ad = re.sub(r"\D", "", str(acc or ""))
        if len(ad) >= 6:
            acc_to_num.setdefault(ad, str(nnum))
    z_amt = {num: (amt or 0) for zid, num, comp, sup, amt, stage in zs if num}

    def _match_num(purpose, amt, name):
        """Оплата → № заявки: 1) № в назначении, 2) № счёта (накопитель), 3) поставщик+сумма."""
        zn = _num_from(purpose)
        if zn and zn in known_nums:
            return zn
        pd = re.sub(r"\D", "", purpose or "")
        if pd:
            for ad, nnum in acc_to_num.items():               # № счёта в назначении
                if ad in pd:
                    return nnum
        if name:                                              # поставщик + близкая сумма
            cand = _cand_by_supplier(name)
            if cand and cand[1] and abs((z_amt.get(cand[1], 0) or 0) - (amt or 0)) < max(1.0, 0.02 * (amt or 0)):
                return cand[1]
        return None

    pay_nums, pay_no_num, pay_no_zayavka = {}, [], []
    cash_tot, cash_n, cash_matched = 0.0, 0, 0
    for company, name, amt, date, purpose, doc, pbin in pays:
        zn = _num_from(purpose)
        mnum = _match_num(purpose, amt, name)        # многоключевой матч
        if mnum:
            pay_nums[mnum] = True
        if "кассов" in (doc or "").lower():          # РКО — наличные, отдельный поток
            cash_tot += amt; cash_n += 1
            if mnum: cash_matched += 1
            continue
        if mnum:
            continue                                 # сматчено (№/счёт/поставщик+сумма) — не сирота
        if not zn:
            pay_no_num.append((company, name, amt, date))
        else:                                        # № есть, но заявки такой нет и иначе не сматчилось
            hit = None
            if zn in reestr_nums:
                hit = ("№", zn, "")
            elif pbin and pbin in reestr_by_bin:
                rn, rnm = reestr_by_bin[pbin]
                hit = ("БИН", rn, rnm)
            pay_no_zayavka.append((zn, company, name, amt, date, _cand_by_supplier(name), hit))
    z_list, by_company = [], defaultdict(lambda: [0, 0])   # by_company: [оплачено, всего]
    reserve, in_progress, rejected = [], [], []
    for zid, num, comp, sup, amt, stage in zs:
        m = num in pay_nums
        sk = _stage_kind(stage)
        z_list.append((zid, num, comp, sup, amt, m, sk))
        by_company[comp][1] += 1
        if m:
            by_company[comp][0] += 1
        elif sk == "fail":
            rejected.append(z_list[-1])
        elif sk == "success":
            reserve.append(z_list[-1])      # Bitrix одобрил, а оплаты по 1С нет — реальный резерв
        else:
            in_progress.append(z_list[-1])  # ещё в работе
    matched_n = sum(1 for z in z_list if z[5])
    waiting = reserve + in_progress
    nz_in_reestr = sum(1 for x in pay_no_zayavka if x[6])
    return {"pays": len(pays), "z_total": len(zs), "matched_n": matched_n, "z_list": z_list,
            "reserve": reserve, "in_progress": in_progress, "rejected": rejected,
            "waiting": waiting, "pay_no_zayavka": pay_no_zayavka, "pay_no_num": pay_no_num,
            "by_company": dict(by_company),
            "cash_tot": cash_tot, "cash_n": cash_n, "cash_matched": cash_matched,
            "reestr_rows": len(rs), "nz_in_reestr": nz_in_reestr,
            "nz_orphan": len(pay_no_zayavka) - nz_in_reestr}


def store(payload):
    """Принять срез: заменить flow, запомнить время/период. payload={flow:[...], months, ts}."""
    rows = payload.get("flow", [])
    c = _db()
    c.execute("DELETE FROM flow")
    c.executemany("INSERT INTO flow VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r.get("company",""), r.get("kind",""), r.get("doc",""), r.get("number",""), r.get("date",""),
         r.get("bin",""), r.get("name",""), float(r.get("amount") or 0), r.get("vidop",""),
         int(r.get("supplier") or 0), r.get("purpose",""), r.get("comment",""), r.get("dogovor_key",""))
        for r in rows])
    for k, v in (("ts", payload.get("ts", "")), ("months", str(payload.get("months", "")))):
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit(); n = len(rows); c.close()
    return n


def store_reestr(payload):
    """Принять реестр финотдела (из Excel): заменить таблицу reestr. payload={rows:[...], ts}."""
    rows = payload.get("rows", [])
    c = _db()
    c.execute("DELETE FROM reestr")
    c.executemany("INSERT INTO reestr VALUES(?,?,?,?,?,?,?,?)", [
        (r.get("src", ""), str(r.get("num", "")), r.get("name", ""), str(r.get("bin", "")),
         r.get("iban", ""), float(r.get("amount") or 0), r.get("purpose", ""), r.get("invoice", ""))
        for r in rows])
    c.execute("INSERT INTO meta(k,v) VALUES('reestr_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (payload.get("ts", ""),))
    c.commit(); n = len(rows); c.close()
    return n


def _canon_article(name):
    """Свод статьи сметы к канону 13-справочника (для сравнения между объектами)."""
    s = (name or "").lower()
    pairs = [
        (("монолит", "перекрыт", "балк", "фундамент", "сваи", "усиление стен"), "Монолит/фундамент"),
        (("фасад", "наружн"), "Фасад"),
        (("кровл",), "Кровля"),
        (("окн", "витраж", "двер", "проем", "ворота"), "Окна/двери/проёмы"),
        (("внутрен", "отделк"), "Внутренняя отделка"),
        (("пол ", "полы"), "Полы"),
        (("кладк", "перегород"), "Кладка/перегородки"),
        (("лифт",), "Лифт"),
        (("земл", "котлован"), "Земляные"),
        (("отоплен", "вентил", "кондицион", "водопровод", "канализ", "газоснаб", "электро", "слаботоч", "пожар", "сети"), "Инж. сети"),
        (("благоустр", "озелен", "маф", "подпорн"), "Благоустройство"),
        (("аренд", "машин", "механизм", "гсм", "инструмент"), "Механизмы/накладные"),
    ]
    for keys, canon in pairs:
        if any(k in s for k in keys):
            return canon
    return "Прочее"


def store_smeta(payload):
    """Принять смету объекта: заменить строки этого объекта. payload={object,ochered,months,articles:[…]}."""
    obj = payload.get("object", "")
    arts = [a for a in payload.get("articles", []) if "." in str(a.get("code", ""))]  # только статьи, не разделы
    c = _db()
    c.execute("DELETE FROM smeta WHERE object=?", (obj,))
    c.executemany("INSERT INTO smeta VALUES(?,?,?,?,?,?,?,?)", [
        (obj, str(payload.get("ochered", "")), str(a.get("block", "")), str(a.get("code", "")),
         a.get("article", ""), float(a.get("budget") or 0),
         json.dumps(a.get("plan", {}), ensure_ascii=False), _canon_article(a.get("article", "")))
        for a in arts])
    c.execute("INSERT INTO meta(k,v) VALUES('smeta_ts',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (datetime.now().strftime("%Y-%m-%d %H:%M"),))
    c.commit(); n = len(arts); c.close()
    return n


def bdds_data():
    """БДДС по объектам: объект → статья (канон) × Бюджет(смета) / Договор / Оплачено / Остаток,
    + Выполнено (1С-поступления) на уровне объекта. Три ноги + бюджет там, где смета есть."""
    c = _db()
    sm = c.execute("SELECT object,budget,canon FROM smeta").fetchall()
    nk = c.execute("SELECT num,object,notes,title,total,article,vypolneno FROM nakopitel").fetchall()
    zs = c.execute("SELECT number,title,bx_object FROM zayavka").fetchall()   # ВСЕ заявки Bitrix — мост к объекту (bx_object = enum, точный)
    pays = c.execute("SELECT amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall()
    meta = dict(c.execute("SELECT k,v FROM meta").fetchall()); c.close()
    smeta_budget = defaultdict(lambda: defaultdict(float))    # object → canon → бюджет
    smeta_objs = set()
    for obj, budget, canon in sm:
        o = _object_from(obj) or obj                          # канон объекта («Аура» и т.п.)
        smeta_budget[o][canon] += budget or 0; smeta_objs.add(o)
    # № заявки → (объект, статья-канон) из названия «№ / компания / поставщик / ОБЪЕКТ / РАБОТА / …» — покрывает ВСЕ заявки
    z_obj, z_canon = {}, {}
    for number, title, bxo in zs:
        n = str(number or "").strip() or _num_from(title or "")
        if n:
            # приоритет — точный enum-объект заявки; канон через _object_from, иначе сырое имя из enum
            z_obj[n] = _object_from(bxo or "") or _object_from(title or "") or (bxo or "").strip() or ""
            z_canon[n] = _canon_article(title or "")
    # прочитанные договоры: точный объект/статья (ИИ) + сумма договора
    nk_by_num = {}
    for num, obj, notes, title, total, article, vyp in nk:
        n = str(num)
        canon = _canon_article(article) if article else (z_canon.get(n) or _canon_article((notes or "") + " " + (title or "")))
        nk_by_num[n] = {"object": _object_from(obj) or z_obj.get(n) or _object_from(title or "") or _object_from(obj or "") or "—",
                        "canon": canon, "total": total or 0, "vyp": vyp or 0}

    def attr(purpose):
        n = _num_from(purpose)
        info = nk_by_num.get(n)
        if info:
            return info["object"] or "—", info["canon"]
        return (z_obj.get(n) or _object_from(purpose) or "—"), (z_canon.get(n) or _canon_article(purpose))

    contr, opl, vyp_oc = defaultdict(float), defaultdict(float), defaultdict(float)   # (object,canon) →
    obj_paid, obj_done, obj_contr = (defaultdict(float) for _ in range(3))
    for info in nk_by_num.values():
        key = (info["object"], info["canon"])
        contr[key] += info["total"]; obj_contr[info["object"]] += info["total"]
        vyp_oc[key] += info["vyp"]; obj_done[info["object"]] += info["vyp"]         # выполнено = АВР (точно)
    for amt, purpose in pays:
        o, cn = attr(purpose); obj_paid[o] += amt or 0; opl[(o, cn)] += amt or 0
    objects = set(smeta_objs) | set(obj_paid) | {i["object"] for i in nk_by_num.values()}
    out = []
    for o in sorted(objects):
        canons = set(smeta_budget.get(o, {})) | {cn for (oo, cn) in contr if oo == o} | {cn for (oo, cn) in opl if oo == o}
        arts = []
        for cn in canons:
            b = smeta_budget.get(o, {}).get(cn, 0.0); ct = contr.get((o, cn), 0.0); op = opl.get((o, cn), 0.0)
            vy = vyp_oc.get((o, cn), 0.0)
            if not (b or ct or op or vy):
                continue
            arts.append({"article": cn, "budget": b, "contracted": ct, "oplacheno": op,
                         "vypolneno": vy, "ostatok": (b or ct) - op})
        out.append({"object": ("(объект не распознан)" if o == "—" else o), "has_smeta": o in smeta_objs,
                    "budget": sum(smeta_budget.get(o, {}).values()),
                    "contracted": obj_contr.get(o, 0.0), "oplacheno": obj_paid.get(o, 0.0),
                    "vypolneno": obj_done.get(o, 0.0),
                    "articles": sorted(arts, key=lambda x: -(x["oplacheno"] or x["budget"] or x["contracted"]))})
    out.sort(key=lambda x: -(x["oplacheno"] or x["budget"] or x["contracted"]))
    return {"objects": out, "smeta_objs": sorted(smeta_objs), "smeta_ts": meta.get("smeta_ts", "")}


def zayavka_card(num):
    """Провал в заявку: ВСЕ документы по ярлыкам (Договор/АВР/Счёт/Тех.требование/Доверенность) +
    оплаты 1С по этой заявке + условия накопителя, если договор прочитан."""
    import bx_reader as R
    item = R.item_by_num(num)
    docs = R.item_documents(item) if item else []
    c = _db()
    nkrow = c.execute("""SELECT contract_no,total,object,ochered,article,notes,bin,
                         avans_sum,retention_sum,barter,barter_sum FROM nakopitel WHERE num=?""", (str(num),)).fetchone()
    pays = c.execute("SELECT date,amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall()
    c.close()
    payments = sorted(({"date": d, "amount": a, "account": _account_from(p), "object": _object_from(p)}
                       for d, a, p in pays if _num_from(p) == str(num)), key=lambda x: x["date"] or "")
    nk = None
    if nkrow and (nkrow[0] or (nkrow[1] or 0) or (nkrow[4] or "")):   # есть договор ИЛИ сумма ИЛИ статья
        nk = {"contract_no": nkrow[0], "total": nkrow[1], "object": nkrow[2], "ochered": nkrow[3],
              "article": nkrow[4], "notes": nkrow[5], "bin": nkrow[6], "avans": nkrow[7],
              "retention": nkrow[8], "barter": bool(nkrow[9]), "barter_sum": nkrow[10]}
    return {"num": str(num), "id": (item.get("id") if item else None),
            "title": (item.get("title", "") if item else ""), "docs": docs,
            "payments": payments, "nakopitel": nk, "bx_portal": BX_PORTAL, "bx_entity": BX_ENTITY}


def store_nakopitel(num, bin_, terms, title=""):
    """Сохранить условия договора (прочитанные ИИ) в кеш накопителя."""
    t = terms or {}
    acc = t.get("account") or _account_from(t.get("notes", ""))
    c = _db()
    c.execute("""INSERT OR REPLACE INTO nakopitel
        (num,bin,contract_no,contract_date,total,avans_sum,retention_pct,retention_sum,
         barter,barter_sum,object,ochered,account,notes,title,read_ts,article,vypolneno)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(num), str(bin_ or ""), t.get("contract_no", ""), t.get("contract_date", ""),
        float(t.get("total") or 0), float(t.get("avans_sum") or 0),
        float(t.get("retention_pct") or 0), float(t.get("retention_sum") or 0),
        1 if t.get("barter") else 0, float(t.get("barter_sum") or 0),
        t.get("object", ""), t.get("ochered", ""), acc, t.get("notes", ""), title,
        datetime.now().strftime("%Y-%m-%d %H:%M"), t.get("article", ""), float(t.get("vypolneno") or 0)))
    c.commit(); c.close()


def store_adata(bin_, data):
    """Кешировать справку Adata по БИН (читаем один раз)."""
    short = (data or {}).get("basic", {}).get("short_name", "")
    c = _db()
    c.execute("INSERT OR REPLACE INTO adata_cache VALUES(?,?,?,?)",
              (str(bin_), short, json.dumps(data, ensure_ascii=False),
               datetime.now().strftime("%Y-%m-%d %H:%M")))
    c.commit(); c.close()


def _nk_pipeline(num, bin_override="", progress=None):
    """Общий конвейер накопителя с этапами (progress(stage,msg)): Bitrix → вложения → ИИ →
    сохранение → Adata. Используется и синхронно (CLI), и в фоновой задаче (UI)."""
    def P(stage, msg):
        if progress:
            progress(stage, msg)
    import bx_reader as R, nakopitel as NK
    P("bitrix", "Открываю заявку в Bitrix…")
    item = R.item_by_num(num)
    if not item:
        return {"error": "заявка не найдена в Bitrix", "num": str(num)}
    title = item.get("title", "")
    files = R.file_fields(item)
    P("download", f"Скачиваю вложения ({len(files)})…")
    paths = []
    for _, url, _ in files:
        try: paths.append(R.download(url))
        except Exception: pass
    binf = bin_override or NK._find_bin(item, title)
    if not paths:
        return {"error": "нет вложений в заявке", "num": str(num)}     # пустую строку НЕ пишем
    P("read", f"ИИ читает договор ({len(paths)} файл.) через Claude API…")
    terms = R.read_docs(paths, R.NAKOPITEL_INSTRUCTION, R.NAKOPITEL_SCHEMA)
    if (terms or {}).get("error") or not (terms.get("contract_no") or terms.get("total") or terms.get("article")):
        return {"error": (terms or {}).get("error") or "документы не распознаны как договор",
                "num": str(num), "terms": terms}                        # не сохраняем пустышку
    if not binf:                                   # БИН не нашёлся в заявке — берём из договора (ИИ)
        bcand = re.sub(r"\D", "", str((terms or {}).get("bin", "")))
        if len(bcand) == 12:
            binf = bcand
    P("save", "Сохраняю условия в накопитель…")
    store_nakopitel(num, binf, terms, title)
    adata_ok = False
    if binf and not (terms or {}).get("error"):
        P("adata", "Тяну справку Adata по БИН…")
        try:
            import adata as A
            store_adata(binf, A.fetch(binf)); adata_ok = True
        except Exception:
            pass
    return {"ok": not bool((terms or {}).get("error")), "num": str(num), "bin": binf,
            "terms": terms, "adata": adata_ok, "attachments": [os.path.basename(p) for p in paths]}


def read_nakopitel(num, bin_override=""):
    """Синхронно (для CLI): прочитать договор заявки и сохранить в накопитель."""
    return _nk_pipeline(num, bin_override)


def _merge_terms(parts):
    """Склейка результатов чтения нескольких файлов в один набор условий (для too_large-фолбэка)."""
    money_keys = ("total", "vypolneno_sum", "avans_sum", "retention_sum", "barter_sum")
    out = {}
    for t in parts:
        for k, v in (t or {}).items():
            if k in money_keys:
                out[k] = max(float(out.get(k) or 0), float(v or 0))
            elif k == "doc_kinds":
                cur = out.get(k, "")
                out[k] = (cur + (", " if cur and v else "") + (v or "")).strip(", ")
            elif k == "barter":
                out[k] = out.get(k) or v
            elif not out.get(k):
                out[k] = v
    return out


def read_zayavka_docs(num, read_contract=True, progress=None):
    """Чтение ВСЕХ вложений заявки одним ИИ-проходом с полной схемой (устойчиво к мисфайлингу).
    Классифицируем по ИМЕНИ файла+ярлыку, дедупим одинаковые (один файл часто залит в несколько полей),
    читаем комбо-файлы (АВР+КС+счёт в одном PDF) целиком → статья/сумма/выполнено/условия/БИН.
    read_contract=False — исключаем тяжёлые файлы-договоры (дёшево)."""
    def P(st, m):
        if progress:
            progress(st, m)
    import bx_reader as R, nakopitel as NK
    P("bitrix", "Открываю заявку в Bitrix…")
    item = R.item_by_num(num)
    if not item:
        return {"error": "заявка не найдена в Bitrix", "num": str(num)}
    title = item.get("title", "")
    labels = R.field_labels()
    # собрать вложения, классифицировать по имени+ярлыку, дедупить по имени/URL
    seen, uniq = set(), []
    for field, url, name in R.file_fields(item):
        kind = R.classify_doc(labels.get(field, ""), name)
        if not read_contract and kind == "contract":
            continue                                   # дешёвый режим — без тяжёлых договоров
        key = (name or url).strip().lower()
        if key in seen:
            continue                                   # тот же файл в другом поле — не качаем дважды
        seen.add(key); uniq.append((field, url, name, kind))
    if not uniq:
        return {"num": str(num), "error": "нет вложений в заявке"}
    P("download", f"Скачиваю вложения ({len(uniq)})…")
    paths, kinds, fileinfo = [], [], []
    for field, url, name, kind in uniq:
        try:
            pth = R.download(url)
            paths.append(pth); kinds.append(kind or "?")
            fileinfo.append("%s[%s]" % (name or "файл", os.path.splitext(pth)[1].lstrip(".") or "?"))
        except Exception:
            pass
    if not paths:
        return {"num": str(num), "error": "не удалось скачать вложения"}
    files_desc = ", ".join(fileinfo)
    P("read", f"ИИ читает {len(paths)} документ(ов) через Claude API (по содержимому, не по полю)…")
    terms = R.read_docs(paths, R.NAKOPITEL_INSTRUCTION, R.NAKOPITEL_SCHEMA) or {}
    if terms.get("too_large") and len(paths) > 1:
        P("read", "Документы крупные — читаю по одному и склеиваю…")
        parts = []
        for p in paths:
            r = R.read_docs([p], R.NAKOPITEL_INSTRUCTION, R.NAKOPITEL_SCHEMA) or {}
            if not r.get("error"):
                parts.append(r)
        if parts:
            terms = _merge_terms(parts)
    if terms.get("error"):
        return {"num": str(num), "error": terms["error"], "too_large": terms.get("too_large", False)}
    if terms.get("vypolneno_sum"):
        terms["vypolneno"] = float(terms["vypolneno_sum"])
    if not (terms.get("article") or terms.get("total") or terms.get("vypolneno")):
        return {"num": str(num),
                "error": "документы не распознаны (ни статьи, ни суммы, ни выполнено) · файлы: %s" % (files_desc or "—"),
                "doc_kinds": terms.get("doc_kinds", "")}
    binf = terms.get("bin") or NK._find_bin(item, title)
    P("save", "Сохраняю в накопитель…")
    store_nakopitel(num, binf, terms, title)
    if binf:
        try:
            import adata as A
            store_adata(binf, A.fetch(binf))
        except Exception:
            pass
    return {"ok": True, "num": str(num), "article": terms.get("article", ""),
            "total": terms.get("total", 0), "vypolneno": terms.get("vypolneno", 0),
            "doc_kinds": terms.get("doc_kinds", ""), "slots": kinds, "bin": binf}


# ---------- фоновые задачи чтения (прогресс для UI) ----------
import threading
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = 0


def _job(jid, stage, msg, done=False, result=None):
    with _JOBS_LOCK:
        _JOBS[jid] = {"stage": stage, "msg": msg, "done": done, "result": result,
                      "ts": datetime.now().strftime("%H:%M:%S")}


def _new_job():
    global _JOB_SEQ
    with _JOBS_LOCK:
        _JOB_SEQ += 1
        jid = "nk%d" % _JOB_SEQ
        if len(_JOBS) > 60:                       # прунинг завершённых, чтобы словарь не рос
            for k in [k for k, v in list(_JOBS.items()) if v.get("done")][:40]:
                _JOBS.pop(k, None)
    _job(jid, "queued", "В очереди…")
    return jid


def _run_nk_job(jid, num, binv):
    try:
        res = _nk_pipeline(num, binv, lambda st, m: _job(jid, st, m))
        _job(jid, "error" if res.get("error") else "done",
             res.get("error") or "Готово", done=True, result=res)
    except Exception as e:
        _job(jid, "error", str(e)[:200], done=True)


def queue_zayavki(limit=300):
    """Оплаченные заявки (платёж матчится по № в назначении 1С), ещё НЕ прочитанные ИИ,
    по убыванию суммы оплаты — очередь на обработку документов."""
    c = _db()
    z = {}
    for num, title in c.execute("SELECT number,title FROM zayavka").fetchall():
        n = str(num or "").strip() or _num_from(title or "")
        if n:
            z[n] = title or ""
    paid = defaultdict(float)
    for amt, purpose in c.execute("SELECT amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall():
        n = _num_from(purpose)
        if n and n in z:
            paid[n] += amt or 0
    read = {str(r[0]) for r in c.execute(
        "SELECT num FROM nakopitel WHERE article!='' OR total>0 OR vypolneno>0").fetchall()}
    c.close()
    rows = [{"num": n, "paid": p, "object": _object_from(z.get(n, "")) or "—",
             "title": (z.get(n, "") or "")[:90]}
            for n, p in paid.items() if n not in read]
    rows.sort(key=lambda x: -x["paid"])
    return rows[:limit] if limit else rows


def queue_stats():
    """Сводка очереди обработки: сколько оплаченных заявок ждёт чтения, на какую сумму, сколько прочитано."""
    allr = queue_zayavki(limit=None)
    c = _db()
    read = c.execute("SELECT COUNT(*) FROM nakopitel WHERE article!='' OR total>0 OR vypolneno>0").fetchone()[0]
    c.close()
    return {"pending": len(allr), "pending_sum": sum(r["paid"] for r in allr),
            "read": read, "top": allr[:200]}


PROC_WORKERS = int(os.environ.get("PROC_WORKERS", "6"))     # параллельных чтений (API — I/O-bound, потоки ок)


def _run_process_job(jid, n):
    """Фоновая обработка: читает документы top-n непрочитанных оплаченных заявок (по сумме),
    ПАРАЛЛЕЛЬНО пулом воркеров (PROC_WORKERS). Чтение — сетевой вызов API, потоки эффективны."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        rows = queue_zayavki(limit=n)
        total = len(rows)
        if not total:
            _job(jid, "done", "Очередь пуста — всё прочитано", done=True, result={"total": 0, "done": []})
            return
        done, lock = [], threading.Lock()

        def work(r):
            try:
                res = read_zayavka_docs(r["num"], read_contract=True)
            except Exception as e:
                res = {"error": str(e)[:200]}
            row = {"num": r["num"], "object": r["object"], "paid": r["paid"],
                   "ok": bool(res.get("ok")), "article": res.get("article", ""),
                   "total": res.get("total", 0), "vypolneno": res.get("vypolneno", 0),
                   "doc_kinds": res.get("doc_kinds", ""), "error": res.get("error", "")}
            with lock:
                done.append(row)
                okn = sum(1 for d in done if d["ok"])
                _job(jid, "read", f"[{len(done)}/{total}] прочитано {okn} · последняя №{r['num']} · {r['object']}",
                     result={"i": len(done), "total": total, "done": done})
            return row

        with ThreadPoolExecutor(max_workers=max(1, min(PROC_WORKERS, total))) as ex:
            futs = [ex.submit(work, r) for r in rows]
            for _ in as_completed(futs):
                pass
        okn = sum(1 for d in done if d["ok"])
        _job(jid, "done", f"Готово: прочитано {okn} из {total} (в {min(PROC_WORKERS, total)} потоков)",
             done=True, result={"i": total, "total": total, "done": done})
    except Exception as e:
        _job(jid, "error", str(e)[:200], done=True)


def oplata_stats():
    """Заявки в стадии «Оплата» (воронка 178) — для вкладки проверки Шерлок+Баффет."""
    import precheck as PC
    stages = {sid: "" for sid in PC.PAY_STAGE_IDS} if PC.PAY_STAGE_IDS else PC.pay_stage_ids()
    if not stages:
        return {"error": "стадии «Оплата» не найдены (PAY_STAGE_NAMES/PAY_STAGE_IDS)", "count": 0, "top": []}
    items = PC.items_in_stages(list(stages.keys()))
    rows = [{"num": it["num"], "supplier": it["supplier"], "amount": it["amount"]}
            for it in items if it["num"]]
    rows.sort(key=lambda x: -(x["amount"] or 0))
    return {"count": len(items), "sum": sum(it["amount"] or 0 for it in items),
            "stages": list(stages.keys()), "top": rows[:200]}


def _run_precheck_job(jid, post, limit, read):
    """Шерлок+Баффет по заявкам «Оплаты»: считает вердикт, при post=True публикует комментарий в Bitrix."""
    import precheck as PC
    import hashlib
    try:
        PC._ensure_table()
        stages = {sid: "" for sid in PC.PAY_STAGE_IDS} if PC.PAY_STAGE_IDS else PC.pay_stage_ids()
        items = [it for it in PC.items_in_stages(list(stages.keys())) if it["num"]]
        items.sort(key=lambda it: -(it["amount"] or 0))
        total = min(len(items), limit)
        pays = PC._pays()
        done = []
        for it in items[:limit]:
            _job(jid, "read", f"[{len(done)+1}/{total}] №{it['num']} · {(it['supplier'] or '')[:24]}"
                 + (" · читаю договор…" if read else ""),
                 result={"i": len(done), "total": total, "done": done})
            try:
                v = PC.verdict(it, pays, read=read)
                text = PC.comment_text(v)
            except Exception as e:
                done.append({"num": it["num"], "supplier": it["supplier"], "amount": it["amount"],
                             "level": "warn", "sherlock": [], "remarks": ["ошибка: " + str(e)[:120]],
                             "text": "", "posted": "", "id": it["id"]})
                continue
            posted = ""
            if post:
                h = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
                ph, pcid = PC._posted(v["num"])
                if ph == h:
                    posted = "не изменился"
                else:
                    try:
                        if pcid:
                            PC.update_comment(pcid, text); cid, posted = pcid, "обновлён"
                        else:
                            cid, posted = PC.post_comment(it["id"], text), "запостен"
                        PC._mark_posted(v["num"], h, cid)
                    except Exception as e:
                        posted = "ошибка: " + str(e)[:80]
            done.append({"num": v["num"], "supplier": v["supplier"], "amount": v["amount"],
                         "level": v.get("level", "warn"), "sherlock": v["sherlock"],
                         "remarks": v["remarks"], "buffett": v.get("buffett", {}),
                         "text": text, "posted": posted, "id": it["id"]})
            _job(jid, "read", f"[{len(done)}/{total}] №{it['num']}", result={"i": len(done), "total": total, "done": done})
        reds = sum(1 for d in done if d["level"] == "red")
        _job(jid, "done", f"Готово: {len(done)} заявок · 🔴 {reds}" + (" · опубликовано в Bitrix" if post else " · без записи"),
             done=True, result={"i": total, "total": total, "done": done})
    except Exception as e:
        _job(jid, "error", str(e)[:200], done=True)


def money(n):
    return f"{n:,.0f}".replace(",", " ")


def day_payments(date):
    """Все платежи 1С за конкретную дату (YYYY-MM-DD): исходящие и входящие — для клика по дню в графике."""
    c = _db()
    rows = c.execute("""SELECT kind,company,name,bin,amount,purpose,doc,vidop FROM flow
                        WHERE substr(date,1,10)=? ORDER BY amount DESC""", (str(date),)).fetchall()
    c.close()
    out, inc = [], []
    for kind, company, name, binf, amount, purpose, doc, vidop in rows:
        rec = {"company": company or "", "name": name or "", "bin": binf or "",
               "amount": amount or 0, "purpose": purpose or "", "doc": doc or "",
               "object": _object_from(purpose or "") or "—", "num": _num_from(purpose or "")}
        (out if kind == "out" else inc).append(rec)
    return {"date": str(date), "out": out, "in": inc,
            "out_sum": sum(r["amount"] for r in out), "in_sum": sum(r["amount"] for r in inc)}


def adata_check(binf, refresh=False):
    """Карточка проверки контрагента (модули благонадёжности Adata) с кэшем в adata_sb по БИН."""
    if not binf:
        return {"error": "нет БИН — сначала прочитай договор"}
    c = _db()
    c.execute("CREATE TABLE IF NOT EXISTS adata_sb(bin TEXT PRIMARY KEY, json TEXT, ts TEXT)")
    r = None if refresh else c.execute("SELECT json,ts FROM adata_sb WHERE bin=?", (str(binf),)).fetchone()
    c.close()
    if r and r[0]:
        try:
            card = json.loads(r[0]); card["cached"] = r[1]; return card
        except Exception:
            pass
    try:
        import adata as A
        card = A.sb_card(binf)
    except Exception as e:
        return {"error": str(e)[:200]}
    c = _db()
    c.execute("INSERT OR REPLACE INTO adata_sb VALUES(?,?,?)",
              (str(binf), json.dumps(card, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M")))
    c.commit(); c.close()
    return card


def bdds_article(obj_target, canon_target):
    """Заявки/платежи, попавшие в объект×вид-работ (для провала из БДДС): № · поставщик · оплачено ·
    договор · выполнено · есть ли накопитель. Та же атрибуция, что в bdds_data."""
    c = _db()
    zrows = c.execute("SELECT number,title,bx_object,supplier,id FROM zayavka").fetchall()
    nkrows = c.execute("SELECT num,object,notes,title,article,total,vypolneno FROM nakopitel").fetchall()
    pays = c.execute("SELECT amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall()
    bx_portal = dict(c.execute("SELECT k,v FROM meta").fetchall()).get("bx_portal", "")
    c.close()
    z_obj, z_canon, z_title, z_sup, z_id = {}, {}, {}, {}, {}
    for number, title, bxo, supplier, iid in zrows:
        n = str(number or "").strip() or _num_from(title or "")
        if not n:
            continue
        z_obj[n] = _object_from(bxo or "") or _object_from(title or "") or (bxo or "").strip() or ""
        z_canon[n] = _canon_article(title or "")
        z_title[n] = title or ""; z_sup[n] = supplier or ""; z_id[n] = iid
    nk_by_num = {}
    for num, obj, notes, title, article, total, vyp in nkrows:
        n = str(num)
        canon = _canon_article(article) if article else (z_canon.get(n) or _canon_article((notes or "") + " " + (title or "")))
        o = _object_from(obj) or z_obj.get(n) or _object_from(title or "") or "—"
        nk_by_num[n] = {"object": o, "canon": canon, "total": total or 0, "vyp": vyp or 0}

    def attr(purpose):
        n = _num_from(purpose)
        info = nk_by_num.get(n)
        if info:
            return info["object"] or "—", info["canon"], n
        return (z_obj.get(n) or _object_from(purpose) or "—"), (z_canon.get(n) or _canon_article(purpose)), n

    grp = {}
    for amt, purpose in pays:
        o, cn, n = attr(purpose)
        if n and o == obj_target and cn == canon_target:
            grp.setdefault(n, 0.0)
            grp[n] += amt or 0
    for n, info in nk_by_num.items():                       # накопители того же объекта×статьи (даже без оплаты)
        if info["object"] == obj_target and info["canon"] == canon_target:
            grp.setdefault(n, 0.0)
    rows = []
    for n, paid in grp.items():
        nk = nk_by_num.get(n)
        rows.append({"num": n, "paid": paid, "supplier": z_sup.get(n, ""),
                     "title": (z_title.get(n, "") or "")[:100], "id": z_id.get(n),
                     "has_nk": bool(nk), "contracted": (nk or {}).get("total", 0),
                     "vypolneno": (nk or {}).get("vyp", 0)})
    rows.sort(key=lambda x: -x["paid"])
    return {"object": obj_target, "article": canon_target, "bx_portal": bx_portal, "bx_entity": 178, "rows": rows}


# ---------- дашборд ----------
CSS = """
*{box-sizing:border-box}body{margin:0;background:#eef2f7;color:#1e293b;font:14px -apple-system,Segoe UI,Roboto,Arial}
.top{background:#0f2233;color:#e2e8f0;padding:14px 22px}.top b{font-size:16px}.top .s{font-size:11px;color:#94a3b8}
.wrap{max-width:1180px;margin:0 auto;padding:18px}
h2{font-size:15px;margin:22px 0 10px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.kpi{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px}
.kpi .l{font-size:10.5px;color:#64748b;text-transform:uppercase;letter-spacing:.3px}
.kpi .v{font-size:19px;font-weight:800;margin-top:5px;font-variant-numeric:tabular-nums}
.kpi .v.pos{color:#0e7490}.kpi .v.negv{color:#c2410c}
.svet{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px}
.sv{border-radius:12px;padding:13px 16px;color:#fff}
.sv .n{font-size:26px;font-weight:800;line-height:1}.sv .l{font-size:12px;opacity:.95;margin-top:3px}
.sv.r{background:#dc2626}.sv.y{background:#d97706}.sv.g{background:#0e7490}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:16px}
.bars{padding:12px 16px}
.brow{margin:9px 0}
.brow .hd{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;margin-bottom:3px}
.brow .hd .co{font-weight:600;color:#1e293b}.brow .hd .vv{color:#64748b;font-variant-numeric:tabular-nums;white-space:nowrap}
.track{background:#f1f5f9;border-radius:5px;height:9px;overflow:hidden;margin:2px 0}
.fill{height:100%;border-radius:5px}.fo{background:#ea580c}.fi{background:#0891b2}
.lg{font-size:11px;color:#64748b;padding:2px 16px 10px;display:flex;gap:16px}
.lg .k{display:inline-flex;align-items:center;gap:5px}.lg .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#f1f5f9;color:#475569;text-align:left;padding:8px 10px;font-size:11px;border-bottom:1px solid #e2e8f0}
td{padding:8px 10px;border-bottom:1px solid #eef2f7}tr:last-child td{border-bottom:0}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.b{font-weight:700}.neg{color:#b91c1c;font-weight:700}
.pill{font-size:10px;padding:2px 7px;border-radius:20px;font-weight:600}.pr{background:#fee2e2;color:#991b1b}.py{background:#fef3c7;color:#92400e}
.pill.ok{background:#dcfce7;color:#166534}.pill.wait{background:#fef3c7;color:#92400e}
.note{font-size:11.5px;color:#64748b;padding:8px 16px;background:#fcfcfd;border-top:1px solid #eef2f7}
td a{color:#0e7490;font-weight:700;text-decoration:none}td a:hover{text-decoration:underline}
.svh{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-top:22px}
.svh h2{margin:0}.svmeta{font-size:11px;color:#94a3b8}
.rbtn{display:inline-block;background:#0e7490;color:#fff!important;padding:6px 13px;border-radius:8px;text-decoration:none;font-size:12px;font-weight:700}
.rbtn:hover{background:#0c5f75}
.cashln{font-size:12.5px;color:#78350f;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 13px;margin-bottom:14px}
.sv.bl{background:#2563eb}.sv.nu{background:#64748b}
.svet.wide{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.donutwrap{display:flex;gap:24px;align-items:center;flex-wrap:wrap;padding:14px 16px}
.donut .dc{font-size:15px;font-weight:800;fill:#1e293b}.donut .dcl{font-size:9px;fill:#94a3b8}
.lgrow{display:flex;flex-direction:column;gap:6px;min-width:220px;flex:1}
.lgi{display:flex;align-items:center;gap:8px;font-size:12.5px}
.lgi .sw{width:11px;height:11px;border-radius:3px;flex:none}
.lgi .lgn{flex:1;color:#334155}.lgi b{font-variant-numeric:tabular-nums}
.lgi .pc{color:#94a3b8;width:38px;text-align:right;font-variant-numeric:tabular-nums}
.chartwrap{position:relative;padding:10px 8px 6px}
.lchart{width:100%;height:260px;display:block}
.axl{font-size:10px;fill:#94a3b8;font-variant-numeric:tabular-nums}
.clg{display:flex;gap:18px;font-size:11.5px;color:#64748b;padding:0 16px 10px}
.clg .k{display:inline-flex;align-items:center;gap:5px}.clg .sw{width:14px;height:3px;border-radius:2px;display:inline-block}
.cftip{position:fixed;display:none;background:#0f172a;color:#fff;padding:8px 11px;border-radius:8px;font-size:11.5px;pointer-events:none;z-index:60;box-shadow:0 6px 20px rgba(0,0,0,.3);max-width:230px}
.cftip .tdh{margin-top:5px;color:#94a3b8;font-size:10px}.cftip .tdrv{color:#cbd5e1;font-size:10.5px}
.loading{padding:40px;text-align:center;color:#94a3b8}.err{padding:20px;color:#b91c1c}
.tabs{background:#0f2233;padding:0 12px;display:flex;gap:2px;overflow-x:auto;position:sticky;top:0;z-index:30}
.tab{background:none;border:0;color:#94a3b8;padding:12px 16px;font-size:13.5px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:#e2e8f0}.tab.on{color:#fff;border-bottom-color:#22d3ee}
.fbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 14px;background:#fff;border:1px solid #e2e8f0;border-bottom:0;border-radius:12px 12px 0 0}
.search{flex:1;min-width:200px;padding:7px 11px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px}
.fbar select{padding:7px 9px;border:1px solid #cbd5e1;border-radius:8px;font-size:12.5px;background:#fff}
.cbf{font-size:12.5px;color:#475569;display:inline-flex;align-items:center;gap:4px}
.fsum{font-size:11.5px;color:#94a3b8;margin-left:auto;font-variant-numeric:tabular-nums}
.tblscroll{max-height:70vh;overflow:auto;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 12px 12px;background:#fff}
.tblscroll thead th{position:sticky;top:0;z-index:1}
.sorth{cursor:pointer;user-select:none}.sorth:hover{background:#e2e8f0}
.tg{font-size:10px;padding:2px 7px;border-radius:5px;font-weight:600;white-space:nowrap}
.t-подряд{background:#cffafe;color:#0e7490}.t-поставка{background:#ffedd5;color:#c2410c}.t-услуга{background:#ede9fe;color:#6d28d9}.t-прочее{background:#f1f5f9;color:#64748b}
.modal-ov{position:fixed;inset:0;background:rgba(15,34,51,.55);z-index:80;display:flex;align-items:flex-start;justify-content:center;padding:32px 16px;overflow:auto}
.modal-box{position:relative;background:#fff;border-radius:14px;max-width:900px;width:100%;padding:0 0 16px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.modal-x{position:absolute;top:10px;right:12px;background:none;border:0;font-size:18px;color:#94a3b8;cursor:pointer;z-index:1}
.modal-box h4{font-size:13px;margin:16px 16px 8px}
.nkhd{background:#0f2233;color:#e2e8f0;border-radius:14px 14px 0 0;padding:16px 20px}
.nkhd .t{font-size:18px;font-weight:800}.nkhd .s{font-size:12px;color:#94a3b8;margin-top:4px}
.nkstrip{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;padding:14px 16px}
.nktile{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}
.nktile .l{font-size:9.5px;text-transform:uppercase;color:#64748b}.nktile .vv{font-size:15px;font-weight:800;margin-top:4px;font-variant-numeric:tabular-nums}
.nktile.f .vv{color:#0e7490}.nktile.r .vv{color:#c2410c}
.nksec{margin:0 16px;padding:11px 14px;background:#f8fafc;border:1px solid #eef2f7;border-radius:10px;font-size:12.5px}
.nknote{color:#64748b;margin-top:6px;font-size:12px}
.modal-box .tblscroll{margin:0 16px;border-radius:10px}
"""

# JS линейного графика cashflow (данные приходят в window.CF). Вынесено из f-строки (много {}).
CF_JS = r"""
(function(){
  var data=window.CF||[], svg=document.getElementById('cfsvg'), tip=document.getElementById('cftip');
  if(!svg) return;
  if(!data.length){ svg.innerHTML='<text x=12 y=24 class=axl>нет данных за период</text>'; return; }
  var W=svg.clientWidth||900, H=260, padL=66, padR=16, padT=14, padB=26, iw=W-padL-padR, ih=H-padT-padB;
  var maxV=1; data.forEach(function(d){ maxV=Math.max(maxV,d.out,d.in); });
  function X(i){ return padL+(data.length<2?iw/2:iw*i/(data.length-1)); }
  function Y(v){ return padT+ih-ih*v/maxV; }
  function M(n){ return (Math.round(n)||0).toLocaleString('ru-RU').replace(/,/g,' '); }
  svg.setAttribute('viewBox','0 0 '+W+' '+H);
  var p=[];
  for(var g=0;g<=4;g++){ var gy=padT+ih-ih*g/4; p.push('<line x1='+padL+' y1='+gy+' x2='+(W-padR)+' y2='+gy+' stroke=#eef2f7 />');
    p.push('<text x='+(padL-8)+' y='+(gy+4)+' text-anchor=end class=axl>'+M(maxV*g/4)+'</text>'); }
  [0,Math.floor(data.length/2),data.length-1].forEach(function(i){ if(i<0||i>=data.length)return;
    p.push('<text x='+X(i)+' y='+(H-7)+' text-anchor=middle class=axl>'+String(data[i].d).slice(5)+'</text>'); });
  function poly(k,c){ return '<polyline points="'+data.map(function(d,i){return X(i)+','+Y(d[k]);}).join(' ')+'" fill=none stroke="'+c+'" stroke-width=2 />'; }
  p.push(poly('in','#0891b2')); p.push(poly('out','#ea580c'));
  p.push('<line id=cfx y1='+padT+' y2='+(padT+ih)+' stroke=#cbd5e1 stroke-dasharray=3 style=display:none />');
  p.push('<circle id=cfdo r=4 fill=#ea580c style=display:none />');
  p.push('<circle id=cfdi r=4 fill=#0891b2 style=display:none />');
  p.push('<rect id=cfov x='+padL+' y='+padT+' width='+iw+' height='+ih+' fill=transparent />');
  svg.innerHTML=p.join('');
  var xl=svg.querySelector('#cfx'),dO=svg.querySelector('#cfdo'),dI=svg.querySelector('#cfdi'),ov=svg.querySelector('#cfov');
  ov.addEventListener('mousemove',function(e){
    var r=svg.getBoundingClientRect(), px=(e.clientX-r.left)*(W/r.width);
    var i=Math.max(0,Math.min(data.length-1,Math.round((px-padL)/(iw||1)*(data.length-1)))), d=data[i], xx=X(i);
    xl.setAttribute('x1',xx);xl.setAttribute('x2',xx);xl.style.display='';
    dO.setAttribute('cx',xx);dO.setAttribute('cy',Y(d.out));dO.style.display='';
    dI.setAttribute('cx',xx);dI.setAttribute('cy',Y(d.in));dI.style.display='';
    var s=d.in-d.out, drv=(d.top||[]).map(function(t){return '<div class=tdrv>'+t.n+' — '+M(t.a)+'</div>';}).join('');
    tip.innerHTML='<b>'+d.d+'</b><div><span style=color:#fb923c>отток</span> '+M(d.out)+' ₸</div>'+
      '<div><span style=color:#22d3ee>приток</span> '+M(d.in)+' ₸</div><div>сальдо '+(s>=0?'+':'')+M(s)+' ₸</div>'+
      (drv?'<div class=tdh>крупные платежи дня:</div>'+drv:'');
    tip.style.display='block';
    tip.style.left=Math.min(window.innerWidth-240,e.clientX+14)+'px'; tip.style.top=(e.clientY+14)+'px';
  });
  ov.addEventListener('mouseleave',function(){ xl.style.display='none';dO.style.display='none';dI.style.display='none';tip.style.display='none'; });
})();
"""


APP_JS = r'''
(function(){
var app=document.getElementById('app'), tip=document.getElementById('cftip');
function money(n){return (Math.round(n||0)).toLocaleString('ru-RU').replace(/,/g,' ');}
function el(t,c,h){var e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
var D=null, tab='obzor';
var TABS=[['obzor','Обзор'],['pay','Платежи 1С'],['sved','Сведение'],['nk','Накопитель'],['bdds','БДДС'],['proc','Обработка'],['oplata','Оплата'],['ctrl','Контроль']];

fetch('data.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){D=d;boot();})
  .catch(function(e){app.className='';app.innerHTML='<div class=wrap><div class=err>Ошибка загрузки данных: '+e+'</div></div>';});

function boot(){
  var m=D.meta||{};
  app.className='';app.innerHTML='';
  var top=el('div','top');
  top.innerHTML='<a href="/logout" style="float:right;color:#94a3b8;font-size:12px;text-decoration:none">выход ↪</a><b>◎ ATAMŪRA · Финансы</b><div class=s>срез 1С: '+esc(m.ts||'—')+' · за '+esc(m.months||'?')+' мес · Bitrix: '+esc(m.bx_sync||'—')+(m.idx_ts?(' · полный индекс: '+esc(m.idx_ts)):' · <a href="/sync-full" style="color:#7dd3fc">включить полный индекс</a>')+'</div>';
  app.appendChild(top);
  var bar=el('div','tabs');
  TABS.forEach(function(t){var b=el('button','tab'+(t[0]==tab?' on':''),t[1]);b.onclick=function(){tab=t[0];for(var i=0;i<bar.children.length;i++)bar.children[i].classList.toggle('on',TABS[i][0]==tab);render();};bar.appendChild(b);});
  app.appendChild(bar);
  var v=el('div','wrap');v.id='view';v.style.paddingBottom='48px';app.appendChild(v);
  render();
  var foot=el('div');
  foot.style.cssText='position:fixed;left:0;right:0;bottom:0;background:#0f2233;color:#94a3b8;font-size:12px;padding:7px 16px;border-top:1px solid #1e3a52;display:flex;gap:8px;flex-wrap:wrap;align-items:center;z-index:40';
  function ago(s){var d=new Date(String(s||'').replace(' ','T'));if(isNaN(d))return '';var mn=Math.round((Date.now()-d.getTime())/60000);if(mn<1)return 'только что';if(mn<60)return mn+' мин назад';var h=Math.round(mn/60);if(h<48)return h+' ч назад';return Math.round(h/24)+' дн назад';}
  function col(s,hw){var d=new Date(String(s||'').replace(' ','T'));return (isNaN(d)||(Date.now()-d.getTime())/3600000>hw)?'#fbbf24':'#34d399';}
  foot.innerHTML='<span style="color:#64748b">Обновлено:</span>'
    +'<span>1С <b style="color:'+col(m.ts,6)+'">'+esc(m.ts||'—')+'</b> <span style="color:#64748b">('+ago(m.ts)+')</span></span>'
    +'<span>· Bitrix <b style="color:'+col(m.bx_sync,12)+'">'+esc(m.bx_sync||'—')+'</b> <span style="color:#64748b">('+ago(m.bx_sync)+')</span></span>'
    +'<span style="color:#64748b">· индекс '+esc(m.idx_ts||'—')+'</span>';
  app.appendChild(foot);
}
function render(){var v=document.getElementById('view');v.innerHTML='';({obzor:rObzor,pay:rPay,sved:rSved,nk:rNk,bdds:rBdds,proc:rProcess,oplata:rOplata,ctrl:rCtrl}[tab])(v);}

function card(inner){var c=el('div','card');if(typeof inner=='string')c.innerHTML=inner;else c.appendChild(inner);return c;}
function h2(t){return el('h2',null,t);}
function kpiTile(l,val,cls){return '<div class=kpi><div class=l>'+l+'</div><div class="v '+(cls||'')+'">'+val+'</div></div>';}
function svc(cls,n,l){return '<div class="sv '+cls+'"><div class=n>'+n+'</div><div class=l>'+l+'</div></div>';}

function selectFilter(label,key,values){
  var s=el('select');s.innerHTML='<option value="">'+label+': все</option>'+values.map(function(v){return '<option value="'+esc(v)+'">'+esc(v)+'</option>';}).join('');
  return {node:s,test:function(x){return !s.value||String(x[key])===s.value;}};
}
function dataTable(rows,cols,opts){
  opts=opts||{};var state={sort:opts.sort||null,dir:opts.dir||-1,q:''};
  var wrap=el('div');var bar=el('div','fbar');
  var inp=el('input','search');inp.type='search';inp.placeholder=opts.searchPlaceholder||'поиск…';
  inp.oninput=function(){state.q=inp.value.toLowerCase();draw();};bar.appendChild(inp);
  (opts.filters||[]).forEach(function(f){bar.appendChild(f.node);f.node.addEventListener('change',draw);});
  var summary=el('span','fsum');bar.appendChild(summary);wrap.appendChild(bar);
  var sc=el('div','tblscroll');var tbl=el('table');sc.appendChild(tbl);wrap.appendChild(sc);
  function draw(){
    var r=rows.filter(function(x){return (opts.filters||[]).every(function(f){return f.test(x);});});
    if(state.q){r=r.filter(function(x){return (opts.searchText?opts.searchText(x):Object.values(x).join(' ')).toLowerCase().indexOf(state.q)>=0;});}
    if(state.sort){var k=state.sort,d=state.dir;r=r.slice().sort(function(a,b){var av=a[k],bv=b[k];if(typeof av=='number'&&typeof bv=='number')return (av-bv)*d;return String(av==null?'':av).localeCompare(String(bv==null?'':bv))*d;});}
    var lim=opts.limit||500;
    tbl.innerHTML='<thead><tr>'+cols.map(function(c){return '<th class="sorth '+(c.num?'num':'')+'" data-k="'+c.key+'">'+c.label+(state.sort==c.key?(state.dir>0?' ▲':' ▼'):'')+'</th>';}).join('')+'</tr></thead><tbody>'+
      r.slice(0,lim).map(function(x){return '<tr>'+cols.map(function(c){return '<td class="'+(c.num?'num':'')+'">'+(c.render?c.render(x):esc(x[c.key]))+'</td>';}).join('')+'</tr>';}).join('')+'</tbody>';
    summary.textContent='Показано '+Math.min(lim,r.length)+' из '+r.length+(opts.sumKey?(' · Σ '+money(r.reduce(function(s,x){return s+(x[opts.sumKey]||0);},0))+' ₸'):'');
    tbl.querySelectorAll('.sorth').forEach(function(th){th.onclick=function(){var k=th.dataset.k;if(state.sort==k)state.dir=-state.dir;else{state.sort=k;state.dir=-1;}draw();};});
    if(opts.onRow){var shown=r.slice(0,lim);tbl.querySelectorAll('tbody tr').forEach(function(tr,i){tr.style.cursor='pointer';tr.onclick=function(){opts.onRow(shown[i]);};});}
  }
  draw();return wrap;
}

function openModal(inner){
  var ov=el('div','modal-ov');ov.onclick=function(e){if(e.target===ov)document.body.removeChild(ov);};
  var box=el('div','modal-box');box.innerHTML='<button class=modal-x>✕</button>'+inner;
  box.querySelector('.modal-x').onclick=function(){document.body.removeChild(ov);};
  ov.appendChild(box);document.body.appendChild(ov);
}

function openNkCard(x, amap, portal, ent){
  var ad=(amap&&amap[x.bin])||{};
  var bxlink=(portal&&x.id)?('<a href="'+portal+'/crm/type/'+ent+'/details/'+x.id+'/" target=_blank>открыть в Bitrix →</a>'):'';
  function tile(l,vv,cls){return '<div class="nktile '+(cls||'')+'"><div class=l>'+l+'</div><div class=vv>'+vv+'</div></div>';}
  var strip=tile('Договор',money(x.total)+' ₸')+tile('Аванс',money(x.avans)+' ₸')+
    tile('Удержание '+(x.retention_pct||0)+'%',money(x.retention)+' ₸')+
    tile('Бартер',x.barter?(x.barter_sum?money(x.barter_sum)+' ₸':'да'):'нет')+
    tile('Оплачено 1С',money(x.fact)+' ₸','f')+tile('Остаток',money(x.ostatok)+' ₸','r');
  var pr=(x.payments||[]).slice().sort(function(a,b){return (a.date||'').localeCompare(b.date||'');})
    .map(function(p){return '<tr><td>'+esc(p.date||'')+'</td><td>'+esc(p.object||'—')+'</td><td>'+esc(p.account||'—')+'</td><td>'+esc((p.purpose||'').slice(0,60))+'</td><td class=num>'+money(p.amount)+'</td></tr>';}).join('')
    || '<tr><td colspan=5 style=color:#94a3b8>нет оплат 1С по этой заявке</td></tr>';
  var html='<div class=nkhd><div class=t>'+esc(x.supplier||ad.short||'—')+'</div>'
    +'<div class=s>БИН '+esc(x.bin||'—')+(ad.iin?(' · ИИН '+esc(ad.iin)):'')+(ad.director?(' · '+esc(ad.director)):'')+(ad.oked?(' · '+esc(ad.oked)):'')+'</div></div>'
    +'<div class=nkstrip>'+strip+'</div>'
    +'<div class=nksec><b>Заявка №'+esc(x.num)+' · Договор '+esc(x.contract_no||'—')+'</b> '+esc(x.date||'')+' · '+esc(x.object||'')+(x.ochered?(' · '+esc(x.ochered)):'')+' &nbsp; '+bxlink
    +(x.notes?('<div class=nknote>📄 '+esc(x.notes)+'</div>'):'')+'</div>'
    +'<h4>Оплаты 1С по заявке</h4><div class=tblscroll style="max-height:30vh"><table><thead><tr><th>Дата</th><th>Объект</th><th>Счёт</th><th>Назначение</th><th class=num>Сумма</th></tr></thead><tbody>'+pr+'</tbody></table></div>'
    +'<h4>Проверка контрагента (Adata)</h4>'
    +'<button class=adchk style="padding:8px 16px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;font-weight:600;cursor:pointer">🛡️ Проверить контрагента</button>'
    +'<div class=note style="margin-top:6px">Проверим по БИН: аресты счетов/имущества, фиктивные сделки, налоговую задолженность, судебные дела, розыск/терроризм по руководителю, лицензии, режим налогообложения, санкционные списки.</div>'
    +'<div id=adatares style="margin-top:8px"></div>';
  html+='<h4>💬 Комментарии сотрудников (Bitrix)</h4><div id=nkcomments class=note>загрузка…</div>';
  openModal(html);
  var achk=document.querySelector('.adchk');
  if(achk)achk.onclick=function(){
    var host=document.getElementById('adatares');
    if(!x.bin){host.innerHTML='<div class=err>Нет БИН — сначала прочитай договор заявки</div>';return;}
    achk.disabled=true;achk.textContent='Проверяю… (несколько секунд)';
    fetch('adata-check?bin='+encodeURIComponent(x.bin),{cache:'no-store'}).then(function(r){return r.json();}).then(function(c){
      achk.disabled=false;achk.textContent='🛡️ Обновить проверку';
      if(c.error){host.innerHTML='<div class=err>'+esc(c.error)+'</div>';return;}
      var flags=c.flags||[],bad=flags.filter(function(f){return f.bad;});
      var head='<div class=note>Лицензий: '+(c.licenses||'—')+' · Режим налогов: '+esc(c.tax_mode||'—')+' · Суды гр/уг/адм: '+((c.courts||{}).civil||0)+'/'+((c.courts||{}).criminal||0)+'/'+((c.courts||{}).admin||0)+(c.cached?(' · кэш '+esc(c.cached)):'')+'</div>';
      var verd=bad.length?('<div style="color:#b91c1c;font-weight:700;margin:6px 0">⚠ Найдено проблем: '+bad.length+' — проверить перед оплатой</div>'):'<div style="color:#15803d;font-weight:700;margin:6px 0">✓ Проблем не найдено — контрагент чист по всем спискам</div>';
      var legend='<div class=note style="margin:2px 0 8px">Каждая строка — проверка по реестру. <b style="color:#15803d">«чисто»</b> = контрагента НЕТ в этом списке (хорошо). <b style="color:#b91c1c">«ЕСТЬ»</b> = найден в списке (риск).</div>';
      // сначала проблемы, потом чистые
      var ordered=flags.slice().sort(function(a,b){return (b.bad?1:0)-(a.bad?1:0);});
      var rows=ordered.map(function(f){
        var st=f.bad?('<b style="color:#b91c1c">⚠ ЕСТЬ'+(f.extra?(' · '+esc(f.extra)):'')+'</b>'):'<span style="color:#15803d">чисто</span>';
        return '<div style="display:flex;justify-content:space-between;gap:14px;padding:5px 0;border-bottom:1px solid #eef2f7">'
          +'<span>'+(f.bad?'🔴':'🟢')+' '+esc(f.name)+'</span><span style="white-space:nowrap">'+st+'</span></div>';
      }).join('');
      host.innerHTML=head+verd+legend+rows;
    }).catch(function(e){achk.disabled=false;achk.textContent='🛡️ Проверить контрагента';host.innerHTML='<div class=err>ошибка: '+e+'</div>';});
  };
  if(x.id){
    fetch('nk-comments?id='+encodeURIComponent(x.id)+'&num='+encodeURIComponent(x.num||''),{cache:'no-store'})
      .then(function(r){return r.json();}).then(function(d){
        var host=document.getElementById('nkcomments');if(!host)return;
        var cs=(d&&d.comments)||[];
        if(!cs.length){host.innerHTML='<span style=color:#94a3b8>заметок в карточке нет</span>';return;}
        host.className='';
        host.innerHTML=cs.map(function(c){return '<div style="border-left:2px solid #334155;padding:3px 0 3px 10px;margin:6px 0"><div style="color:#94a3b8;font-size:11px">'+esc(c.created||'')+' · автор '+esc(c.author||'')+'</div><div>'+esc(c.text||'')+'</div></div>';}).join('');
      }).catch(function(e){var h=document.getElementById('nkcomments');if(h)h.innerHTML='<span class=err>ошибка загрузки комментариев</span>';});
  }
}

// ---- накопитель: общий флоу чтения (форма во вкладке + кнопка в «Сведении») ----
var NK_STEPS=[['queued','Очередь'],['bitrix','Заявка'],['download','Вложения'],['read','ИИ читает'],['save','Сохранение'],['adata','Adata'],['done','Готово']];
function nkStepsHTML(stage,text){
  var cur=0;for(var i=0;i<NK_STEPS.length;i++)if(NK_STEPS[i][0]===stage)cur=i;
  var chips=NK_STEPS.map(function(st,i){
    var done=(stage==='done')||i<cur,now=(i===cur&&stage!=='done');
    var c=done?'#34d399':(now?'#38bdf8':'#475569'),mk=done?'✓':(now?'●':'○');
    return '<span style="color:'+c+';margin-right:10px;white-space:nowrap">'+mk+' '+st[1]+'</span>';
  }).join('');
  return '<div style="color:#e2e8f0;margin-bottom:4px">'+esc(text||'')+'</div><div style="font-size:12px">'+chips+'</div>';
}
function nkRead(num,bin,onStage,onDone){
  onStage('queued','Запускаю чтение…');
  fetch('nk-read?num='+encodeURIComponent(num)+(bin?('&bin='+encodeURIComponent(bin)):''),{cache:'no-store'})
    .then(function(r){return r.json();}).then(function(j){
      if(j.error||!j.job){onDone({error:j.error||'не удалось запустить задачу'});return;}
      var poll=setInterval(function(){
        fetch('nk-status?id='+encodeURIComponent(j.job),{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
          if(!s.done){onStage(s.stage,s.msg);return;}
          clearInterval(poll);var res=s.result||{},t=res.terms||{};
          if(s.stage==='error'||res.error||t.error){onDone({error:s.msg||res.error||t.error||'ошибка'});return;}
          onDone({ok:true,num:res.num,terms:t,adata:res.adata});
        }).catch(function(e){clearInterval(poll);onDone({error:''+e});});
      },1200);
    }).catch(function(e){onDone({error:''+e});});
}
function openNkByNum(num){
  fetch('nakopitel.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(nd){
    var row=(nd.rows||[]).filter(function(x){return String(x.num)===String(num);})[0];
    if(row)openNkCard(row,nd.adata,nd.bx_portal,nd.bx_entity);
    else openModal('<div class=note>Накопитель по №'+esc(num)+' пока не найден.</div>');
  });
}
function nkCreateFlow(z,btn,nkSet){
  openModal('<div class=nkhd><div class=t>Создаю накопитель · заявка №'+esc(z.num)+'</div><div class=s>'+esc(z.supplier||z.name||'')+'</div></div><div id=nkprog style="margin-top:12px"><div class=note>Запускаю…</div></div>');
  var host=document.getElementById('nkprog');btn.disabled=true;
  nkRead(z.num,'',function(stage,text){host.innerHTML=nkStepsHTML(stage,text);},function(res){
    btn.disabled=false;
    if(res.error){host.innerHTML='<div class=err style="margin-top:6px">✕ '+esc(res.error)+'</div>';return;}
    if(nkSet)nkSet[String(z.num)]=1;
    btn.textContent='Накопитель ✓';btn.dataset.has='1';btn.style.background='#134e4a';btn.style.color='#5eead4';
    host.innerHTML=nkStepsHTML('done','Готово')+'<div style="color:#34d399;margin:8px 0">✓ '+esc(res.terms.contract_no||'договор')+' · '+money(res.terms.total||0)+' ₸ сохранён'+(res.adata?' (+ справка Adata)':'')+'</div><button class=nkopenbtn style="padding:7px 14px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;cursor:pointer;font-weight:600">Открыть карточку заявки →</button>';
    host.querySelector('.nkopenbtn').onclick=function(){openNkByNum(z.num);};
  });
}

function donutSVG(pairs,colors){
  var size=180,r=62,w=24,cx=size/2,cy=size/2,tot=0,i;
  for(i=0;i<pairs.length;i++){if(pairs[i][1]>0)tot+=pairs[i][1];}
  tot=tot||1;var a0=-Math.PI/2,segs='';
  pairs.forEach(function(p){if(p[1]<=0)return;var fr=p[1]/tot,a1=a0+fr*2*Math.PI;
    var x0=cx+r*Math.cos(a0),y0=cy+r*Math.sin(a0),x1=cx+r*Math.cos(a1),y1=cy+r*Math.sin(a1),lg=fr>0.5?1:0;
    segs+='<path d="M '+x0.toFixed(2)+' '+y0.toFixed(2)+' A '+r+' '+r+' 0 '+lg+' 1 '+x1.toFixed(2)+' '+y1.toFixed(2)+'" fill=none stroke="'+(colors[p[0]]||'#94a3b8')+'" stroke-width='+w+'><title>'+esc(p[0])+': '+money(p[1])+' ₸ · '+Math.round(fr*100)+'%</title></path>';
    a0=a1;});
  return '<svg viewBox="0 0 '+size+' '+size+'" width='+size+' height='+size+' class=donut>'+segs+'<text x='+cx+' y='+(cy-2)+' text-anchor=middle class=dc>'+money(tot)+'</text><text x='+cx+' y='+(cy+14)+' text-anchor=middle class=dcl>₸</text></svg>';
}
function legend(pairs,colors){var tot=0;pairs.forEach(function(p){tot+=p[1];});tot=tot||1;
  return '<div class=lgrow>'+pairs.map(function(p){return '<div class=lgi><span class=sw style="background:'+(colors[p[0]]||'#94a3b8')+'"></span><span class=lgn>'+esc(p[0])+'</span><b>'+money(p[1])+'</b><span class=pc>'+Math.round(p[1]/tot*100)+'%</span></div>';}).join('')+'</div>';
}
function openDay(date){
  openModal('<div class=nkhd><div class=t>Платежи за '+esc(date)+'</div><div class=s>исходящие и входящие 1С за сутки</div></div><div id=dayb class=note>загрузка…</div>');
  fetch('day.json?date='+encodeURIComponent(date),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    var host=document.getElementById('dayb');if(!host)return;host.className='';
    function tbl(rows,color){
      if(!rows.length)return '<div class=note>нет платежей</div>';
      return '<div class=tblscroll style="max-height:34vh"><table><thead><tr><th>Компания</th><th>Контрагент</th><th>Объект</th><th>№</th><th>Назначение</th><th class=num>Сумма</th></tr></thead><tbody>'
        +rows.map(function(p){return '<tr'+(p.num?' style="cursor:pointer" data-num="'+esc(p.num)+'"':'')+'><td>'+esc(p.company||'—')+'</td><td>'+esc(p.name||'—')+'</td><td>'+esc(p.object||'—')+'</td><td>'+(p.num?('<b style=color:#0ea5e9>'+esc(p.num)+'</b>'):'—')+'</td><td style=color:#64748b>'+esc((p.purpose||'').slice(0,70))+'</td><td class=num style=color:'+color+'>'+money(p.amount)+'</td></tr>';}).join('')
        +'</tbody></table></div>';
    }
    host.innerHTML='<div class=nkstrip>'
      +'<div class="nktile"><div class=l>Исходящих</div><div class=vv>'+d.out.length+' · '+money(d.out_sum)+' ₸</div></div>'
      +'<div class="nktile f"><div class=l>Входящих</div><div class=vv>'+d.in.length+' · '+money(d.in_sum)+' ₸</div></div>'
      +'<div class="nktile r"><div class=l>Сальдо дня</div><div class=vv>'+((d.in_sum-d.out_sum)>=0?'+':'')+money(d.in_sum-d.out_sum)+' ₸</div></div></div>'
      +'<h4 style="color:#c2410c">↑ Исходящие ('+d.out.length+')</h4>'+tbl(d.out,'#b91c1c')
      +'<h4 style="color:#0e7490">↓ Входящие ('+d.in.length+')</h4>'+tbl(d.in,'#0e7490');
    host.querySelectorAll('tr[data-num]').forEach(function(tr){tr.onclick=function(){openZayavka(tr.getAttribute('data-num'));};});
  }).catch(function(e){var h=document.getElementById('dayb');if(h)h.innerHTML='<div class=err>ошибка: '+e+'</div>';});
}
function lineChart(host,data){
  if(!data||!data.length){host.innerHTML='<div class=note>нет данных за период</div>';return;}
  var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','lchart');host.appendChild(svg);
  var W=host.clientWidth||900,H=250,padL=76,padR=16,padT=14,padB=26,iw=W-padL-padR,ih=H-padT-padB;
  data.forEach(function(d){d._s=d.in-d.out;});
  var maxV=1,minV=0;
  data.forEach(function(d){maxV=Math.max(maxV,d.out,d.in,d._s);minV=Math.min(minV,d._s);});
  var rng=(maxV-minV)||1;
  function X(i){return padL+(data.length<2?iw/2:iw*i/(data.length-1));}
  function Y(v){return padT+ih-ih*(v-minV)/rng;}
  var p=[],g;
  for(g=0;g<=4;g++){var gv=minV+rng*g/4,gy=Y(gv);p.push('<line x1='+padL+' y1='+gy+' x2='+(W-padR)+' y2='+gy+' stroke=#eef2f7 />');p.push('<text x='+(padL-8)+' y='+(gy+4)+' text-anchor=end class=axl>'+money(gv)+'</text>');}
  if(minV<0){var zy=Y(0);p.push('<line x1='+padL+' y1='+zy+' x2='+(W-padR)+' y2='+zy+' stroke=#94a3b8 />');}
  [0,Math.floor(data.length/2),data.length-1].forEach(function(i){if(i<0||i>=data.length)return;p.push('<text x='+X(i)+' y='+(H-7)+' text-anchor=middle class=axl>'+String(data[i].d).slice(5)+'</text>');});
  function poly(k,c){return '<polyline points="'+data.map(function(d,i){return X(i)+','+Y(d[k]);}).join(' ')+'" fill=none stroke="'+c+'" stroke-width=2 />';}
  p.push(poly('_s','#6d28d9'));p.push(poly('in','#0891b2'));p.push(poly('out','#ea580c'));
  p.push('<line id=cfx y1='+padT+' y2='+(padT+ih)+' stroke=#cbd5e1 stroke-dasharray=3 style=display:none />');
  p.push('<circle id=cfdo r=4 fill=#ea580c style=display:none /><circle id=cfdi r=4 fill=#0891b2 style=display:none /><circle id=cfds r=4 fill=#6d28d9 style=display:none />');
  p.push('<rect id=cfov x='+padL+' y='+padT+' width='+iw+' height='+ih+' fill=transparent />');
  svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.innerHTML=p.join('');
  var xl=svg.querySelector('#cfx'),dO=svg.querySelector('#cfdo'),dI=svg.querySelector('#cfdi'),dS=svg.querySelector('#cfds'),ov=svg.querySelector('#cfov');
  ov.addEventListener('mousemove',function(e){
    var rc=svg.getBoundingClientRect(),px=(e.clientX-rc.left)*(W/rc.width);
    var i=Math.max(0,Math.min(data.length-1,Math.round((px-padL)/(iw||1)*(data.length-1)))),d=data[i],xx=X(i);
    xl.setAttribute('x1',xx);xl.setAttribute('x2',xx);xl.style.display='';
    dO.setAttribute('cx',xx);dO.setAttribute('cy',Y(d.out));dO.style.display='';
    dI.setAttribute('cx',xx);dI.setAttribute('cy',Y(d.in));dI.style.display='';
    dS.setAttribute('cx',xx);dS.setAttribute('cy',Y(d._s));dS.style.display='';
    var s=d._s,drv=(d.top||[]).map(function(t){return '<div class=tdrv>'+esc(t.n)+' — '+money(t.a)+'</div>';}).join('');
    tip.innerHTML='<b>'+d.d+'</b><div><span style=color:#fb923c>отток</span> '+money(d.out)+' ₸</div><div><span style=color:#22d3ee>приток</span> '+money(d.in)+' ₸</div><div><span style=color:#a78bfa>сальдо</span> '+(s>=0?'+':'')+money(s)+' ₸</div>'+(drv?'<div class=tdh>крупные платежи дня:</div>'+drv:'');
    tip.style.display='block';tip.style.left=Math.min(window.innerWidth-240,e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';
  });
  ov.addEventListener('mouseleave',function(){xl.style.display='none';dO.style.display='none';dI.style.display='none';dS.style.display='none';tip.style.display='none';});
  ov.style.cursor='pointer';
  ov.addEventListener('click',function(e){
    var rc=svg.getBoundingClientRect(),px=(e.clientX-rc.left)*(W/rc.width);
    var i=Math.max(0,Math.min(data.length-1,Math.round((px-padL)/(iw||1)*(data.length-1))));
    if(data[i])openDay(data[i].d);
  });
}

function rObzor(v){
  var maxD=(D.series&&D.series.length)?D.series[D.series.length-1].d:'';
  function cut(days){ if(!days||!maxD)return ''; var d=new Date(maxD+'T12:00:00'); d.setDate(d.getDate()-days);
    var m=d.getMonth()+1, day=d.getDate(); return d.getFullYear()+'-'+(m<10?'0':'')+m+'-'+(day<10?'0':'')+day; }
  var PRESETS=[['1 месяц',30],['2 месяца',60],['3 месяца (всё)',null]];
  var bar=el('div');bar.style.cssText='display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:12px';
  var lbl=el('span',null,'Период:');lbl.style.cssText='color:#94a3b8;font-size:13px;margin-right:4px';bar.appendChild(lbl);
  var body=el('div');
  function draw(days){
    var c0=cut(days),c=D.counts;
    var fs=D.series.filter(function(s){return !c0||s.d>=c0;});
    var fp=D.payments.filter(function(p){return !c0||(p.date||'')>=c0;});
    var kOut=fs.reduce(function(s,x){return s+x.out;},0),kIn=fs.reduce(function(s,x){return s+x.in;},0),
        kSup=fp.reduce(function(s,p){return s+p.amount;},0),saldo=kIn-kOut;
    body.innerHTML='';
    var kp=el('div','kpis');
    kp.innerHTML=kpiTile('Отток (исходящие)',money(kOut)+' ₸')+kpiTile('Приток (входящие)',money(kIn)+' ₸')+
      kpiTile('Сальдо',(saldo>=0?'+':'')+money(saldo)+' ₸',saldo>=0?'pos':'negv')+kpiTile('Оплаты поставщикам',money(kSup)+' ₸');
    body.appendChild(kp);
    body.appendChild(h2('Движение денег по дням (отток / приток)'));
    var ch=card('');var host=el('div','chartwrap');ch.appendChild(host);
    ch.appendChild(el('div','clg','<span class=k><span class=sw style="background:#ea580c"></span>отток</span><span class=k><span class=sw style="background:#0891b2"></span>приток</span><span class=k><span class=sw style="background:#6d28d9"></span>сальдо (день)</span><span style=color:#94a3b8>наведи — суммы дня · <b style=color:#0ea5e9>клик — все платежи за сутки</b></span>'));
    body.appendChild(ch);lineChart(host,fs);
    body.appendChild(h2('Сведение с Bitrix'));
    var sv=el('div','svet wide');
    sv.innerHTML=svc('g',c.matched,'Оплачено (есть платёж 1С)')+svc('y',c.reserve,'Одобрено, ждёт 1С')+svc('bl',c.in_progress,'В работе')+svc('nu',c.rejected,'Отказано')+svc('r',c.nz_orphan,'Оплата без заявки (нигде нет)');
    body.appendChild(sv);
    var byco={},bycat={};
    fp.forEach(function(p){byco[p.company]=(byco[p.company]||0)+p.amount;bycat[p.cat]=(bycat[p.cat]||0)+p.amount;});
    var comp=Object.keys(byco).map(function(k){return [k,byco[k]];}).sort(function(a,b){return b[1]-a[1];});
    var top=comp.slice(0,7),rest=comp.slice(7).reduce(function(s,x){return s+x[1];},0);if(rest>0)top.push(['прочее',rest]);
    var pal=['#0e7490','#c2410c','#7c3aed','#0891b2','#b45309','#4d7c0f','#be123c'],ccol={};top.forEach(function(x,i){ccol[x[0]]=pal[i]||'#cbd5e1';});ccol['прочее']='#cbd5e1';
    var catcol={'подряд':'#0e7490','поставка':'#c2410c','услуга':'#7c3aed','прочее':'#94a3b8'};
    var catp=['подряд','поставка','услуга','прочее'].filter(function(k){return bycat[k]>0;}).map(function(k){return [k,bycat[k]];});
    body.appendChild(h2('Отток по дочкам'));
    body.appendChild(card('<div class=donutwrap>'+donutSVG(top,ccol)+legend(top,ccol)+'</div>'));
    body.appendChild(h2('Разрез по типу расхода'));
    body.appendChild(card('<div class=donutwrap>'+donutSVG(catp,catcol)+legend(catp,catcol)+'</div>'));
  }
  PRESETS.forEach(function(pr){
    var b=el('button',null,pr[0]);
    b.style.cssText='background:#0f2233;border:1px solid #334155;color:#cbd5e1;padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px';
    b.onclick=function(){
      Array.prototype.forEach.call(bar.querySelectorAll('button'),function(x){x.style.background='#0f2233';x.style.color='#cbd5e1';});
      b.style.background='#0ea5e9';b.style.color='#fff';draw(pr[1]);
    };
    bar.appendChild(b);
  });
  v.appendChild(bar);v.appendChild(body);
  var btns=bar.querySelectorAll('button');if(btns.length){btns[btns.length-1].style.background='#0ea5e9';btns[btns.length-1].style.color='#fff';}
  draw(null);
}

function rPay(v){
  var companies=Object.keys(D.payments.reduce(function(a,p){a[p.company]=1;return a;},{})).sort();
  var fCo=selectFilter('Компания','company',companies);
  var objs=Object.keys(D.payments.reduce(function(a,p){if(p.obj)a[p.obj]=1;return a;},{})).sort();
  var fObj=selectFilter('Объект','obj',objs);
  var fCat=selectFilter('Тип','cat',['подряд','поставка','услуга','прочее']);
  var cb=el('label','cbf','<input type=checkbox> только наличные');var chk=cb.querySelector('input');
  var fCash={node:cb,test:function(x){return !chk.checked||x.cash;}};chk.addEventListener('change',function(){});
  var cols=[
    {key:'date',label:'Дата'},
    {key:'company',label:'Компания'},
    {key:'name',label:'Поставщик'},
    {key:'num',label:'№',render:function(x){return x.num?('№'+x.num):'<span style=color:#cbd5e1>—</span>';}},
    {key:'obj',label:'Объект',render:function(x){return x.obj||'<span style=color:#cbd5e1>—</span>';}},
    {key:'cat',label:'Тип',render:function(x){return '<span class="tg t-'+x.cat+'">'+x.cat+'</span>';}},
    {key:'purpose',label:'Назначение',render:function(x){return '<span title="'+esc(x.purpose)+'">'+esc((x.purpose||'').slice(0,52))+'</span>';}},
    {key:'amount',label:'Сумма',num:true,render:function(x){return money(x.amount)+(x.cash?' 💵':'');}}
  ];
  v.appendChild(h2('Все платежи 1С'));
  v.appendChild(card(dataTable(D.payments,cols,{filters:[fCo,fObj,fCat,fCash],sort:'amount',dir:-1,sumKey:'amount',
    searchText:function(x){return x.name+' '+x.purpose+' '+x.num+' '+x.company;},searchPlaceholder:'поиск: поставщик, назначение, №…',limit:1500})));
}

function rSved(v){
  var stmap={success:'Успешно',fail:'Отказано',progress:'в работе'};
  D.zayavki.forEach(function(z){z._st=z.paid?'оплачено':(z.stage=='fail'?'отказано':'ждёт 1С');});
  var bx=D.bx_portal,ent=D.bx_entity;
  v.appendChild(h2('Заявки Bitrix ↔ платежи 1С'));
  v.appendChild(el('div','note','Кнопка «Создать» читает договор заявки через Claude API и строит накопитель. После — «Накопитель ✓» открывает карточку (провал в заявку).'));
  var zhost=el('div');zhost.innerHTML='<div class=note>Загрузка…</div>';v.appendChild(zhost);
  var c=D.counts;
  D.orphans.forEach(function(o){o._r=o.reestr?'в реестре':'нигде нет';});
  var fR=selectFilter('Реестр','_r',['нигде нет','в реестре']);
  var ocols=[
    {key:'num',label:'№',render:function(o){return '№'+o.num;}},
    {key:'company',label:'Компания'},{key:'name',label:'Поставщик'},
    {key:'amount',label:'Сумма',num:true,render:function(o){return money(o.amount);}},
    {key:'date',label:'Дата'},
    {key:'_r',label:'Реестр финотдела',render:function(o){return o.reestr?('<span class="pill ok">реестр: '+o.reestr.by+' №'+o.reestr.num+'</span>'):'<span class="pill pr">нигде нет</span>';}},
    {key:'cand',label:'Заявка по поставщику',render:function(o){return o.cand?('№'+o.cand):'<span style=color:#cbd5e1>—</span>';}}
  ];
  v.appendChild(h2('🔴 Оплата без заявки ('+c.nz+')'));
  v.appendChild(el('div','cashln','Из '+c.nz+' платежей без заявки Bitrix: <b>'+c.nz_in_reestr+'</b> нашлись в реестре финотдела, <b>'+c.nz_orphan+'</b> — нигде нет (реальные кандидаты на проверку).'));
  v.appendChild(card(dataTable(D.orphans,ocols,{filters:[fR],sort:'amount',dir:-1,sumKey:'amount',
    searchText:function(o){return o.num+' '+o.name+' '+o.company;},searchPlaceholder:'поиск: №, поставщик, компания…',limit:1500})));
  fetch('nakopitel.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(nd){
    var nkSet={};((nd&&nd.rows)||[]).forEach(function(x){nkSet[String(x.num)]=1;});
    var fSt=selectFilter('Статус','_st',['оплачено','ждёт 1С','отказано']);
    var zcols=[
      {key:'num',label:'Заявка',render:function(z){return (bx&&z.id)?('<a href="'+bx+'/crm/type/'+ent+'/details/'+z.id+'/" target=_blank>№'+z.num+'</a>'):('№'+z.num);}},
      {key:'company',label:'Компания'},{key:'supplier',label:'Поставщик'},
      {key:'amount',label:'Сумма',num:true,render:function(z){return money(z.amount);}},
      {key:'_st',label:'Статус',render:function(z){return (z.paid?'<span class="pill ok">✅ оплачено</span>':'<span class="pill wait">⏳ ждёт</span>')+' <span class="pill '+(z.stage=='fail'?'pr':(z.stage=='success'?'ok':'py'))+'">'+(stmap[z.stage]||z.stage)+'</span>';}},
      {key:'_nk',label:'Накопитель',render:function(z){
        if(!z.num)return '<span style=color:#cbd5e1>—</span>';
        var has=nkSet[String(z.num)]?1:0;
        return '<button class=nkbtn2 data-num="'+esc(z.num)+'" data-has="'+has+'" style="padding:4px 10px;border:0;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;'+(has?'background:#134e4a;color:#5eead4':'background:#0ea5e9;color:#fff')+'">'+(has?'Накопитель ✓':'Создать')+'</button>';
      }}
    ];
    var ztbl=dataTable(D.zayavki,zcols,{filters:[fSt],sort:'amount',dir:-1,
      searchText:function(z){return z.num+' '+z.company+' '+z.supplier;},searchPlaceholder:'поиск: №, компания, поставщик…',limit:1500});
    ztbl.addEventListener('click',function(e){
      var b=e.target.closest('button.nkbtn2');if(!b)return;e.stopPropagation();
      var num=b.getAttribute('data-num');
      if(b.dataset.has==='1'){openNkByNum(num);return;}
      var z=D.zayavki.filter(function(x){return String(x.num)===String(num);})[0]||{num:num};
      nkCreateFlow(z,b,nkSet);
    });
    zhost.innerHTML='';zhost.appendChild(card(ztbl));
  }).catch(function(e){zhost.innerHTML='<div class=err>Ошибка загрузки заявок: '+e+'</div>';});
}

function rNk(v){
  v.appendChild(h2('Накопитель — обязательства по договорам'));
  var form=el('div','nkform');
  form.style.cssText='margin:2px 0 14px;display:flex;align-items:center;flex-wrap:wrap;gap:6px';
  form.innerHTML='<input class=nkin placeholder="№ заявки, напр. 15871" style="padding:7px 10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;border-radius:8px;width:200px">'
    +'<input class=nkin2 placeholder="БИН (необяз.)" style="padding:7px 10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;border-radius:8px;width:150px">'
    +'<button class=nkbtn style="padding:7px 14px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;cursor:pointer;font-weight:600">Создать накопитель</button>'
    +'<div class=nkmsg style="font-size:13px;color:#94a3b8;flex-basis:100%"></div>';
  v.appendChild(form);
  var host=el('div');host.innerHTML='<div class=note>Загрузка накопителя…</div>';v.appendChild(host);
  function load(){
  host.innerHTML='<div class=note>Загрузка накопителя…</div>';
  fetch('nakopitel.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(nd){
    host.innerHTML='';
    var rows=(nd&&nd.rows)||[];
    if(!rows.length){host.innerHTML='<div class=cashln>Договоры ещё не прочитаны. Введи <b>№ заявки</b> выше и нажми «Создать накопитель» — бот прочитает договор из Bitrix через Claude API.</div>';return;}
    var sT=0,sF=0,sO=0,sR=0,nb=0;
    rows.forEach(function(x){sT+=x.total;sF+=x.fact;sO+=x.ostatok;sR+=x.retention;if(x.barter)nb++;});
    var strip=el('div','svet wide');
    function tile(cls,val,lbl){return '<div class="sv '+cls+'"><div class=n style="font-size:17px">'+val+'</div><div class=l>'+lbl+'</div></div>';}
    strip.innerHTML=tile('g',rows.length,'Договоров прочитано')+tile('bl',money(sT)+' ₸','Сумма договоров')+
      tile('g',money(sF)+' ₸','Оплачено (1С)')+tile('y',money(sR)+' ₸','Гар. удержание')+
      tile('r',money(sO)+' ₸','Остаток')+tile('nu',nb,'С бартером');
    host.appendChild(strip);
    var objs=Object.keys(rows.reduce(function(a,x){if(x.object)a[x.object]=1;return a;},{})).sort();
    var fObj=selectFilter('Объект','object',objs);
    var cb=el('label','cbf','<input type=checkbox> только бартер');var chk=cb.querySelector('input');
    var fB={node:cb,test:function(x){return !chk.checked||x.barter;}};
    var cols=[
      {key:'num',label:'Заявка',render:function(x){return '№'+x.num;}},
      {key:'contract_no',label:'Договор',render:function(x){return esc(x.contract_no||'—');}},
      {key:'supplier',label:'Поставщик',render:function(x){return esc(x.supplier||'—');}},
      {key:'object',label:'Объект',render:function(x){return esc(x.object||'—');}},
      {key:'total',label:'Договор',num:true,render:function(x){return money(x.total);}},
      {key:'fact',label:'Оплачено',num:true,render:function(x){return money(x.fact);}},
      {key:'retention',label:'Удерж.',num:true,render:function(x){return x.retention?money(x.retention):'—';}},
      {key:'barter',label:'Бартер',render:function(x){return x.barter?'<span class="pill py">бартер</span>':'—';}},
      {key:'ostatok',label:'Остаток',num:true,render:function(x){return '<b>'+money(x.ostatok)+'</b>';}}
    ];
    host.appendChild(card(dataTable(rows,cols,{filters:[fObj,fB],sort:'total',dir:-1,
      onRow:function(x){openNkCard(x, nd.adata, nd.bx_portal, nd.bx_entity);},
      searchText:function(x){return x.num+' '+x.supplier+' '+(x.contract_no||'')+' '+(x.object||'')+' '+(x.notes||'');},
      searchPlaceholder:'поиск: №, поставщик, договор, объект…',limit:1000})));
    host.appendChild(el('div','note','👆 Клик по строке — полная карточка заявки (договор + оплаты 1С + Adata). Остаток = Договор − Аванс − Удержание − Бартер − Оплачено(1С).'));
  }).catch(function(e){host.innerHTML='<div class=err>Ошибка загрузки накопителя: '+e+'</div>';});
  }
  var inp=form.querySelector('.nkin'),inp2=form.querySelector('.nkin2'),btn=form.querySelector('.nkbtn'),msg=form.querySelector('.nkmsg');
  btn.onclick=function(){
    var num=(inp.value||'').trim();if(!num){msg.style.color='#f87171';msg.textContent='введи № заявки';return;}
    var bin=(inp2.value||'').trim();btn.disabled=true;msg.style.color='#94a3b8';
    nkRead(num,bin,function(stage,text){msg.innerHTML=nkStepsHTML(stage,text);},function(res){
      btn.disabled=false;
      if(res.error){msg.style.color='#f87171';msg.innerHTML='<div style="color:#f87171">✕ '+esc(res.error)+'</div>';return;}
      msg.innerHTML=nkStepsHTML('done','Готово')+'<div style="color:#34d399;margin-top:6px">✓ '+esc(res.terms.contract_no||'договор')+' · '+money(res.terms.total||0)+' ₸ сохранён'+(res.adata?' (+ справка Adata)':'')+'. Строка №'+esc(res.num)+' в таблице ниже — кликни, чтобы провалиться в заявку.</div>';
      inp.value='';inp2.value='';load();
    });
  };
  inp.addEventListener('keydown',function(e){if(e.key==='Enter')btn.click();});
  load();
}

function openZayavka(num){
  openModal('<div id=zkbody class=note style="min-height:120px;padding:20px">Заявка №'+esc(num)+' — тяну документы и оплаты из Bitrix… <span style=color:#94a3b8>(пара секунд)</span></div>');
  fetch('zayavka-card?num='+encodeURIComponent(num),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    var body=document.getElementById('zkbody');if(!body)return;
    if(d.error){body.innerHTML='<div class=err style="padding:16px">'+esc(d.error)+'</div>';return;}
    var link=(d.bx_portal&&d.id)?(' · <a href="'+d.bx_portal+'/crm/type/'+d.bx_entity+'/details/'+d.id+'/" target=_blank style="color:#7dd3fc">открыть в Bitrix →</a>'):'';
    var h='<div class=nkhd><div class=t>Заявка №'+esc(d.num)+'</div><div class=s>'+esc((d.title||'').slice(0,100))+link+'</div></div><div style="padding:14px 16px">';
    var docs=d.docs||[];
    h+='<h4>📎 Документы заявки</h4>';
    h+=docs.length?('<div class=tblscroll style="max-height:22vh"><table><tbody>'+docs.map(function(dc){return '<tr><td style="color:#0e7490;font-weight:600;white-space:nowrap">'+esc(dc.label)+'</td><td>'+esc(dc.name||'файл')+'</td></tr>';}).join('')+'</tbody></table></div>'):'<div class=note>вложений нет</div>';
    var nk=d.nakopitel;
    h+='<h4>💰 Условия договора</h4>';
    if(nk){h+='<div class=nknote>Договор '+esc(nk.contract_no||'—')+' · <b>'+money(nk.total||0)+' ₸</b> · статья: <b>'+esc(nk.article||'—')+'</b> · объект: '+esc(nk.object||'—')+(nk.ochered?(' · '+esc(nk.ochered)):'')+(nk.retention?(' · удерж. '+money(nk.retention)):'')+(nk.barter?' · бартер':'')+(nk.notes?('<div style="margin-top:5px;color:#64748b">📄 '+esc(nk.notes)+'</div>'):'')+'</div>';}
    else{h+='<div class=note>документы не прочитаны. <button class=zkread style="padding:5px 12px;border:0;border-radius:7px;background:#0ea5e9;color:#fff;cursor:pointer;font-weight:600">Разобрать заявку (все документы)</button> <span class=zkmsg style="font-size:12px;color:#94a3b8"></span> <span style="color:#94a3b8;font-size:11px">— ИИ читает договор + счёт + тех.требование + АВР</span></div>';}
    var pr=(d.payments||[]).map(function(p){return '<tr><td>'+esc(p.date||'')+'</td><td>'+esc(p.object||'—')+'</td><td>'+esc(p.account||'—')+'</td><td class=num>'+money(p.amount)+'</td></tr>';}).join('')||'<tr><td colspan=4 style=color:#94a3b8>нет оплат 1С по заявке</td></tr>';
    h+='<h4>Оплаты 1С по заявке</h4><div class=tblscroll style="max-height:22vh"><table><thead><tr><th>Дата</th><th>Объект</th><th>Счёт</th><th class=num>Сумма</th></tr></thead><tbody>'+pr+'</tbody></table></div></div>';
    body.className='';body.style.padding='0';body.innerHTML=h;
    var rb=body.querySelector('.zkread'),msg=body.querySelector('.zkmsg');
    if(rb){rb.onclick=function(){rb.disabled=true;nkRead(num,'',function(s,tx){if(msg)msg.textContent=tx;},function(res){openZayavka(num);});};}
  }).catch(function(e){var b=document.getElementById('zkbody');if(b)b.innerHTML='<div class=err style="padding:16px">ошибка: '+e+'</div>';});
}

function openObjectBdds(o){
  var arts=(o.articles||[]).slice();
  var basis=arts.some(function(a){return a.oplacheno>0;})?'oplacheno':(arts.some(function(a){return a.budget>0;})?'budget':'contracted');
  var basisLbl=basis==='oplacheno'?'оплачено 1С':(basis==='budget'?'бюджет сметы':'договоры');
  var pairs=arts.map(function(a){return [a.article,a[basis]||0];}).filter(function(p){return p[1]>0;}).sort(function(a,b){return b[1]-a[1];});
  var pal=['#0e7490','#c2410c','#7c3aed','#0891b2','#b45309','#4d7c0f','#be123c','#0369a1','#a21caf','#15803d'];
  var top=pairs.slice(0,9),restv=pairs.slice(9).reduce(function(s,x){return s+x[1];},0);if(restv>0)top.push(['прочее',restv]);
  var col={};top.forEach(function(x,i){col[x[0]]=pal[i%pal.length];});col['прочее']='#94a3b8';
  var rest=(o.budget||o.contracted)-o.oplacheno;
  function tile(l,vv,cls){return '<div class="nktile '+(cls||'')+'"><div class=l>'+l+'</div><div class=vv>'+vv+'</div></div>';}
  var h='<div class=nkhd><div class=t>'+esc(o.object)+(o.has_smeta?' <span class="pill ok" style="font-weight:600">смета</span>':'')+'</div>'
    +'<div class=s>Разбор объекта: из чего состоит и что законтрактовано / оплачено / выполнено</div></div>'
    +'<div class=nkstrip>'+tile('Бюджет (смета)',money(o.budget)+' ₸')+tile('Договоры',money(o.contracted)+' ₸')
    +tile('Оплачено 1С',money(o.oplacheno)+' ₸','f')+tile('Выполнено (АВР)',money(o.vypolneno)+' ₸')
    +tile('Остаток',money(rest)+' ₸','r')+'</div>';
  h+='<h4>Из чего состоит объект · '+basisLbl+'</h4>';
  if(top.length){h+='<div style="display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin:6px 0 4px">'
    +donutSVG(top,col)+'<div style="flex:1;min-width:260px">'+legend(top,col)+'</div></div>';}
  else h+='<div class=note>Пока нет разложения по статьям — прочитай договоры/тех.требования по заявкам этого объекта.</div>';
  arts.sort(function(a,b){return (b.oplacheno||b.budget||0)-(a.oplacheno||a.budget||0);});
  h+='<h4>Статьи <span style="font-weight:400;color:#94a3b8;font-size:12px">— клик по строке: заявки, накопитель, Bitrix</span></h4><div class=tblscroll style="max-height:34vh"><table><thead><tr><th>Статья</th><th class=num>Бюджет</th><th class=num>Договоры</th><th class=num>Оплачено</th><th class=num>Выполнено</th><th class=num>Остаток</th></tr></thead><tbody>'
    +arts.map(function(a){return '<tr class=bdartrow data-art="'+esc(a.article)+'" style="cursor:pointer"><td>'+esc(a.article)+' <span style="color:#0ea5e9;font-size:11px">→</span></td><td class=num>'+(a.budget?money(a.budget):'—')+'</td><td class=num>'+money(a.contracted)+'</td><td class=num style=color:#0e7490>'+money(a.oplacheno)+'</td><td class=num style=color:#7c3aed>'+(a.vypolneno?money(a.vypolneno):'—')+'</td><td class=num style=color:#b91c1c>'+money(a.ostatok)+'</td></tr>';}).join('')
    +'</tbody></table></div>';
  openModal(h);
  document.querySelectorAll('.bdartrow').forEach(function(tr){tr.onclick=function(){openArticleDrill(o.object,tr.getAttribute('data-art'));};});
}
function openArticleDrill(object,article){
  openModal('<div class=nkhd><div class=t>'+esc(object)+' · '+esc(article)+'</div><div class=s>заявки и платежи по этому виду работ</div></div><div id=adrill class=note>загрузка…</div>');
  fetch('bdds-article?object='+encodeURIComponent(object)+'&article='+encodeURIComponent(article),{cache:'no-store'})
    .then(function(r){return r.json();}).then(function(d){
    var host=document.getElementById('adrill');if(!host)return;host.className='';
    var rows=d.rows||[];
    if(!rows.length){host.innerHTML='<div class=note>Нет заявок по этому виду работ.</div>';return;}
    var tot=rows.reduce(function(s,x){return s+x.paid;},0);
    var portal=d.bx_portal||'',ent=d.bx_entity||178;
    host.innerHTML='<div class=note>Заявок: '+rows.length+' · оплачено суммарно '+money(tot)+' ₸. Клик по № — карточка/накопитель.</div>'
      +'<div class=tblscroll style="max-height:46vh"><table><thead><tr><th>№</th><th>Поставщик</th><th class=num>Оплачено</th><th class=num>Договор</th><th class=num>Выполнено</th><th>Накопитель</th><th>Bitrix</th></tr></thead><tbody>'
      +rows.map(function(x){
        var bx=(portal&&x.id)?('<a href="'+portal+'/crm/type/'+ent+'/details/'+x.id+'/" target=_blank style=color:#0ea5e9 onclick="event.stopPropagation()">открыть →</a>'):'—';
        return '<tr class=adrow data-num="'+esc(x.num)+'" style="cursor:pointer">'
          +'<td><b style=color:#0ea5e9>'+esc(x.num)+'</b></td><td>'+esc((x.supplier||'—')).slice(0,28)+'</td>'
          +'<td class=num style=color:#0e7490>'+money(x.paid)+'</td>'
          +'<td class=num>'+(x.contracted?money(x.contracted):'—')+'</td>'
          +'<td class=num style=color:#7c3aed>'+(x.vypolneno?money(x.vypolneno):'—')+'</td>'
          +'<td>'+(x.has_nk?'<span style=color:#15803d>✓ есть</span>':'<span style=color:#b45309>— создать</span>')+'</td>'
          +'<td>'+bx+'</td></tr>';
      }).join('')+'</tbody></table></div>';
    host.querySelectorAll('.adrow').forEach(function(tr){tr.onclick=function(){openZayavka(tr.getAttribute('data-num'));};});
  }).catch(function(e){var h=document.getElementById('adrill');if(h)h.innerHTML='<div class=err>ошибка: '+e+'</div>';});
}
function rBdds(v){
  v.appendChild(h2('БДДС по объектам'));
  v.appendChild(el('div','note','Три ноги: Договоры (план) · Выполнено (АВР из 1С-поступлений) · Оплачено (1С). Где есть смета — колонка Бюджет. Клик по объекту открывает окно-разбор с визуалом «из чего состоит».'));
  var pf=el('div');pf.style.cssText='display:flex;gap:6px;align-items:center;margin-bottom:10px';
  pf.innerHTML='<span style="color:#94a3b8;font-size:13px">Провалиться в заявку:</span><input class=zkin placeholder="№ заявки" style="padding:6px 10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;border-radius:8px;width:150px"><button class=zkgo style="padding:6px 12px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;cursor:pointer;font-weight:600">Открыть</button>';
  v.appendChild(pf);
  var zi=pf.querySelector('.zkin'),zg=pf.querySelector('.zkgo');
  zg.onclick=function(){var n=(zi.value||'').trim();if(n)openZayavka(n);};
  zi.addEventListener('keydown',function(e){if(e.key==='Enter')zg.click();});
  var host=el('div');host.innerHTML='<div class=note>Загрузка БДДС…</div>';v.appendChild(host);
  fetch('bdds.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    host.innerHTML='';
    var objs=(d&&d.objects)||[];
    if(!objs.length){host.innerHTML='<div class=cashln>Нет данных. Прочитай договоры во вкладке «Накопитель» и/или залей смету (<b>tools/smeta_import.py --post</b>).</div>';return;}
    var tB=0,tC=0,tO=0,tV=0;objs.forEach(function(o){tB+=o.budget;tC+=o.contracted;tO+=o.oplacheno;tV+=o.vypolneno;});
    var strip=el('div','svet wide');
    function tl(cls,val,lbl){return '<div class="sv '+cls+'"><div class=n style="font-size:16px">'+val+'</div><div class=l>'+lbl+'</div></div>';}
    strip.innerHTML=tl('bl',money(tB)+' ₸','Бюджет (смета)')+tl('nu',money(tC)+' ₸','Договоры')+tl('g',money(tO)+' ₸','Оплачено 1С')+tl('y',money(tV)+' ₸','Выполнено (АВР)');
    host.appendChild(strip);
    var wrap=el('div','tblscroll');wrap.style.marginTop='12px';var t=el('table');
    var body='';
    objs.sort(function(a,b){return (b.oplacheno||b.budget||0)-(a.oplacheno||a.budget||0);});
    objs.forEach(function(o,oi){
      var rest=(o.budget||o.contracted)-o.oplacheno;
      body+='<tr class=bdobj data-i="'+oi+'" style="cursor:pointer;font-weight:700">'
        +'<td>'+esc(o.object)+(o.has_smeta?' <span class="pill ok" style="font-weight:600">смета</span>':'')+' <span style="color:#0ea5e9;font-weight:600;font-size:12px">изучить →</span></td>'
        +'<td class=num>'+(o.budget?money(o.budget):'—')+'</td><td class=num>'+money(o.contracted)+'</td>'
        +'<td class=num style=color:#0e7490>'+money(o.oplacheno)+'</td><td class=num style=color:#7c3aed>'+money(o.vypolneno)+'</td>'
        +'<td class=num style=color:#b91c1c><b>'+money(rest)+'</b></td></tr>';
    });
    t.innerHTML='<thead><tr><th>Объект</th><th class=num>Бюджет</th><th class=num>Договоры</th><th class=num>Оплачено</th><th class=num>Выполнено</th><th class=num>Остаток</th></tr></thead><tbody>'+body+'</tbody>';
    wrap.appendChild(t);host.appendChild(wrap);
    t.querySelectorAll('tr.bdobj').forEach(function(tr){
      tr.onclick=function(){openObjectBdds(objs[+tr.getAttribute('data-i')]);};
    });
    host.appendChild(el('div','note','Оплачено/статья — все платежи 1С, разложенные по объекту и виду работ через ЗАЯВКУ Bitrix (в названии заявки есть объект и работа). Договоры — из прочитанных договоров (пока мало). Бюджет — из сметы (пока Аура). Выполнено — 1С-поступления (АВР). Статьи уточняются по мере чтения договоров (ИИ).'));
  }).catch(function(e){host.innerHTML='<div class=err>Ошибка БДДС: '+e+'</div>';});
}

function rProcess(v){
  v.appendChild(h2('Обработка заявок — чтение документов по клику'));
  v.appendChild(el('div','note','Очередь = оплаченные заявки, ещё не прочитанные ИИ, по убыванию суммы. «Обработать» → ИИ читает вложения ПО СОДЕРЖИМОМУ (устойчиво к мисфайлингу), заполняет статью/сумму/выполнено/условия в накопитель и БДДС.'));
  var host=el('div');host.innerHTML='<div class=note>Загрузка очереди…</div>';v.appendChild(host);
  function renderRes(hostEl,done){
    hostEl.innerHTML='<div class=card><div style="padding:8px 12px;font-size:12px;color:#64748b;background:#f8fafc">Результаты чтения ('+done.length+')</div>'
      +'<div class=tblscroll><table><thead><tr><th>№</th><th>Объект</th><th class=num>Оплачено</th><th>Что прочитано</th><th>Статья</th><th class=num>Сумма</th><th class=num>Выполнено</th><th>Итог</th></tr></thead><tbody>'
      +done.map(function(x){return '<tr>'
        +'<td>'+esc(x.num)+'</td><td>'+esc(x.object)+'</td><td class=num>'+money(x.paid)+'</td>'
        +'<td style="color:#64748b;font-size:11px">'+esc((x.doc_kinds||'').slice(0,40))+'</td>'
        +'<td>'+esc((x.article||'').slice(0,30))+'</td>'
        +'<td class=num>'+(x.total?money(x.total):'—')+'</td>'
        +'<td class=num style=color:#7c3aed>'+(x.vypolneno?money(x.vypolneno):'—')+'</td>'
        +'<td>'+(x.ok?'<span style=color:#0e7490>✓ сохранено</span>':'<span style=color:#b91c1c title="'+esc(x.error||'')+'">✕ '+esc((x.error||'').slice(0,70))+'</span>')+'</td>'
        +'</tr>';}).join('')+'</tbody></table></div></div>';
  }
  function load(){
    host.innerHTML='<div class=note>Загрузка очереди…</div>';
    fetch('queue.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
      host.innerHTML='';
      var strip=el('div','svet wide');
      function tl(cls,val,lbl){return '<div class="sv '+cls+'"><div class=n style="font-size:20px">'+val+'</div><div class=l>'+lbl+'</div></div>';}
      strip.innerHTML=tl('y',d.pending,'В очереди (не прочитано)')+tl('r',money(d.pending_sum)+' ₸','Сумма в очереди')+tl('g',d.read,'Прочитано ИИ');
      host.appendChild(strip);
      var pf=el('div');pf.style.cssText='display:flex;gap:8px;align-items:center;margin:14px 0;flex-wrap:wrap';
      pf.innerHTML='<span style="color:#475569;font-size:13px">Обработать следующих:</span>'
        +'<input class=pn type=number value=10 min=1 max=50 style="width:80px;padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px">'
        +'<button class=pgo style="padding:8px 16px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;font-weight:600;cursor:pointer">Обработать →</button>'
        +'<span class=pmsg style="color:#64748b;font-size:12px"></span>';
      host.appendChild(pf);
      var prog=el('div');prog.style.marginBottom='12px';host.appendChild(prog);
      var resHost=el('div');host.appendChild(resHost);
      var top=d.top||[];
      var qc=el('div','card');
      qc.innerHTML='<div style="padding:8px 12px;font-size:12px;color:#64748b;background:#f8fafc">Очередь (топ '+top.length+' по сумме оплаты)</div>'
        +'<div class=tblscroll><table><thead><tr><th>№</th><th>Объект</th><th class=num>Оплачено</th><th>Заявка</th></tr></thead><tbody>'
        +top.map(function(x){return '<tr><td>'+esc(x.num)+'</td><td>'+esc(x.object)+'</td><td class=num>'+money(x.paid)+'</td><td style="color:#64748b">'+esc((x.title||'').slice(0,60))+'</td></tr>';}).join('')+'</tbody></table></div>';
      host.appendChild(qc);
      var pn=pf.querySelector('.pn'),pgo=pf.querySelector('.pgo'),pmsg=pf.querySelector('.pmsg');
      pgo.onclick=function(){
        var n=Math.max(1,Math.min(50,parseInt(pn.value)||10));
        pgo.disabled=true;pmsg.textContent='запускаю…';
        fetch('process?n='+n,{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
          if(j.error||!j.job){pgo.disabled=false;pmsg.textContent='ошибка: '+(j.error||'нет задачи');return;}
          var poll=setInterval(function(){
            fetch('nk-status?id='+encodeURIComponent(j.job),{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
              var rr=s.result||{},i=rr.i||0,total=rr.total||n,pct=total?Math.round(i/total*100):0;
              prog.innerHTML='<div style="font-size:13px;color:#334155;margin-bottom:4px">'+esc(s.msg||'')+'</div>'
                +'<div class=track style="height:12px"><div class="fill fi" style="width:'+pct+'%;background:#0ea5e9"></div></div>';
              var done=rr.done||[];if(done.length)renderRes(resHost,done);
              if(s.done){clearInterval(poll);pgo.disabled=false;pmsg.textContent=s.msg||'готово';
                fetch('queue.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(dd){
                  strip.innerHTML=tl('y',dd.pending,'В очереди (не прочитано)')+tl('r',money(dd.pending_sum)+' ₸','Сумма в очереди')+tl('g',dd.read,'Прочитано ИИ');
                }).catch(function(){});}   // обновляем ТОЛЬКО счётчики, результаты не стираем
            }).catch(function(e){clearInterval(poll);pgo.disabled=false;pmsg.textContent='ошибка опроса';});
          },1200);
        }).catch(function(e){pgo.disabled=false;pmsg.textContent='ошибка запуска';});
      };
    }).catch(function(e){host.innerHTML='<div class=err>Ошибка очереди: '+e+'</div>';});
  }
  load();
}
function rOplata(v){
  v.appendChild(h2('Проверка перед оплатой — Шерлок + Баффет'));
  v.appendChild(el('div','note','Заявки в стадии «Оплата» воронки 178. «Проверить» считает вердикт (дубли/переплата/контрагент) без записи; «Опубликовать» пишет ОДИН комментарий в карточку Bitrix (идемпотентно по хэшу). Временный инструмент.'));
  var host=el('div');host.innerHTML='<div class=note>Загрузка…</div>';v.appendChild(host);
  function lvlPill(l){return l==='red'?'<span style="color:#b91c1c;font-weight:700">⛔ СТОП</span>':(l==='warn'?'<span style="color:#b45309;font-weight:700">⚠ замечания</span>':'<span style="color:#15803d;font-weight:700">✅ можно</span>');}
  function renderRes(hostEl,done){
    hostEl.innerHTML='<div class=card><div style="padding:8px 12px;font-size:12px;color:#64748b;background:#f8fafc">Результаты ('+done.length+') — клик по строке: полный текст комментария</div>'
      +'<div class=tblscroll><table><thead><tr><th>Итог</th><th>№</th><th>Поставщик</th><th class=num>Сумма</th><th>Шерлок / замечания</th><th>Bitrix</th></tr></thead><tbody>'
      +done.map(function(x,i){
        var sh=(x.sherlock&&x.sherlock.length)?esc(x.sherlock[0].slice(0,50)):'—';
        var rm=(x.remarks&&x.remarks.length)?(' · замечаний '+x.remarks.length):'';
        return '<tr class=ocard data-i="'+i+'" style="cursor:pointer"><td>'+lvlPill(x.level)+'</td>'
          +'<td><b style=color:#0ea5e9>'+esc(x.num)+'</b></td><td>'+esc((x.supplier||'—')).slice(0,26)+'</td>'
          +'<td class=num>'+money(x.amount)+'</td><td style="font-size:12px;color:#475569">'+sh+rm+'</td>'
          +'<td>'+(x.posted?('<span style=color:#0e7490>'+esc(x.posted)+'</span>'):'—')+'</td></tr>'
          +'<tr class="ofull o'+i+'" style="display:none"><td colspan=6 style="background:#f8fafc"><pre style="white-space:pre-wrap;font-size:12px;margin:0;padding:8px 10px;font-family:inherit">'+esc(x.text||'(нет текста)')+'</pre></td></tr>';
      }).join('')+'</tbody></table></div></div>';
    hostEl.querySelectorAll('tr.ocard').forEach(function(tr){tr.onclick=function(){
      var i=tr.getAttribute('data-i'),f=hostEl.querySelector('tr.o'+i);if(f)f.style.display=(f.style.display==='none'?'':'none');
    };});
  }
  function load(){
    host.innerHTML='<div class=note>Загрузка…</div>';
    fetch('oplata.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
      host.innerHTML='';
      if(d.error){host.innerHTML='<div class=err>'+esc(d.error)+'</div>';return;}
      var strip=el('div','svet wide');
      function tl(cls,val,lbl){return '<div class="sv '+cls+'"><div class=n style="font-size:20px">'+val+'</div><div class=l>'+lbl+'</div></div>';}
      strip.innerHTML=tl('y',d.count,'Заявок в стадии «Оплата»')+tl('r',money(d.sum)+' ₸','Сумма к оплате');
      host.appendChild(strip);
      var pf=el('div');pf.style.cssText='display:flex;gap:8px;align-items:center;margin:14px 0;flex-wrap:wrap';
      pf.innerHTML='<span style="color:#475569;font-size:13px">Заявок:</span>'
        +'<input class=pn type=number value=10 min=1 max=60 style="width:74px;padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px">'
        +'<label style="font-size:13px;color:#475569;display:flex;align-items:center;gap:5px"><input type=checkbox class=prd checked> дочитывать документы (счёт/договор/АВР — нужно для Баффета)</label>'
        +'<button class=pchk style="padding:8px 16px;border:0;border-radius:8px;background:#0ea5e9;color:#fff;font-weight:600;cursor:pointer">Проверить (без записи)</button>'
        +'<button class=ppost style="padding:8px 16px;border:0;border-radius:8px;background:#b45309;color:#fff;font-weight:600;cursor:pointer">Опубликовать в Bitrix</button>'
        +'<span class=pmsg style="color:#64748b;font-size:12px"></span>';
      host.appendChild(pf);
      var prog=el('div');prog.style.marginBottom='12px';host.appendChild(prog);
      var resHost=el('div');host.appendChild(resHost);
      var top=d.top||[];
      var qc=el('div','card');
      qc.innerHTML='<div style="padding:8px 12px;font-size:12px;color:#64748b;background:#f8fafc">Очередь стадии «Оплата» (топ '+top.length+' по сумме)</div>'
        +'<div class=tblscroll><table><thead><tr><th>№</th><th>Поставщик</th><th class=num>Сумма</th></tr></thead><tbody>'
        +top.map(function(x){return '<tr><td>'+esc(x.num)+'</td><td>'+esc((x.supplier||'—')).slice(0,40)+'</td><td class=num>'+money(x.amount)+'</td></tr>';}).join('')+'</tbody></table></div>';
      host.appendChild(qc);
      var pn=pf.querySelector('.pn'),prd=pf.querySelector('.prd'),pchk=pf.querySelector('.pchk'),ppost=pf.querySelector('.ppost'),pmsg=pf.querySelector('.pmsg');
      function run(post){
        var n=Math.max(1,Math.min(60,parseInt(pn.value)||10)),read=prd.checked?1:0;
        if(post&&!confirm('Опубликовать вердикты в '+n+' карточках Bitrix? (идемпотентно — повтор не плодит дубли)'))return;
        pchk.disabled=ppost.disabled=true;pmsg.textContent='запускаю…';
        fetch('precheck-run?post='+(post?1:0)+'&read='+read+'&n='+n,{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
          if(j.error||!j.job){pchk.disabled=ppost.disabled=false;pmsg.textContent='ошибка: '+(j.error||'нет задачи');return;}
          var poll=setInterval(function(){
            fetch('nk-status?id='+encodeURIComponent(j.job),{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
              var rr=s.result||{},i=rr.i||0,total=rr.total||n,pct=total?Math.round(i/total*100):0;
              prog.innerHTML='<div style="font-size:13px;color:#334155;margin-bottom:4px">'+esc(s.msg||'')+'</div><div class=track style="height:12px"><div class="fill fi" style="width:'+pct+'%;background:#0ea5e9"></div></div>';
              var done=rr.done||[];if(done.length)renderRes(resHost,done);
              if(s.done){clearInterval(poll);pchk.disabled=ppost.disabled=false;pmsg.textContent=s.msg||'готово';}
            }).catch(function(e){clearInterval(poll);pchk.disabled=ppost.disabled=false;pmsg.textContent='ошибка опроса';});
          },1200);
        }).catch(function(e){pchk.disabled=ppost.disabled=false;pmsg.textContent='ошибка запуска';});
      }
      pchk.onclick=function(){run(false);};
      ppost.onclick=function(){run(true);};
    }).catch(function(e){host.innerHTML='<div class=err>Ошибка: '+e+'</div>';});
  }
  load();
}
function rCtrl(v){
  var g={};D.payments.forEach(function(p){if(!p.bin)return;var k=p.company+'|'+p.bin+'|'+Math.round(p.amount)+'|'+p.date;(g[k]=g[k]||[]).push(p);});
  var dub=Object.keys(g).map(function(k){return g[k];}).filter(function(a){return a.length>1;}).map(function(a){return {company:a[0].company,name:a[0].name,bin:a[0].bin,amount:a[0].amount,date:a[0].date,n:a.length};});
  var cols=[{key:'company',label:'Компания'},{key:'name',label:'Поставщик'},{key:'bin',label:'БИН'},
    {key:'amount',label:'Сумма',num:true,render:function(x){return money(x.amount);}},{key:'date',label:'Дата'},{key:'n',label:'Платежей',num:true}];
  v.appendChild(h2('🔴 Кандидаты в дубли (оплаты поставщикам)'));
  v.appendChild(el('div','note','Один БИН + одинаковая сумма + одна дата в одной компании, ≥2 платежей — проверить перед/после оплаты.'));
  v.appendChild(card(dataTable(dub,cols,{sort:'amount',dir:-1,searchText:function(x){return x.name+' '+x.company+' '+x.bin;},searchPlaceholder:'поиск…',limit:800})));
}
})();
'''


def dashboard():
    """Лёгкая оболочка SPA: тянет данные с /data.json и рендерит вкладки на клиенте."""
    return (f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>ATAMŪRA · Финансы</title><style>{CSS}</style></head><body>"
            f"<div id=app class=loading>Загрузка данных…</div>"
            f"<div id=cftip class=cftip></div>"
            f"<script>{APP_JS}</script></body></html>")


def data_json():
    """Все данные дашборда как JSON — основа для интерактивного клиента (вкладки/фильтры/поиск)."""
    c = _db()
    flow = c.execute("SELECT company,kind,date,bin,name,amount,vidop,supplier,number,purpose,comment,doc FROM flow").fetchall()
    meta = dict(c.execute("SELECT k,v FROM meta").fetchall()); c.close()
    rec = reconcile()
    out = [r for r in flow if r[1] == "out"]; inp = [r for r in flow if r[1] == "in"]
    rc = [r for r in flow if r[1] == "receipt"]; sup = [r for r in out if r[7] == 1]
    payments = [{"company": r[0], "date": r[2], "bin": r[3], "name": r[4], "amount": r[5],
                 "vidop": r[6], "num": _num_from(r[9]), "purpose": r[9], "doc": r[11],
                 "acct": _account_from(r[9]), "obj": _object_from(r[9]),
                 "cat": _category(r[9], r[10], r[4], r[6]),
                 "cash": "кассов" in (r[11] or "").lower()} for r in sup]
    orphans = [{"num": x[0], "company": x[1], "name": x[2], "amount": x[3], "date": x[4],
                "cand": (x[5][1] if x[5] else None),
                "reestr": ({"by": x[6][0], "num": x[6][1], "name": x[6][2]} if x[6] else None)}
               for x in rec["pay_no_zayavka"]]
    zayavki = [{"id": z[0], "num": z[1], "company": z[2], "supplier": z[3], "amount": z[4],
                "paid": z[5], "stage": z[6]} for z in rec["z_list"]]
    return {"meta": meta,
            "kpi": {"out": sum(r[5] for r in out), "in": sum(r[5] for r in inp),
                    "receipt": sum(r[5] for r in rc), "sup": sum(r[5] for r in sup)},
            "series": _timeseries(flow), "payments": payments, "orphans": orphans, "zayavki": zayavki,
            "by_company": rec["by_company"],
            "counts": {"matched": rec["matched_n"], "reserve": len(rec["reserve"]),
                       "in_progress": len(rec["in_progress"]), "rejected": len(rec["rejected"]),
                       "nz": len(rec["pay_no_zayavka"]), "nz_in_reestr": rec["nz_in_reestr"],
                       "nz_orphan": rec["nz_orphan"], "reestr_rows": rec["reestr_rows"],
                       "cash_tot": rec["cash_tot"], "cash_n": rec["cash_n"]},
            "bx_portal": BX_PORTAL, "bx_entity": BX_ENTITY}


def nakopitel_data():
    """Накопитель: договоры (условия ИИ) × факт 1С (по № заявки) → остаток + детали для drill."""
    c = _db()
    nk = c.execute("""SELECT num,bin,contract_no,contract_date,total,avans_sum,retention_pct,
                      retention_sum,barter,barter_sum,object,ochered,account,notes,title
                      FROM nakopitel""").fetchall()
    ashort = dict(c.execute("SELECT bin,short FROM adata_cache").fetchall())
    acache = dict(c.execute("SELECT bin,json FROM adata_cache").fetchall())
    idx = dict(c.execute("SELECT num,id FROM zayavka_idx").fetchall())
    pays = c.execute("SELECT date,amount,purpose FROM flow WHERE kind='out' AND supplier=1").fetchall()
    c.close()
    fact_by_num = defaultdict(float)
    pay_by_num = defaultdict(list)
    for date, amt, purpose in pays:
        n = _num_from(purpose)
        if n:
            fact_by_num[n] += amt or 0
            pay_by_num[n].append({"date": date, "amount": amt, "account": _account_from(purpose),
                                  "object": _object_from(purpose), "purpose": (purpose or "")[:100]})
    rows, bins = [], set()
    for (num, bin_, cno, cdate, total, avans, rpct, rsum, barter, bsum, obj, och, acc, notes, title) in nk:
        fact = fact_by_num.get(num, 0.0)
        supplier = ashort.get(bin_) or ""
        if not supplier and title:
            parts = [p.strip() for p in title.split("/")]
            supplier = parts[2] if len(parts) > 2 else ""
        if bin_:
            bins.add(bin_)
        rows.append({"num": num, "bin": bin_, "id": idx.get(num), "contract_no": cno, "date": cdate,
                     "total": total, "avans": avans, "retention_pct": rpct, "retention": rsum,
                     "barter": bool(barter), "barter_sum": bsum, "object": obj, "ochered": och,
                     "account": acc, "notes": notes, "supplier": supplier, "fact": fact,
                     "ostatok": total - avans - rsum - bsum - fact,
                     "payments": pay_by_num.get(num, [])})
    # карточки Adata (basic + 10-пунктовый чеклист) только для нужных БИН
    try:
        import adata as _A
    except Exception:
        _A = None
    amap = {}
    for b in bins:
        j = acache.get(b)
        if not j:
            continue
        try:
            info = json.loads(j)
        except Exception:
            continue
        basic = info.get("basic", {})
        cl = []
        if _A:
            try:
                cl = [{"n": n, "name": nm, "status": st, "dep": dp}
                      for n, nm, st, dp in _A.render_checklist(info)]
            except Exception:
                cl = []
        amap[b] = {"short": basic.get("short_name", ""), "oked": basic.get("oked", ""),
                   "director": basic.get("fullname_director", ""), "iin": basic.get("biin", ""),
                   "nds": bool(basic.get("is_nds_payer")), "checklist": cl}
    return {"rows": rows, "count": len(rows), "adata": amap, "bx_portal": BX_PORTAL, "bx_entity": BX_ENTITY}


def login_page(err=""):
    e = f"<div style='color:#dc2626;font-size:12.5px;margin-bottom:10px'>{err}</div>" if err else ""
    return (f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>Вход · ATAMŪRA Финансы</title>"
            f"<style>{CSS}body{{background:#0f2233;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}"
            f".loginbox{{display:block;background:#fff;border-radius:16px;padding:34px 40px;max-width:340px;width:100%;box-sizing:border-box}}"
            f".loginbox input{{display:block;width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:9px;font-size:14px;margin-bottom:10px;box-sizing:border-box}}"
            f".loginbox button{{display:block;width:100%;background:#0e7490;color:#fff;border:0;border-radius:9px;padding:11px;font-size:14px;font-weight:700;cursor:pointer}}</style>"
            f"</head><body><form class=loginbox method=post action=/login>"
            f"<div style='font-size:30px;text-align:center'>◎</div>"
            f"<h2 style='margin:6px 0 2px;text-align:center'>ATAMŪRA · Финансы</h2>"
            f"<div style='color:#64748b;font-size:12.5px;text-align:center;margin-bottom:18px'>Вход в финансовое ядро</div>"
            f"{e}<input name=u placeholder='Логин' autofocus autocomplete=username>"
            f"<input name=p type=password placeholder='Пароль' autocomplete=current-password>"
            f"<button>Войти</button></form></body></html>")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def _user(self):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith("fin_sess="):
                return _check_session(part[len("fin_sess="):])
        return None

    def _gate(self):
        """True — пропускаем дальше; False — уже отправили редирект/401 (нет доступа)."""
        if not AUTH_ON:
            return True
        if self.path in ("/healthz", "/login", "/logout") or self.path.startswith("/api/"):
            return True
        if self._user():
            return True
        if self.path.endswith(".json") or self.path in ("/sync", "/sync-full") or self.path.startswith(("/nk-", "/zayavka")):
            self._send('{"error":"auth required"}', "application/json", 401)
        else:
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers()
        return False

    def do_GET(self):
        if not self._gate():
            return
        if self.path == "/login":
            self._send(login_page())
        elif self.path == "/logout":
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "fin_sess=; Path=/; Max-Age=0"); self.end_headers()
        elif self.path == "/healthz": self._send("ok", "text/plain")
        elif self.path == "/sync":
            try: self._send(json.dumps(sync_bitrix(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/sync-full":
            try: self._send(json.dumps(sync_bitrix_full(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/refresh":
            try:
                sync_bitrix()
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
            except Exception as e: self._send(f"<pre>Ошибка синхронизации: {e}</pre>", code=500)
        elif self.path == "/data.json":
            try: self._send(json.dumps(data_json(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/nakopitel.json":
            try: self._send(json.dumps(nakopitel_data(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/bdds.json":
            try: self._send(json.dumps(bdds_data(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/zayavka-card"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                num = (q.get("num", [""])[0]).strip()
                if not num:
                    self._send('{"error":"нет № заявки"}', "application/json", 400)
                else:
                    self._send(json.dumps(zayavka_card(num), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/day.json"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                dt = (q.get("date", [""])[0]).strip()
                self._send(json.dumps(day_payments(dt), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/adata-check"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                binf = (q.get("bin", [""])[0]).strip()
                refresh = (q.get("refresh", ["0"])[0]) == "1"
                self._send(json.dumps(adata_check(binf, refresh), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/bdds-article"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                obj = (q.get("object", [""])[0]).strip()
                art = (q.get("article", [""])[0]).strip()
                self._send(json.dumps(bdds_article(obj, art), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/queue.json":
            try: self._send(json.dumps(queue_stats(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/oplata.json":
            try: self._send(json.dumps(oplata_stats(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/precheck-run"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                post = (q.get("post", ["0"])[0]) == "1"
                read = (q.get("read", ["0"])[0]) == "1"
                n = max(1, min(60, int((q.get("n", ["10"])[0]) or 10)))
                jid = _new_job()
                threading.Thread(target=_run_precheck_job, args=(jid, post, n, read), daemon=True).start()
                self._send(json.dumps({"job": jid}), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/process"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                n = max(1, min(50, int((q.get("n", ["10"])[0]) or 10)))
                jid = _new_job()
                threading.Thread(target=_run_process_job, args=(jid, n), daemon=True).start()
                self._send(json.dumps({"job": jid}), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/nk-read"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                num = (q.get("num", [""])[0]).strip()
                binv = (q.get("bin", [""])[0]).strip()
                if not num:
                    self._send('{"error":"нет № заявки"}', "application/json", 400)
                else:
                    jid = _new_job()
                    threading.Thread(target=_run_nk_job, args=(jid, num, binv), daemon=True).start()
                    self._send(json.dumps({"job": jid}), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path.startswith("/nk-status"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = (q.get("id", [""])[0]).strip()
            with _JOBS_LOCK:
                st = dict(_JOBS.get(jid) or {"stage": "unknown", "msg": "задача не найдена", "done": True})
            self._send(json.dumps(st, ensure_ascii=False), "application/json")
        elif self.path.startswith("/nk-comments"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                num = (q.get("num", [""])[0]).strip()
                iid = (q.get("id", [""])[0]).strip()
                import precheck as PC
                cms = PC.fetch_comments(int(iid)) if iid.isdigit() else []
                if num and iid.isdigit():
                    PC.store_comments(num, int(iid), cms)
                self._send(json.dumps({"comments": cms}, ensure_ascii=False), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/" or self.path.startswith("/?"):
            try: self._send(dashboard())
            except Exception as e: self._send(f"<pre>Ошибка: {e}</pre>", code=500)
        else: self._send("404", code=404)

    def do_POST(self):
        if self.path == "/login":
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
            u = (form.get("u", [""])[0]).strip()
            p = form.get("p", [""])[0]
            if AUTH_USERS.get(u) and hmac.compare_digest(AUTH_USERS[u], _pw_hash(p)):
                self.send_response(302); self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 f"fin_sess={_make_session(u)}; Path=/; HttpOnly; SameSite=Lax; Max-Age={7*86400}")
                self.end_headers()
            else:
                self._send(login_page("Неверный логин или пароль"), code=401)
            return
        if self.path not in ("/api/ingest", "/api/reestr", "/api/smeta"):
            self._send("404", code=404); return
        if self.headers.get("X-Service-Key") != KEY:
            self._send('{"error":"bad key"}', "application/json", 401); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            saved = ({"/api/reestr": store_reestr, "/api/smeta": store_smeta}.get(self.path) or store)(payload)
            self._send(json.dumps({"ok": True, "saved": saved}), "application/json")
        except Exception as e:
            self._send(json.dumps({"error": str(e)}), "application/json", 500)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True


if __name__ == "__main__":
    print(f"ATAMŪRA Finance ЛК: http://localhost:{PORT}  (приём: POST /api/ingest)")
    Server((HOST, PORT), H).serve_forever()
