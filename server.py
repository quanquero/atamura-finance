# -*- coding: utf-8 -*-
"""
ATAMŪRA Finance — веб-ЛК финдира (приёмник + дашборд).
Принимает срез от парсера (POST /api/ingest, X-Service-Key), кладёт в своё ядро (SQLite),
показывает дашборд: сводка по холдингу / дубли / расхождения / топ поставщиков.
Только стандартная библиотека Python 3. Запуск:  python server.py

Env:  SERVICE_KEY (ключ для /api/ingest),  PORT (по умолч. 8013),  HOST (0.0.0.0 в проде).
"""
import http.server, json, math, os, re, socketserver, sqlite3, threading, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "finance_core.sqlite3")
PORT = int(os.environ.get("PORT", "8013"))
HOST = os.environ.get("HOST", "127.0.0.1")
KEY  = os.environ.get("SERVICE_KEY", "dev-finance-key")   # в проде задать через env
# Вебхук Bitrix (crm) — финсервер сам тянет Служебные записки (воронка оплат, entityTypeId 178)
BITRIX = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
BX_PORTAL = BITRIX.split("/rest/")[0] if "/rest/" in BITRIX else ""   # для ссылок на карточки заявок
BX_ENTITY = 178
BX_MONTHS = int(os.environ.get("BX_MONTHS", "6"))   # окно заявок шире среза 1С: платёж может ссылаться на старую/продлённую заявку


