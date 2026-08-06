# -*- coding: utf-8 -*-
"""Авто-синхра Bitrix для фин-ядра — БЕЗ HTTP/авторизации (эндпоинты /sync под входом).
Вызывает функции server.py напрямую в том же процессе; BITRIX_WEBHOOK берётся из .env
(server._load_env_file подтягивает при импорте). Запускается systemd-таймером на finance-сервере.

    python3 tools/sync_bitrix.py          # окно заявок (воронка 178 за BX_MONTHS) — часто (каждые N ч)
    python3 tools/sync_bitrix.py --full   # + полный индекс № → id (вся история) — раз в сутки ночью
"""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


def main():
    full = "--full" in sys.argv
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] sync_bitrix (окно {S.BX_MONTHS} мес): {S.sync_bitrix()}")
    if full:
        print(f"[{ts}] sync_bitrix_full (полный индекс): {S.sync_bitrix_full()}")


if __name__ == "__main__":
    main()
