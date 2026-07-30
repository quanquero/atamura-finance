# -*- coding: utf-8 -*-
"""
ATAMŪRA Finance — веб-ЛК финдира (приёмник + дашборд).
Принимает срез от парсера (POST /api/ingest, X-Service-Key), кладёт в своё ядро (SQLite),
показывает дашборд: сводка по холдингу / дубли / расхождения / топ поставщиков.
Только стандартная библиотека Python 3. Запуск:  python server.py

Env:  SERVICE_KEY (ключ для /api/ingest),  PORT (по умолч. 8013),  HOST (0.0.0.0 в проде).
"""
import http.server, json, os, re, socketserver, sqlite3, threading, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "finance_core.sqlite3")
PORT = int(os.environ.get("PORT", "8013"))
HOST = os.environ.get("HOST", "127.0.0.1")
KEY  = os.environ.get("SERVICE_KEY", "dev-finance-key")   # в проде задать через env
# Вебхук Bitrix (crm) — финсервер сам тянет Служебные записки (воронка оплат, entityTypeId 178)
BITRIX = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
BX_ENTITY = 178
BX_MONTHS = int(os.environ.get("BX_MONTHS", "3"))   # за сколько месяцев тянуть заявки


def _db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS flow(
        company TEXT, kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS zayavka(
        id INTEGER, number TEXT, title TEXT, supplier TEXT, amount REAL, stage TEXT)""")
    return c


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
    c = _db(); c.execute("DELETE FROM zayavka")
    c.executemany("INSERT INTO zayavka VALUES(?,?,?,?,?,?)", [(
        it.get("id"),
        str(it.get("ufCrm4_1644310716") or "").strip() or _num_from(it.get("title")),
        it.get("title", ""), str(it.get("ufCrm4_1762251054209") or ""),
        float(it.get("opportunity") or 0), it.get("stageId", ""),
    ) for it in rows])
    c.execute("INSERT INTO meta(k,v) VALUES('bx_sync',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (datetime.now().strftime("%Y-%m-%d %H:%M"),))
    c.commit(); n = len(rows); c.close()
    return {"ok": True, "заявок": n}


def _num_from(s):
    """Вытащить № заявки (15xxx–16xxx…) из текста."""
    m = re.search(r"№\s*(\d{4,6})", str(s or "")) or re.search(r"\b(1[0-9]{4})\b", str(s or ""))
    return m.group(1) if m else ""


def reconcile():
    """Сверка: платежи поставщикам (1С) ↔ Служебные записки (Bitrix) по № заявки.
    Возвращает счётчики + примеры: сматчено / оплата без заявки / заявка без оплаты."""
    c = _db()
    pays = c.execute("SELECT company,name,amount,date,purpose,number FROM flow WHERE kind='out' AND supplier=1").fetchall()
    zs = c.execute("SELECT number,supplier,amount,stage,title FROM zayavka").fetchall(); c.close()
    znums = {z[0] for z in zs if z[0]}
    matched, no_zayavka, no_num = [], [], []
    used = set()
    for company, name, amt, date, purpose, num in pays:
        zn = _num_from(purpose)
        if not zn:
            no_num.append((company, name, amt, date))
        elif zn in znums:
            matched.append((zn, company, name, amt)); used.add(zn)
        else:
            no_zayavka.append((zn, company, name, amt, date))
    # заявки на стадии оплаты без найденного платежа
    z_no_pay = [z for z in zs if z[0] and z[0] not in used]
    return {"pays": len(pays), "matched": matched, "no_zayavka": no_zayavka,
            "no_num": no_num, "z_total": len(zs), "z_no_pay": z_no_pay}


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
.note{font-size:11.5px;color:#64748b;padding:8px 16px;background:#fcfcfd;border-top:1px solid #eef2f7}
"""


def dashboard():
    c = _db()
    flow = c.execute("SELECT company,kind,date,bin,name,amount,vidop,supplier,number FROM flow").fetchall()
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

    # обороты по дочкам (отток/приток), бары
    byco = defaultdict(lambda: [0.0, 0.0])
    for r in out: byco[r[0]][0] += r[5]
    for r in inp: byco[r[0]][1] += r[5]
    comps = sorted(byco.items(), key=lambda x: -x[1][0])
    mx = max((max(d) for _, d in comps), default=1) or 1
    bars = ""
    for co, d in comps[:15]:
        wo, wi = max(1, d[0]/mx*100), max(1, d[1]/mx*100)
        bars += (f"<div class=brow><div class=hd><span class=co>{co}</span>"
                 f"<span class=vv>отток {money(d[0])} · приток {money(d[1])}</span></div>"
                 f"<div class=track><div class='fill fo' style='width:{wo:.1f}%'></div></div>"
                 f"<div class=track><div class='fill fi' style='width:{wi:.1f}%'></div></div></div>")

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

    svet = (f"<div class='sv r'><div class=n>{len(dub)}</div><div class=l>Кандидаты в дубли</div></div>"
            f"<div class='sv y'><div class=n>{len(disc)}</div><div class=l>Расхождения (проверить)</div></div>"
            f"<div class='sv g'><div class=n>{len(byco)}</div><div class=l>Компаний в своде</div></div>")

    rec = reconcile()
    if rec.get("z_total"):
        sv2 = (f"<div class='sv g'><div class=n>{len(rec['matched'])}</div><div class=l>Платёж ↔ заявка</div></div>"
               f"<div class='sv r'><div class=n>{len(rec['no_zayavka'])}</div><div class=l>Оплата без заявки</div></div>"
               f"<div class='sv y'><div class=n>{len(rec['z_no_pay'])}</div><div class=l>Заявка без оплаты</div></div>")
        nz = "".join(f"<tr><td>{co}</td><td>{nm}</td><td class='num neg'>{money(a)}</td><td>{d}</td><td>{zn}</td></tr>"
                     for zn, co, nm, a, d in sorted(rec['no_zayavka'], key=lambda x: -x[3])[:12]) or "<tr><td colspan=5 style=color:#94a3b8>нет</td></tr>"
        svedenie = (f"<h2>Сведение: платежи 1С ↔ заявки Bitrix</h2>"
                    f"<div class=svet style='margin-bottom:14px'>{sv2}</div>"
                    f"<div class=card><table><thead><tr><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Дата</th><th>№ заявки</th></tr></thead>"
                    f"<tbody>{nz}</tbody></table><div class=note>«Оплата без заявки» — платёж есть, а заявки Bitrix с таким № нет. Проверить основание.</div></div>")
    else:
        svedenie = "<div class=note style='margin-top:14px'>Заявки Bitrix не подгружены — открой <b>/sync</b> один раз, чтобы включить сведение.</div>"

    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8><title>ATAMŪRA · Финансы</title><style>{CSS}</style></head><body>
<div class=top><b>◎ ATAMŪRA · Финансы</b><div class=s>Свод по холдингу · срез: {meta.get('ts','—')} · за {meta.get('months','?')} мес</div></div>
<div class=wrap>
  <div class=kpis>{kpis}</div>
  <div class=svet>{svet}</div>
  {svedenie}
  <h2>Обороты по дочкам</h2>
  <div class=card><div class=bars>{bars}</div>
    <div class=lg><span class=k><span class=sw style='background:#ea580c'></span>отток</span><span class=k><span class=sw style='background:#0891b2'></span>приток</span></div></div>
  <h2>Топ поставщиков по оплате</h2>
  <div class=card><div class=bars>{top_bars}</div></div>
  <h2>🔴 Кандидаты в дубли (оплаты поставщикам)</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Дата</th><th class=num>Платежей</th></tr></thead><tbody>{dub_rows}</tbody></table>
    <div class=note>Один БИН + сумма + дата в одной компании — проверить перед/после оплаты.</div></div>
  <h2>⚠ Расхождения: поступило vs оплачено</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Подрядчик</th><th class=num>Поступило</th><th class=num>Оплачено</th><th class=num>Δ</th><th></th></tr></thead><tbody>{disc_rows}</tbody></table>
    <div class=note>Сырой сигнал — без договора не вердикт (аванс/бартер/удержания).</div></div>
  <div class=note>Данные из 1С (OData) → парсер → этот сервер. Дальше: платёжный календарь + сведение с Bitrix-заявками.</div>
</div></body></html>"""


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
        elif self.path == "/" or self.path.startswith("/?"):
            try: self._send(dashboard())
            except Exception as e: self._send(f"<pre>Ошибка: {e}</pre>", code=500)
        else: self._send("404", code=404)

    def do_POST(self):
        if self.path != "/api/ingest":
            self._send("404", code=404); return
        if self.headers.get("X-Service-Key") != KEY:
            self._send('{"error":"bad key"}', "application/json", 401); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            saved = store(payload)
            self._send(json.dumps({"ok": True, "saved": saved}), "application/json")
        except Exception as e:
            self._send(json.dumps({"error": str(e)}), "application/json", 500)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True


if __name__ == "__main__":
    print(f"ATAMŪRA Finance ЛК: http://localhost:{PORT}  (приём: POST /api/ingest)")
    Server((HOST, PORT), H).serve_forever()
