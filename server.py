# -*- coding: utf-8 -*-
"""
ATAMŪRA Finance — веб-ЛК финдира (приёмник + дашборд).
Принимает срез от парсера (POST /api/ingest, X-Service-Key), кладёт в своё ядро (SQLite),
показывает дашборд: сводка по холдингу / дубли / расхождения / топ поставщиков.
Только стандартная библиотека Python 3. Запуск:  python server.py

Env:  SERVICE_KEY (ключ для /api/ingest),  PORT (по умолч. 8013),  HOST (0.0.0.0 в проде).
"""
import http.server, json, os, socketserver, sqlite3, threading
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "finance_core.sqlite3")
PORT = int(os.environ.get("PORT", "8013"))
HOST = os.environ.get("HOST", "127.0.0.1")
KEY  = os.environ.get("SERVICE_KEY", "dev-finance-key")   # в проде задать через env


def _db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS flow(
        company TEXT, kind TEXT, doc TEXT, number TEXT, date TEXT, bin TEXT, name TEXT,
        amount REAL, vidop TEXT, supplier INT, purpose TEXT, comment TEXT, dogovor_key TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c


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
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px}
.kpi .l{font-size:10.5px;color:#64748b;text-transform:uppercase}.kpi .v{font-size:20px;font-weight:800;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi.warn .v{color:#dc2626}.kpi.good .v{color:#166534}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:16px}
.card h3{font-size:14px;margin:0;padding:12px 16px;border-bottom:1px solid #eef2f7;background:#f8fafc}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#f1f5f9;color:#475569;text-align:left;padding:8px 10px;font-size:11px;border-bottom:1px solid #e2e8f0}
td{padding:8px 10px;border-bottom:1px solid #eef2f7}tr:last-child td{border-bottom:0}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.b{font-weight:700}.neg{color:#b91c1c;font-weight:700}.red{color:#b91c1c}
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

    out_t, in_t, done_t = sum(r[5] for r in out), sum(r[5] for r in inp), sum(r[5] for r in rec)
    sup_t = sum(r[5] for r in sup)

    kpis = "".join(f"<div class='kpi {g}'><div class=l>{l}</div><div class=v>{v}</div></div>" for v,l,g in [
        (money(out_t)+" ₸","Отток (исходящие)","warn"),(money(in_t)+" ₸","Приток (входящие)","good"),
        (money(sup_t)+" ₸","Оплаты поставщикам",""),(money(done_t)+" ₸","Выполнено (АВР)","")])

    # по компаниям
    byco = defaultdict(lambda: [0.0,0.0])
    for r in out: byco[r[0]][0]+=r[5]
    for r in inp: byco[r[0]][1]+=r[5]
    co_rows = "".join(f"<tr><td>{co}</td><td class=num>{money(d[0])}</td><td class=num>{money(d[1])}</td></tr>"
                      for co,d in sorted(byco.items(), key=lambda x:-x[1][0]))

    # дубли: company+bin+amount+date, >1
    g = defaultdict(list)
    for r in sup:
        if r[3]: g[(r[0],r[3],round(r[5],2),r[2])].append(r)
    dub = {k:v for k,v in g.items() if len(v)>1}
    dub_rows = "".join(f"<tr><td>{v[0][0]}</td><td>{v[0][4]}</td><td class='num neg'>{money(a)}</td><td>{d}</td><td class=num>{len(v)}</td></tr>"
                       for (co,b,a,d),v in sorted(dub.items(),key=lambda x:-x[0][2])) or "<tr><td colspan=5 style=color:#94a3b8>Дублей не найдено</td></tr>"

    # расхождения (выполнено vs выплачено по company+bin)
    byc = defaultdict(lambda:[0.0,0.0,""])
    for r in sup:
        if r[3]: byc[(r[0],r[3])][1]+=r[5]; byc[(r[0],r[3])][2]=r[4]
    for r in rec:
        if r[3]:
            byc[(r[0],r[3])][0]+=r[5]
            if not byc[(r[0],r[3])][2]: byc[(r[0],r[3])][2]=r[4]
    disc = sorted(((abs(d[0]-d[1]),co,d[2],d[0],d[1],d[0]-d[1]) for (co,b),d in byc.items()), reverse=True)[:15]
    disc_rows = ""
    for _,co,nm,done,paid,diff in disc:
        pcls = "pr" if diff < -1 else "py"
        plabel = "оплата&gt;поступл" if diff < -1 else "поступл&gt;оплата"
        disc_rows += (f"<tr><td>{co}</td><td>{nm}</td><td class=num>{money(done)}</td>"
                      f"<td class=num>{money(paid)}</td><td class='num b'>{money(diff)}</td>"
                      f"<td><span class='pill {pcls}'>{plabel}</span></td></tr>")

    # топ поставщиков
    tot=defaultdict(float); nm={}
    for r in sup:
        k=r[3] or r[4]; tot[k]+=r[5]; nm[k]=r[4]
    top_rows="".join(f"<tr><td>{nm[k]}</td><td class='num b'>{money(s)}</td></tr>"
                     for k,s in sorted(tot.items(),key=lambda x:-x[1])[:12])

    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8><title>ATAMŪRA · Финансы</title><style>{CSS}</style></head><body>
<div class=top><b>◎ ATAMŪRA · Финансы</b><div class=s>Свод по холдингу · срез: {meta.get('ts','—')} · за {meta.get('months','?')} мес · компаний: {len(byco)}</div></div>
<div class=wrap>
  <div class=kpis>{kpis}</div>
  <h2>По компаниям</h2>
  <div class=card><table><thead><tr><th>Компания</th><th class=num>Отток</th><th class=num>Приток</th></tr></thead><tbody>{co_rows}</tbody></table></div>
  <h2>🔴 Кандидаты в дубли (оплаты поставщикам)</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Поставщик</th><th class=num>Сумма</th><th>Дата</th><th class=num>Платежей</th></tr></thead><tbody>{dub_rows}</tbody></table>
    <div class=note>Один БИН + сумма + дата в одной компании. Проверить перед/после оплаты.</div></div>
  <h2>⚠ Расхождения: выполнено vs оплачено (сырой сигнал — вердикт с договором)</h2>
  <div class=card><table><thead><tr><th>Компания</th><th>Подрядчик</th><th class=num>Поступило</th><th class=num>Оплачено</th><th class=num>Δ</th><th></th></tr></thead><tbody>{disc_rows}</tbody></table>
    <div class=note>Без договора это не переплата/долг — норму (аванс/бартер/удержания) знает только договор. Флаг = «посмотреть».</div></div>
  <h2>Топ поставщиков по оплате</h2>
  <div class=card><table><thead><tr><th>Поставщик</th><th class=num>Оплачено</th></tr></thead><tbody>{top_rows}</tbody></table></div>
  <div class=note>Данные из 1С по OData, собираются парсером и присылаются на этот сервер. Обновляется при каждом прогоне.</div>
</div></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def do_GET(self):
        if self.path == "/healthz": self._send("ok", "text/plain")
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
