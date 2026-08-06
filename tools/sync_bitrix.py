# -*- coding: utf-8 -*-
"""Авто-синхра Bitrix для фин-ядра — БЕЗ HTTP/авторизации (эндпоинты /sync под входом).
Вызывает функции server.py напрямую в том же процессе; BITRIX_WEBHOOK берётся из .env
(server._load_env_file подтягивает при импорте). Запускается systemd-таймером на finance-сервере.

    python3 tools/sync_bitrix.py            # окно заявок (воронка 178 за BX_MONTHS) — часто (каждые N ч)
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
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] sync_bitrix (окно {S.BX_MONTHS} мес): {S.sync_bitrix()}")
    if full:
        tag = "полный пересбор" if rebuild else "инкремент"
        print(f"[{ts}] sync_bitrix_full ({tag}): {S.sync_bitrix_full(rebuild=rebuild)}")


if __name__ == "__main__":
    main()
