# -*- coding: utf-8 -*-
"""Ручная проверка контрагента по БИН через Adata (запрос с сервера).

    python3 tools/check_bin.py 240440035749          # печать сводки в терминал
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
    flag("Действующая", not s.get("company_status"), "" if s.get("company_status") else "СТАТУС НЕ ДЕЙСТВУЮЩИЙ")
    flag("Банкротство", s.get("bankcrupt"))
    flag("Налоговая задолженность", bool(s.get("tax_debt")), (_m(s.get("tax_debt")) + " ₸") if s.get("tax_debt") else "")
    flag("Нарушения по налогам", s.get("violation_tax"))
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
    if "--html" in args:
        os.makedirs("out", exist_ok=True)
        p = os.path.join("out", "spravka_%s.html" % bn)
        open(p, "w", encoding="utf-8").write(adata.render_html(info))
        print("\n  💾 Полная HTML-справка: %s" % p)


if __name__ == "__main__":
    main()
