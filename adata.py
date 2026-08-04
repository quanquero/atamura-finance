# -*- coding: utf-8 -*-
"""Клиент Adata: справка по контрагенту (БИН) + рендер HTML.
   Двухшаговый поток: Get Request -> request-token -> Check Request (опрос).
   Токен авторизации — из env ADATA_TOKEN (или .env). В git не коммитим."""
import urllib.request, ssl, json, time, os

def _token():
    t = os.environ.get("ADATA_TOKEN")
    if not t:
        p = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.startswith("ADATA_TOKEN="):
                    t = line.split("=", 1)[1].strip()
    return t

_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=25, context=_CTX).read().decode("utf-8", "replace"))

def fetch(bin_, dtype="info", tries=8, delay=2):
    """Возвращает dict данных Adata по БИН (по умолчанию 'info' — сводная справка)."""
    T = _token()
    if not T: raise RuntimeError("ADATA_TOKEN не задан (env или .env)")
    g = _get("https://api.adata.kz/api/company/%s/%s?iinBin=%s" % (dtype, T, bin_))
    rt = g.get("token")
    if not rt: return {"error": g.get("message", "нет request-токена")}
    for _ in range(tries):
        time.sleep(delay)
        c = _get("https://api.adata.kz/api/company/%s/check/%s?token=%s" % (dtype, T, rt))
        if c.get("data"): return c["data"]
    return {"error": "данные ещё готовятся (timeout)"}

# ---------- рендер справки ----------
def _money(x): return format(int(round(x or 0)), ",d").replace(",", " ")
def _yn(v): return "🔴 да" if v else "🟢 нет"

def render_checklist(info, work="", zakaz=""):
    """10-пунктовая проверка контрагента (Adata + наши проверки). Возвращает список (#, пункт, статус, отдел)."""
    b = info.get("basic", {}); s = info.get("status", {})
    oked = b.get("oked", ""); oid = str(b.get("oked_id", "")); emp = b.get("employee_count")
    nds = b.get("is_nds_payer"); tax_debt = s.get("tax_debt"); bankrupt = s.get("bankcrupt")
    build = oid[:2] in ("41", "42", "43")
    return [
        (1, "Стоимость услуг vs эталон/рынок", "🟡 сверить с КП", "Снабжение/ПТО"),
        (2, "ОКЭД соответствует работам", ("🟢 %s (%s)" % (oked, oid)) if build else ("🟡 %s — проверить" % oked), "Юр"),
        (3, "Лицензируемое / лицензия", "🟡 проверить (СМР)", "Юр"),
        (4, "Кол-во сотрудников", ("🟢 %s" % emp) if emp else "🟡 нет данных", "СБ"),
        (5, "Режим налогообложения", "🟢 плательщик НДС (общий)" if nds else "🟡 не НДС — упрощёнка?", "Фин"),
        (6, "Лимиты (если не НДСник)", "🟢 НДСник — лимиты н/п" if nds else "🟡 проверить оборот-лимит", "Фин"),
        (7, "Наименование услуг (если без лиц.)", "🟡 как назвать (вспомог. работы?)", "Юр/ПТО"),
        (8, "С какой дочкой (группы) работает", ("🟢 %s" % zakaz) if zakaz else "🟡 из заявки", "—"),
        (9, "Благонадёжность (СБ)", ("🔴 налог.долг %s ₸" % _money(tax_debt)) if tax_debt else ("🔴 банкрот" if bankrupt else "🟢 чисто"), "СБ"),
        (10, "Сроки выполнения работ", "🔴 дата окончания не заполнена" if not work else ("🟢 " + work), "ПТО"),
    ]

