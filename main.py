"""kelbot 진입점."""
from __future__ import annotations

import asyncio
import logging
import os

import sentry_sdk

from bot.manager import run_bot
from config.logging_config import setup_logging
from config.settings import SETTINGS  # import 시점에 load_dotenv() 실행

# 장애 추적. settings import로 .env가 로드된 뒤여야 DSN 값을 읽을 수 있음
# DSN이 비어 있으면 transport가 없어 어디로도 전송되지 않는다.
def _sentry_before_send(event, hint):
    """discord 재연결 과정의 일시적 네트워크 에러는 Sentry로 보내지 않는다."""
    if event.get("logger") not in ("discord.client", "discord.gateway"):
        return event
    exc_info = hint.get("exc_info")
    if exc_info:
        name = getattr(exc_info[0], "__name__", "")
        msg = str(exc_info[1])
        if name in ("TimeoutError", "ConnectTimeoutError", "ReadTimeout", "ConnectionError", "ClientConnectorError", "ClientOSError", "ServerDisconnectedError", "WSServerHandshakeError", "ConnectionClosed", "ConnectionResetError"):
            return None
        for _t in ("Connection timeout", "Cannot connect to host", "Temporary failure in name resolution", "네트워크 오류", "연결 중 오류"):
            if _t in msg:
                return None
    return event


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    traces_sample_rate=0.1,
    before_send=_sentry_before_send,
)


def main() -> None:
    setup_logging()
    logger = logging.getLogger("kel-bot")
    logger.info("kelbot 시작")
    asyncio.run(run_bot(SETTINGS))


if __name__ == "__main__":
    main()
