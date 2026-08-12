# -*- coding: utf-8 -*-
"""Авто-синхра Bitrix для фин-ядра — БЕЗ HTTP/авторизации (эндпоинты /sync под входом).
Вызывает функции server.py напрямую в том же процессе; BITRIX_WEBHOOK берётся из .env
(server._load_env_file подтягивает при импорте). Запускается systemd-таймером на finance-сервере.

    python3 tools/sync_bitrix.py            # ВСЕ заявки 178 с названиями → zayavka (для БДДС/матчинга)
    python3 tools/sync_bitrix.py --window   # только окно BX_MONTHS (быстро, если нужно)
    python3 tools/sync_bitrix.py --full     # + индекс № → id ИНКРЕМЕНТНО (только новые) — раз в сутки
    python3 tools/sync_bitrix.py --rebuild  # разовый полный пересбор индекса с нуля
"""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


def main():
    rebuild = "--rebuild" in sys.argv
    full = rebuild or "--full" in sys.argv
    window = "--window" in sys.argv
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = f"окно {S.BX_MONTHS} мес" if window else "ВСЕ заявки"
    print(f"[{ts}] sync_bitrix ({tag}): {S.sync_bitrix(full=not window)}")
    if full:
        t2 = "полный пересбор" if rebuild else "инкремент"
        print(f"[{ts}] sync_bitrix_full ({t2}): {S.sync_bitrix_full(rebuild=rebuild)}")


if __name__ == "__main__":
    main()