def render_html(info, work="", zakaz=""):
    b = info.get("basic", {}); s = info.get("status", {})
    rf = info.get("riskFactor", {}).get("company", {}); req = info.get("requisites", {})
    risks = [
        ("Статус — действующая", "🟢 да" if s.get("company_status") else "🔴 нет"),
        ("Банкротство", _yn(s.get("bankcrupt"))),
        ("Налоговая задолженность", ("🔴 " + _money(s["tax_debt"]) + " ₸") if s.get("tax_debt") else "🟢 нет"),
        ("Нарушения по налогам", _yn(s.get("violation_tax"))),
        ("Фин. проблемы", _yn(b.get("financial_problems"))),
        ("Недобросовестный госзакуп", _yn(b.get("unreliable_zakup"))),
        ("Степень налог. риска", ("🟡 " + rf.get("tax_risk_degree", "")) if rf.get("tax_risk_degree") else "—"),
        ("Аресты счетов/имущества", _yn(rf.get("seized_bank_account") or rf.get("seized_property"))),
        ("Исполнит. производство", _yn(rf.get("enforcement_debt"))),
    ]
    tax = info.get("taxDeductions", {}).get("details", [])[:6]
    aff = info.get("connectedDiagram", {}).get("affiliation_by_head", {}).get("companies", [])
    def kv(k, v): return '<div class=kv><span class=k>%s</span><span class=v>%s</span></div>' % (k, v)
    h = ['<style>*{box-sizing:border-box;font-family:-apple-system,Segoe UI,Arial}.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden}.hd{background:#0f2233;color:#e2e8f0;padding:14px 18px}.hd .t{font-weight:700;font-size:16px}.hd .s{font-size:12px;color:#94a3b8}.sec{padding:12px 18px;border-top:1px solid #eef2f7}.sec h3{font-size:12px;text-transform:uppercase;color:#64748b;margin:0 0 8px}.kv{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;border-bottom:1px solid #f4f6f9}.kv .k{color:#64748b}.kv .v{font-weight:600;text-align:right}.tax span{background:#f1f5f9;border-radius:6px;padding:3px 8px;font-size:11px;margin:2px;display:inline-block}.note{font-size:11px;color:#94a3b8;padding:10px 18px}</style>']
    h.append('<div class=card><div class=hd><div class=t>Справка по контрагенту · %s</div><div class=s>БИН %s · %s (%s) · Adata</div></div>' % (b.get("short_name",""), b.get("biin",""), b.get("oked",""), b.get("oked_id","")))
    h.append('<div class=sec><h3>Основное</h3>')
    for k, v in [("Полное наименование", b.get("name_ru","").title()), ("Директор", b.get("fullname_director","")),
                 ("Юр. адрес", b.get("legal_address","")), ("Форма", b.get("legal_form","")),
                 ("Регистрация", b.get("date_registration","")), ("Сотрудников", str(b.get("employee_count",""))),
                 ("Плательщик НДС", "🟢 да" if b.get("is_nds_payer") else "🔴 нет"), ("Размер (КРП)", b.get("krp",""))]:
        h.append(kv(k, v))
    h.append('</div><div class=sec><h3>🚦 Проверка / риски</h3>')
    for k, v in risks: h.append(kv(k, v))
    h.append('</div><div class=sec><h3>Реквизиты (из Adata)</h3>')
    for k, v in [("ИИК", req.get("iik","")), ("Банк", req.get("bank","")), ("БИК", req.get("bik","")), ("КБе", str(req.get("kbe_code","")))]:
        h.append(kv(k, v))
    h.append('</div><div class=sec><h3>Налоговые отчисления</h3><div>')
    for d in tax:
        yr = d.get("year")
        if yr is None:
            continue
        h.append('<span class=tax><span>%s: %s ₸</span></span>' % (yr, _money(d.get("amount"))))
    h.append('</div></div><div class=sec><h3>Аффилированность (по руководителю)</h3>')
    for c in aff: h.append(kv(c.get("name",""), "%s · %s" % (c.get("type",""), c.get("bin",""))))
    # 10-пунктовая проверка контрагента
    h.append('</div><div class=sec><h3>✅ Проверка контрагента (10 пунктов)</h3>')
    for n, name, st, dep in render_checklist(info, work, zakaz):
        h.append('<div class=kv><span class=k>%d. %s <b style="color:#94a3b8;font-weight:500">· %s</b></span><span class=v>%s</span></div>' % (n, name, dep, st))
    h.append('</div><div class=note>Источник: Adata · %s · проверка: Adata (ОКЭД, сотрудники, НДС, благонадёжность) + наши (цена, лимиты, наименование, сроки)</div></div>' % b.get("source_link",""))
    return "".join(h)

if __name__ == "__main__":
    import sys
    bn = sys.argv[1] if len(sys.argv) > 1 else "150140020969"
    data = fetch(bn)
    open("out/spravka_%s.html" % bn, "w", encoding="utf-8").write(render_html(data))
    print("saved out/spravka_%s.html" % bn)
