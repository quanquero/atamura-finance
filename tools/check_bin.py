# -*- coding: utf-8 -*-
"""Ручная проверка контрагента по БИН через Adata (запрос с сервера).

    python3 tools/check_bin.py 240440035749          # печать сводки в терминал
    python3 tools/check_bin.py 240440035749 --sb      # + карточка СБ (риски/суды/санкции/лицензии)
    python3 tools/check_bin.py 240440035749 --full    # + прогон всех модулей Adata (что доступно)
    python3 tools/check_bin.py 240440035749 --html    # + сохранить полную HTML-справку в out/

Требует ADATA_TOKEN в окружении/.env (см. adata.py)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import adata


def _m(x):
    try:
        return format(int(round(x or 0)), ",d").replace(",", " ")
    except Exception:
        return str(x)


def main():
    args = sys.argv[1:]
    bins = [a for a in args if a.replace(" ", "").isdigit()]
    if not bins:
        print("Использование: python3 tools/check_bin.py <БИН> [--html]")
        return
    bn = bins[0].replace(" ", "")
    print("Запрос в Adata по БИН %s … (двухшаговый: заказ → поллинг, несколько секунд)" % bn)
    info = adata.fetch(bn)
    if not info or info.get("error"):
        print("✕ Не удалось получить справку:", (info or {}).get("error", "пусто"))
        print("  Проверь: ADATA_TOKEN задан? БИН верный (12 цифр)? Сеть до api.adata.kz есть?")
        return
    # отдельный модуль status — даёт сильные флаги СБ, которых нет в сводке info
    st = adata.fetch(bn, dtype="status")
    st = st if isinstance(st, dict) and not st.get("error") else {}
    b = info.get("basic", {}); s = info.get("status", {})
    rf = info.get("riskFactor", {}).get("company", {}); req = info.get("requisites", {})
    print("=" * 60)
    print(" %s" % (b.get("short_name") or b.get("name_ru", "").title() or "—"))
    print("=" * 60)
    print("  БИН/ИИН:      %s" % b.get("biin", bn))
    print("  Директор:     %s" % (b.get("fullname_director") or "—"))
    print("  ОКЭД:         %s (%s)" % (b.get("oked") or "—", b.get("oked_id") or "—"))
    print("  Сотрудников:  %s" % (b.get("employee_count") or "—"))
    print("  НДС:          %s" % ("да (общий режим)" if b.get("is_nds_payer") else "нет / упрощёнка"))
    print("  Адрес:        %s" % (b.get("legal_address") or "—"))
    print("\n  🚦 ФЛАГИ СБ:")
    def flag(name, bad, extra=""):
        print("     %s %s%s" % ("🔴" if bad else "🟢", name, (" · " + extra) if extra else ""))
    flag("Действующая", not (st.get("company_status", s.get("company_status"))),
         "" if st.get("company_status", s.get("company_status")) else "СТАТУС НЕ ДЕЙСТВУЮЩИЙ")
    flag("Лжепредприятие (в списке)", st.get("pseudo_company"))
    flag("Бездействующее", st.get("inactive"))
    flag("Отсутствует по юр.адресу", st.get("absent_at_address"))
    flag("Регистрация недействительна", st.get("registration_invalid"))
    flag("Банкротство", st.get("bankcrupt", s.get("bankcrupt")))
    tdebt = st.get("tax_debt", s.get("tax_debt"))
    flag("Налоговая задолженность", bool(tdebt), (_m(tdebt) + " ₸") if tdebt else "")
    flag("Нарушения по налогам (реорг.)", st.get("violation_tax", s.get("violation_tax")))
    flag("Арест счетов", rf.get("seized_bank_account"))
    flag("Арест имущества", rf.get("seized_property"))
    flag("Исполнительное производство", rf.get("enforcement_debt"))
    flag("Недобросовестный госзакуп", b.get("unreliable_zakup"))
    if rf.get("tax_risk_degree"):
        print("     🟡 Степень налог. риска: %s" % rf.get("tax_risk_degree"))
    if req.get("iik"):
        print("\n  Реквизиты:    %s · %s · БИК %s" % (req.get("iik", ""), req.get("bank", ""), req.get("bik", "")))
    aff = info.get("connectedDiagram", {}).get("affiliation_by_head", {}).get("companies", [])
    if aff:
        print("\n  Аффилированные (по руководителю):")
        for c in aff[:8]:
            print("     · %s — %s %s" % (c.get("name", ""), c.get("type", ""), c.get("bin", "")))
    if "--sb" in args:
        print("\n  🛡️  КАРТОЧКА СБ (модули благонадёжности Adata) … (несколько запросов, подожди)")
        card = adata.sb_card(bn)
        bad = [f for f in card["flags"] if f["bad"]]
        print("     Лицензий: %s · Режим налогов: %s · Суды (гр/уг/адм): %d/%d/%d" % (
            card["licenses"] or "—", card["tax_mode"] or "—",
            card["courts"]["civil"], card["courts"]["criminal"], card["courts"]["admin"]))
        if bad:
            print("     🔴 КРАСНЫЕ ФЛАГИ (%d):" % len(bad))
            for f in bad:
                print("        🔴 %s%s" % (f["name"], (" · " + f["extra"]) if f["extra"] else ""))
        else:
            print("     🟢 красных флагов по благонадёжности не найдено")
        # санкции — по названию компании
        nm = b.get("short_name") or b.get("name_ru", "")
        if nm:
            try:
                sanc = adata.fetch_sanctions(nm)
                dets = (sanc or {}).get("details", []) if isinstance(sanc, dict) else []
                hits = [d for d in dets if (d.get("percentage") or d.get("highest_percentage") or 0) >= 85]
                print("     %s Санкционные списки: %s" % ("🔴" if hits else "🟢",
                      ("совпадений %d (проверить!)" % len(hits)) if hits else "не найдено"))
            except Exception as e:
                print("     ⚠ санкции: %s" % str(e)[:80])
    if "--full" in args:
        print("\n  📦 ВСЕ МОДУЛИ ADATA по БИН (что доступно на аккаунте):")
        mods = adata.fetch_modules(bn)
        for slug, data in mods.items():
            name = adata.MODULES.get(slug, slug)
            ok = isinstance(data, dict) and not data.get("error")
            n = len(data) if isinstance(data, dict) else 0
            print("     %s %-26s %s" % ("🟢" if ok else "🔴", slug,
                                        ("(%d полей)" % n) if ok else ("— " + str((data or {}).get("error", "")))))
    if "--html" in args:
        os.makedirs("out", exist_ok=True)
        p = os.path.join("out", "spravka_%s.html" % bn)
        open(p, "w", encoding="utf-8").write(adata.render_html(info))
        print("\n  💾 Полная HTML-справка: %s" % p)


if __name__ == "__main__":
    main()
