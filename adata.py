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

# Модули Adata: slug для Get Request → человеческое имя. Check у ВСЕХ общий: company/info/check
# (в доке check для basic/status/tax-dynamics = info/check). Добавлять модули — одной строкой.
MODULES = {
    "info":                   "Сводная информация",
    "basic":                  "Базовая информация",
    "status":                 "Статус предприятия (лжепредприятие/банкрот/нет по адресу/налог.долг)",
    "riskfactor":             "Факторы риска (предприятие + руководитель)",
    "trustworthy-extended":   "Неблагонадёжные реестры",
    "trustworthy-plus":       "Расширенные признаки благонадёжности",
    "rehab-bankruptcy":       "Реабилитация и банкротство",
    "risk-check":             "Реестр проверок и имущества",
    "courtcase":              "Судебные разбирательства (сводная)",
    "license":                "Лицензии (сводная)",
    "licenses-certificates":  "Сертификаты",
    "tax-mode":               "Режим налогообложения",
    "tax":                    "Сводная информация по налогам",
    "tax-deduction/dynamics": "Налоги в динамике по годам",
    "tax-debt/details":       "Задолженность по налогам (детально)",
}
# Санкции — ОСОБЫЙ модуль: по НАЗВАНИЮ (keyword), одношаговый (без check). См. fetch_sanctions().


def fetch(bin_, dtype="info", tries=8, delay=2):
    """Данные Adata по БИН для одного модуля. Get Request = company/{dtype}/…,
    Check Request = company/info/check/… (общий для всех модулей)."""
    T = _token()
    if not T: raise RuntimeError("ADATA_TOKEN не задан (env или .env)")
    g = _get("https://api.adata.kz/api/company/%s/%s?iinBin=%s" % (dtype, T, bin_))
    rt = g.get("token")
    if not rt: return {"error": g.get("message", "нет request-токена")}
    for _ in range(tries):
        time.sleep(delay)
        c = _get("https://api.adata.kz/api/company/info/check/%s?token=%s" % (T, rt))
        if c.get("data"): return c["data"]           # 200 «Данные готовятся» → data пусто, ждём и повторяем
    return {"error": "данные ещё готовятся (timeout)"}


def fetch_modules(bin_, modules=None):
    """Несколько модулей по БИН → {slug: data|{'error':…}}. По умолчанию все из MODULES."""
    out = {}
    for m in (modules or list(MODULES)):
        try:
            out[m] = fetch(bin_, dtype=m)
        except Exception as e:
            out[m] = {"error": str(e)[:200]}
    return out


def fetch_sanctions(keyword, page=1):
    """Санкции — по НАЗВАНИЮ компании (не БИН), ОДНОШАГОВЫЙ (без check-поллинга)."""
    import urllib.parse
    T = _token()
    if not T:
        raise RuntimeError("ADATA_TOKEN не задан")
    r = _get("https://api.adata.kz/api/company/sanction/%s?keyword=%s&page=%d" %
             (T, urllib.parse.quote(str(keyword or "")), page))
    return r.get("data", r) if isinstance(r, dict) else r


def _ok(x):
    return x if isinstance(x, dict) and not x.get("error") else {}


def sb_card(bin_):
    """Карточка службы безопасности по БИН: тянет ключевые модули благонадёжности и собирает флаги.
    ⚠ несколько запросов с поллингом — займёт несколько секунд."""
    rf = _ok(fetch(bin_, "riskfactor"))
    tr = _ok(fetch(bin_, "trustworthy-extended"))
    st = _ok(fetch(bin_, "status"))
    cc = _ok(fetch(bin_, "courtcase"))
    lic = _ok(fetch(bin_, "license"))
    tm = _ok(fetch(bin_, "tax-mode"))
    co = rf.get("company", {}) if isinstance(rf.get("company"), dict) else {}
    hd = rf.get("head", {}) if isinstance(rf.get("head"), dict) else {}
    flags = []

    def F(name, bad, extra=""):
        flags.append({"name": name, "bad": bool(bad), "extra": extra})

    # предприятие
    F("Не действующая", st.get("company_status") is False)
    F("Лжепредприятие", st.get("pseudo_company"))
    F("Сделки без факт. выполнения (фиктив)", co.get("irresponsible_taxpayer") or tr.get("workless_taxpayer"))
    F("Банкрот / ликвидация", co.get("bankrupt") or co.get("bankruptcy_decision") or co.get("liquidating_taxpayer"))
    F("Реабилитация (банкротство)", co.get("bankruptcy_rehabilitation") or tr.get("rehabilitation"))
    F("Арест счетов", co.get("seized_bank_account"))
    F("Арест имущества", co.get("seized_property") or tr.get("transport_arrest"))
    F("Исполнительное производство", co.get("enforcement_debt"),
      (_money(co.get("enforcement_debt_sum")) + " ₸") if co.get("enforcement_debt_sum") else "")
    F("Налоговая задолженность", st.get("tax_debt") or tr.get("tax_arrears_150"),
      (_money(st.get("tax_debt")) + " ₸") if st.get("tax_debt") else "")
    F("Высокая степень налог. риска", str(co.get("tax_risk_degree", "")).lower() in ("высокая", "high", "красная"),
      co.get("tax_risk_degree", ""))
    F("Нет по юр. адресу", tr.get("wrong_address"))
    F("Ограничение выписки ЭСФ", tr.get("esf_bounded") or tr.get("esf_withdrawn") or tr.get("esf_suspended"))
    F("Налоговая проверка в этом году", tr.get("first_half_tax_inspection") or tr.get("second_half_tax_inspection"))
    # руководитель — тяжёлые
    F("Рук.: терроризм/экстремизм", hd.get("terrorist") or hd.get("terrorism_involved"))
    F("Рук.: в розыске", hd.get("citizen_hiding_from_investigation"))
    F("Рук.: педофил/алименты/пропал без вести", hd.get("pedophile") or hd.get("alimony_payer") or hd.get("missing"))
    F("Рук.: запрет на выезд", hd.get("ban_leaving"))
    F("Рук.: проблемные компании (тот же директор)", tr.get("same_director_problem_company"))
    return {
        "flags": flags,
        "licenses": lic.get("total_licenses_count", 0),
        "tax_mode": tm.get("tax_mode", ""),
        "courts": {"civil": cc.get("total_civil_count", 0),
                   "criminal": cc.get("total_criminal_count", 0),
                   "admin": cc.get("total_administrative_count", 0)},
    }

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