def _db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS flow(
        company TEXT, kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS zayavka(
        id INTEGER, number TEXT, title TEXT, supplier TEXT, amount REAL, stage TEXT, company TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reestr(
        src TEXT, num TEXT, name TEXT, bin TEXT, iban TEXT, amount REAL, purpose TEXT, invoice TEXT)""")
    # миграция: старая zayavka без company (создана прошлой версией)
    zcols = {r[1] for r in c.execute("PRAGMA table_info(zayavka)").fetchall()}
    if "company" not in zcols:
        c.execute("ALTER TABLE zayavka ADD COLUMN company TEXT")
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


def sync_bitrix():
    """Тянем Служебные записки (воронка оплат 178) за BX_MONTHS мес → таблица zayavka."""
    if not BITRIX:
        return {"error": "BITRIX_WEBHOOK не задан"}
    since = (datetime.now() - timedelta(days=30 * BX_MONTHS)).strftime("%Y-%m-%dT00:00:00")
    rows, start = [], 0
    while True:
        d = _bx("crm.item.list", {
            "entityTypeId": BX_ENTITY, "start": start,
            "filter[>=createdTime]": since, "order[id]": "desc",
            "select[]": ["id", "title", "opportunity", "stageId", "ufCrm4_1644310716", "ufCrm4_1762251054209"],
        })
        items = d.get("result", {}).get("items", [])
        rows += items
        if len(items) < 50 or len(rows) >= 6000:
            break
        start += 50
    c = _db()
    c.execute("DROP TABLE IF EXISTS zayavka")
    c.execute("""CREATE TABLE zayavka(
        id INTEGER, number TEXT, title TEXT, supplier TEXT, amount REAL, stage TEXT, company TEXT)""")
    c.executemany("INSERT INTO zayavka VALUES(?,?,?,?,?,?,?)", [(
        it.get("id"),
        str(it.get("ufCrm4_1644310716") or "").strip() or _num_from(it.get("title")),
        it.get("title", ""), str(it.get("ufCrm4_1762251054209") or ""),
        float(it.get("opportunity") or 0), it.get("stageId", ""), _company_from(it.get("title")),
    ) for it in rows])
    c.execute("INSERT INTO meta(k,v) VALUES('bx_sync',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (datetime.now().strftime("%Y-%m-%d %H:%M"),))
    c.commit(); n = len(rows); c.close()
    return {"ok": True, "заявок": n}


def _num_from(s):
    """Вытащить № заявки (15xxx–16xxx…) из текста."""
    m = re.search(r"№\s*(\d{4,6})", str(s or "")) or re.search(r"\b(1[0-9]{4})\b", str(s or ""))
    return m.group(1) if m else ""


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
    rs = c.execute("SELECT num,name,bin FROM reestr").fetchall(); c.close()
    znums = {z[1] for z in zs if z[1]}
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

    pay_nums, pay_no_num, pay_no_zayavka = {}, [], []
    cash_tot, cash_n, cash_matched = 0.0, 0, 0
    for company, name, amt, date, purpose, doc, pbin in pays:
        zn = _num_from(purpose)
        if zn:
            pay_nums.setdefault(zn, True)   # заявка «оплачена» и наличными, и с р/с
        if "кассов" in (doc or "").lower():   # РКО — наличные, отдельный поток
            cash_tot += amt; cash_n += 1
            if zn and zn in znums: cash_matched += 1
            continue
        if not zn:
            pay_no_num.append((company, name, amt, date))
        elif zn not in znums:
            # проверяем реестр финотдела: по № или по БИН
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


def money(n):
    return f"{n:,.0f}".replace(",", " ")


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


def dashboard():
    c = _db()
    flow = c.execute("SELECT company,kind,date,bin,name,amount,vidop,supplier,number,purpose,comment FROM flow").fetchall()
    meta = dict(c.execute("SELECT k,v FROM meta").fetchall()); c.close()
    if not flow:
        return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><div class=top><b>ATAMŪRA · Финансы</b></div><div class=wrap><p>Ядро пустое — срез ещё не пришёл. Запусти парсер с push.</p></div>"

    out = [r for r in flow if r[1] == "out"]
    inp = [r for r in flow if r[1] == "in"]
    rec = [r for r in flow if r[1] == "receipt"]
    sup = [r for r in out if r[7] == 1]
    out_t = sum(r[5] for r in out); in_t = sum(r[5] for r in inp)
    done_t = sum(r[5] for r in rec); sup_t = sum(r[5] for r in sup); saldo = in_t - out_t

    def kpi(v, l, cls=""):
        return f"<div class=kpi><div class=l>{l}</div><div class='v {cls}'>{v}</div></div>"
    kpis = (kpi(money(out_t)+" ₸", "Отток (исходящие)") + kpi(money(in_t)+" ₸", "Приток (входящие)")
            + kpi(("+" if saldo >= 0 else "")+money(saldo)+" ₸", "Сальдо", "pos" if saldo >= 0 else "negv")
            + kpi(money(sup_t)+" ₸", "Оплаты поставщикам") + kpi(money(done_t)+" ₸", "Выполнено (АВР)"))

    def _legend(pairs, colors):
        tot = sum(v for _, v in pairs) or 1
        h = ""
        for lb, v in pairs:
            col = colors.get(lb, "#94a3b8")
            h += (f"<div class=lgi><span class=sw style='background:{col}'></span>"
                  f"<span class=lgn>{lb}</span><b>{money(v)}</b><span class=pc>{v/tot*100:.0f}%</span></div>")
        return f"<div class=lgrow>{h}</div>"

    # обороты по дочкам — пончик «отток по дочкам» (топ-7 + прочее)
    byco = defaultdict(lambda: [0.0, 0.0])
    for r in out: byco[r[0]][0] += r[5]
    for r in inp: byco[r[0]][1] += r[5]
    comp_out = sorted(((co, d[0]) for co, d in byco.items()), key=lambda x: -x[1])
    _pal = ["#0e7490", "#c2410c", "#7c3aed", "#0891b2", "#b45309", "#4d7c0f", "#be123c"]
    top7 = comp_out[:7]
    rest = sum(v for _, v in comp_out[7:])
    comp_pairs = top7 + ([("прочее", rest)] if rest > 0 else [])
    comp_col = {co: _pal[i] for i, (co, _) in enumerate(top7)}
    comp_col["прочее"] = "#cbd5e1"
    oborot = f"<div class=donutwrap>{_donut(comp_pairs, comp_col)}{_legend(comp_pairs, comp_col)}</div>"

    # дубли: company+bin+amount+date, >1
    g = defaultdict(list)
    for r in sup:
        if r[3]: g[(r[0], r[3], round(r[5], 2), r[2])].append(r)
    dub = {k: v for k, v in g.items() if len(v) > 1}
    dub_rows = "".join(f"<tr><td>{v[0][0]}</td><td>{v[0][4]}</td><td class='num neg'>{money(a)}</td><td>{d}</td><td class=num>{len(v)}</td></tr>"
                       for (co, b, a, d), v in sorted(dub.items(), key=lambda x: -x[0][2])) or "<tr><td colspan=5 style=color:#94a3b8>Дублей не найдено</td></tr>"

    # расхождения (поступило vs оплачено по company+bin)
    byc = defaultdict(lambda: [0.0, 0.0, ""])
    for r in sup:
        if r[3]: byc[(r[0], r[3])][1] += r[5]; byc[(r[0], r[3])][2] = r[4]
    for r in rec:
        if r[3]:
            byc[(r[0], r[3])][0] += r[5]
            if not byc[(r[0], r[3])][2]: byc[(r[0], r[3])][2] = r[4]
    disc = sorted(((abs(d[0]-d[1]), co, d[2], d[0], d[1], d[0]-d[1]) for (co, b), d in byc.items()), reverse=True)[:12]
    disc_rows = ""
    for _, co, nm2, done, paid, diff in disc:
        pcls = "pr" if diff < -1 else "py"
        plabel = "оплата&gt;поступл" if diff < -1 else "поступл&gt;оплата"
        disc_rows += (f"<tr><td>{co}</td><td>{nm2}</td><td class=num>{money(done)}</td>"
                      f"<td class=num>{money(paid)}</td><td class='num b'>{money(diff)}</td>"
                      f"<td><span class='pill {pcls}'>{plabel}</span></td></tr>")

    # топ поставщиков (бары)
    tot = defaultdict(float); nmm = {}
    for r in sup:
        k = r[3] or r[4]; tot[k] += r[5]; nmm[k] = r[4]
    tops = sorted(tot.items(), key=lambda x: -x[1])[:12]
    tmx = tops[0][1] if tops else 1
    top_bars = ""
    for k, s in tops:
        w = max(1, s/tmx*100)
        top_bars += (f"<div class=brow><div class=hd><span class=co>{nmm[k]}</span><span class=vv>{money(s)} ₸</span></div>"
                     f"<div class=track><div class='fill fo' style='width:{w:.1f}%'></div></div></div>")

    # разрез по типу расхода (услуги/поставки/подряд) — пончик, эвристика по назначению платежа
    cats = defaultdict(float)
    for r in sup:
        cats[_category(r[9], r[10], r[4], r[6])] += r[5]
    cat_col = {"подряд": "#0e7490", "поставка": "#c2410c", "услуга": "#7c3aed", "прочее": "#94a3b8"}
    cat_pairs = [(k, cats.get(k, 0.0)) for k in ["подряд", "поставка", "услуга", "прочее"] if cats.get(k, 0) > 0]
    cat_donut = f"<div class=donutwrap>{_donut(cat_pairs, cat_col)}{_legend(cat_pairs, cat_col)}</div>"

    # временной ряд отток/приток (для линейного графика с тултипом)
    series = _timeseries(flow)
    chart_block = ("<div class=chartwrap><svg id=cfsvg class=lchart></svg></div>"
                   "<div class=clg><span class=k><span class=sw style='background:#ea580c'></span>отток</span>"
                   "<span class=k><span class=sw style='background:#0891b2'></span>приток</span>"
                   "<span style=color:#94a3b8>наведи курсор — суммы дня и крупные платежи</span></div>"
                   "<script>window.CF=" + json.dumps(series, ensure_ascii=False) + ";" + CF_JS + "</script>")

    svet = (f"<div class='sv r'><div class=n>{len(dub)}</div><div class=l>Кандидаты в дубли</div></div>"
            f"<div class='sv y'><div class=n>{len(disc)}</div><div class=l>Расхождения (проверить)</div></div>"
            f"<div class='sv g'><div class=n>{len(byco)}</div><div class=l>Компаний в своде</div></div>")

    rec = reconcile()
    bx_sync = meta.get('bx_sync', '—')
    refresh_btn = ("<a href='/refresh' class=rbtn "
                   "onclick=\"this.textContent='↻ обновляю из Bitrix…'\">↻ Обновить из Bitrix</a>")
    if rec.get("z_total"):
        sv2 = (f"<div class='sv g'><div class=n>{rec['matched_n']}</div><div class=l>Оплачено (есть платёж 1С)</div></div>"
               f"<div class='sv y'><div class=n>{len(rec['reserve'])}</div><div class=l>Одобрено, ждёт 1С (резерв)</div></div>"
               f"<div class='sv bl'><div class=n>{len(rec['in_progress'])}</div><div class=l>В работе</div></div>"
               f"<div class='sv nu'><div class=n>{len(rec['rejected'])}</div><div class=l>Отказано</div></div>"
               f"<div class='sv r'><div class=n>{len(rec['pay_no_zayavka'])}</div><div class=l>Оплата без заявки</div></div>")
        cash_line = (f"<div class=cashln>💵 Наличные (касса, РКО): <b>{money(rec['cash_tot'])} ₸</b> · "
                     f"{rec['cash_n']} платежей · сматчено с заявкой: {rec['cash_matched']}. "
                     f"<span style=color:#94a3b8>Отдельный поток — в «оплата без заявки» не считаются.</span></div>")
        # реестр заявок: два сигнала — стадия Bitrix × оплата 1С + ссылка на карточку
        _bx_badge = {"success": "<span class='pill ok'>Bitrix: Успешно</span>",
                     "fail": "<span class='pill pr'>Bitrix: Отказано</span>",
                     "progress": "<span class='pill py'>Bitrix: в работе</span>"}
        zrows = ""
        for zid, num, comp, sup, amt, m, sk in sorted(rec['z_list'], key=lambda x: -(x[4] or 0))[:30]:
            no = f"№{num}"
            cell = (f"<a href='{BX_PORTAL}/crm/type/{BX_ENTITY}/details/{zid}/' target=_blank>{no}</a>"
                    if BX_PORTAL and zid else no)
            pay = ("<span class='pill ok'>✅ 1С: оплачено</span>" if m
                   else "<span class='pill wait'>⏳ 1С: нет</span>")
            zrows += (f"<tr><td>{cell}</td><td>{comp or '—'}</td><td>{sup or '—'}</td>"
                      f"<td class=num>{money(amt)}</td><td>{_bx_badge.get(sk,'')} {pay}</td></tr>")
        # сматчено по компаниям
        bcrows = ""
        for co, d in sorted(rec['by_company'].items(), key=lambda x: -x[1][1]):
            pct = (100 * d[0] / d[1]) if d[1] else 0
            bcrows += (f"<tr><td>{co or '—'}</td><td class=num>{d[0]}</td><td class=num>{d[1]}</td>"
                       f"<td class=num>{pct:.0f}%</td></tr>")
        # оплата без заявки — платёж есть, № не найден среди заявок Bitrix.
        # Сначала «нигде нет» (реальные подозрительные), потом объяснимые через реестр.
        nzsorted = sorted(rec['pay_no_zayavka'], key=lambda x: (1 if x[6] else 0, -x[3]))
        nz = ""
        for zn, co, nm, a, d, cand, hit in nzsorted[:20]:
            if hit:
                by, rnum, rname = hit
                reestr_cell = (f"<span class='pill ok'>реестр: {by} №{rnum}</span>"
                               + (f" <span style=color:#94a3b8>{rname[:26]}</span>" if rname else ""))
            else:
                reestr_cell = "<span class='pill pr'>нигде нет</span>"
            if cand and BX_PORTAL:
                hint = f"<a href='{BX_PORTAL}/crm/type/{BX_ENTITY}/details/{cand[0]}/' target=_blank>№{cand[1]}</a>"
            elif cand:
                hint = f"№{cand[1]}"
            else:
                hint = "<span style=color:#94a3b8>—</span>"
            nz += (f"<tr><td>{co}</td><td>{nm}</td><td class='num neg'>{money(a)}</td>"
                   f"<td>{d}</td><td>№{zn}</td><td>{reestr_cell}</td><td>{hint}</td></tr>")
        nz = nz or "<tr><td colspan=7 style=color:#94a3b8>нет</td></tr>"
        svedenie = (f"<div class=svh><h2>Сведение: заявки Bitrix ↔ платежи 1С</h2>"
                    f"<div class=svmeta>окно: {BX_MONTHS} мес · заявок из Bitrix: {rec['z_total']} · синхр.: {bx_sync} &nbsp; {refresh_btn}</div></div>"
                    f"<div class='svet wide' style='margin-bottom:10px'>{sv2}</div>"
                    f"{cash_line}"
                    f"<div class=card><table><thead><tr><th>Заявка</th><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Статус</th></tr></thead>"
                    f"<tbody>{zrows}</tbody></table>"
                    f"<div class=note>Клик по № открывает карточку в Bitrix. Показаны 30 из {rec['z_total']} — остальное проверяй в Bitrix напрямую по ссылке.</div></div>"
                    f"<h2>Сматчено по компаниям</h2>"
                    f"<div class=card><table><thead><tr><th>Компания</th><th class=num>Оплачено</th><th class=num>Всего заявок</th><th class=num>%</th></tr></thead>"
                    f"<tbody>{bcrows}</tbody></table><div class=note>Сколько заявок компании уже прошли оплату по 1С.</div></div>"
                    f"<h2>🔴 Оплата без заявки в Bitrix</h2>"
                    f"<div class=cashln style='background:#f0f9ff;border-color:#bae6fd;color:#0c4a6e'>"
                    f"Из {len(rec['pay_no_zayavka'])} платежей без заявки Bitrix: <b>{rec['nz_in_reestr']}</b> нашлись в реестре финотдела "
                    f"(по № или БИН), <b>{rec['nz_orphan']}</b> — нигде нет (это и есть реальные кандидаты на проверку). "
                    f"<span style=color:#94a3b8>Реестр: {rec['reestr_rows']} строк.</span></div>"
                    f"<div class=card><table><thead><tr><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Дата</th><th>№ в назначении</th><th>Реестр финотдела</th><th>Заявка по поставщику</th></tr></thead>"
                    f"<tbody>{nz}</tbody></table>"
                    f"<div class=note>«Нигде нет» = ни в Bitrix, ни в реестре финотдела → проверить в первую очередь. «Реестр: № / БИН» = платёж есть в ручном реестре (легитимен, просто нет заявки Bitrix).</div></div>")
    else:
        svedenie = (f"<div class=note style='margin-top:14px'>Заявки Bitrix ещё не подгружены. "
                    f"{refresh_btn} — один раз, чтобы включить сведение.</div>")

    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8><title>ATAMŪRA · Финансы</title><style>{CSS}</style></head><body>
<div class=top><b>◎ ATAMŪRA · Финансы</b><div class=s>Свод по холдингу · срез: {meta.get('ts','—')} · за {meta.get('months','?')} мес</div></div>
<div class=wrap>
  <div class=kpis>{kpis}</div>
  <h2>Движение денег по дням (отток / приток)</h2>
  <div class=card>{chart_block}</div>
  <div class=svet>{svet}</div>
  {svedenie}
  <h2>Отток по дочкам</h2>
  <div class=card>{oborot}
    <div class=note>Доля оттока по компаниям (топ-7 + прочее). Наведи на сектор — сумма и %.</div></div>
  <h2>Разрез по типу расхода</h2>
  <div class=card>{cat_donut}
    <div class=note>Эвристика по назначению платежа: подряд (работы/СМР) · поставка (материалы/товар) · услуга (аренда/обслуживание/налоги). Ключевые слова можно уточнять.</div></div>
  <h2>Топ поставщиков по оплате</h2>
  <div class=card><div class=bars>{top_bars}</div></div>
  <h2>🔴 Кандидаты в дубли (оплаты поставщикам)</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Дата</th><th class=num>Платежей</th></tr></thead><tbody>{dub_rows}</tbody></table>
    <div class=note>Один БИН + сумма + дата в одной компании — проверить перед/после оплаты.</div></div>
  <h2>⚠ Расхождения: поступило vs оплачено</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Подрядчик</th><th class=num>Поступило</th><th class=num>Оплачено</th><th class=num>Δ</th><th></th></tr></thead><tbody>{disc_rows}</tbody></table>
    <div class=note>Сырой сигнал — без договора не вердикт (аванс/бартер/удержания).</div></div>
  <div class=note>Данные из 1С (OData) → парсер → этот сервер. Дальше: платёжный календарь + сведение с Bitrix-заявками.</div>
</div><div id=cftip class=cftip></div></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def do_GET(self):
        if self.path == "/healthz": self._send("ok", "text/plain")
        elif self.path == "/sync":
            try: self._send(json.dumps(sync_bitrix(), ensure_ascii=False), "application/json")
            except Exception as e: self._send(json.dumps({"error": str(e)}, ensure_ascii=False), "application/json", 500)
        elif self.path == "/refresh":
            try:
                sync_bitrix()
                self.send_response(303); self.send_header("Location", "/"); self.end_headers()
            except Exception as e: self._send(f"<pre>Ошибка синхронизации: {e}</pre>", code=500)
        elif self.path == "/" or self.path.startswith("/?"):
            try: self._send(dashboard())
            except Exception as e: self._send(f"<pre>Ошибка: {e}</pre>", code=500)
        else: self._send("404", code=404)

    def do_POST(self):
        if self.path not in ("/api/ingest", "/api/reestr"):
            self._send("404", code=404); return
        if self.headers.get("X-Service-Key") != KEY:
            self._send('{"error":"bad key"}', "application/json", 401); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            saved = store_reestr(payload) if self.path == "/api/reestr" else store(payload)
            self._send(json.dumps({"ok": True, "saved": saved}), "application/json")
        except Exception as e:
            self._send(json.dumps({"error": str(e)}), "application/json", 500)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True


if __name__ == "__main__":
    print(f"ATAMŪRA Finance ЛК: http://localhost:{PORT}  (приём: POST /api/ingest)")
    Server((HOST, PORT), H).serve_forever()
