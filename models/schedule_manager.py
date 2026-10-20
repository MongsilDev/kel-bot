"""스케줄러 + 재시작 시 놓친 이벤트 보정."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import time, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from discord.ext import tasks

from config.settings import Settings
from models.application import ApplicationStore
from models.draw_orchestrator import DrawOrchestrator, DrawResult
from models.draw_state import DrawState, DrawStatus
from models.priority import PriorityStore
from models.priority_audit import PriorityAuditStore
from utils.time import KST, current_scrim_date, kst_at, now_kst

logger = logging.getLogger("kel-bot.schedule_manager")


DrawCallback = Callable[[DrawResult], Awaitable[None]]
SimpleCallback = Callable[[], Awaitable[None]]


@dataclass
class StateBundle:
    applications: ApplicationStore
    priorities: PriorityStore
    draw_state: DrawState
    audit: PriorityAuditStore


class ScheduleManager:
    """일일 운영 스케줄 + 재시작 보정.

    - RESET_HOUR 일일 리셋
    - DRAW_HOUR:DRAW_MINUTE 1차 추첨, 미달이면 보류
    - DEADLINE_HOUR 데드라인, 미달 취소와 우선권 소멸
    - 신청 이벤트 시 즉시 추첨 트리거
    """

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        on_draw: DrawCallback,
        on_state_changed: SimpleCallback,
        on_deadline_cancelled: SimpleCallback,
        on_reset: SimpleCallback,
    ) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self.on_draw = on_draw
        self.on_state_changed = on_state_changed
        self.on_deadline_cancelled = on_deadline_cancelled
        self.on_reset = on_reset

        self._lock = asyncio.Lock()
        scrim_date = current_scrim_date(self.settings.reset_hour).isoformat()
        self._state = StateBundle(
            applications=ApplicationStore.load(data_dir / "applications.json", scrim_date),
            priorities=PriorityStore.load(data_dir / "priorities.json"),
            draw_state=DrawState.load(data_dir / "draw_state.json", scrim_date),
            audit=PriorityAuditStore.load(data_dir / "priority_audit.json"),
        )

        self._reset_task = tasks.loop(time=time(hour=settings.reset_hour, minute=0, tzinfo=KST))(
            self._guarded("일일 리셋", self._run_reset)
        )
        self._draw_task = tasks.loop(
            time=time(hour=settings.draw_hour, minute=settings.draw_minute, tzinfo=KST)
        )(
            self._guarded("1차 추첨", self._run_draw)
        )
        self._deadline_task = tasks.loop(time=time(hour=settings.deadline_hour, minute=0, tzinfo=KST))(
            self._guarded("데드라인", self._run_deadline)
        )

    @staticmethod
    def _guarded(name: str, job: SimpleCallback) -> SimpleCallback:
        # tasks.loop는 예외 한 번에 영구 정지
        async def runner() -> None:
            try:
                await job()
            except Exception:
                logger.exception("[스케줄] %s 작업 실패 - 다음 회차에 재시도", name)

        return runner

    # 외부 접근 ------------------------------------------------------------
    @property
    def state(self) -> StateBundle:
        return self._state

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def orchestrator(self) -> DrawOrchestrator:
        return DrawOrchestrator(
            self._state.applications,
            self._state.priorities,
            self._state.draw_state,
            self.settings.team_slots,
        )

    # 부팅 시 보정 ----------------------------------------------------------
    async def catch_up(self) -> None:
        async with self._lock:
            await self._catch_up_locked()
        await self.on_state_changed()

    async def _catch_up_locked(self) -> None:
        time_based = current_scrim_date(self.settings.reset_hour).isoformat()
        # 저장된 일자가 과거면 시간 기준으로 catch-up (놓친 리셋). 미래/현재면 그대로.
        if self._state.draw_state.scrim_date < time_based:
            logger.info("[스케줄] 놓친 리셋 보정: %s에서 %s로", self._state.draw_state.scrim_date, time_based)
            self._reset_to(time_based)
        scrim_date = self._state.draw_state.scrim_date
        self._state.priorities.purge_outdated(scrim_date)
        self._state.audit.purge_outdated(scrim_date)

        moment = now_kst()
        scrim_date_obj = date_cls.fromisoformat(scrim_date)
        draw_at = kst_at(scrim_date_obj, self.settings.draw_hour, self.settings.draw_minute)
        deadline_at = kst_at(scrim_date_obj, self.settings.deadline_hour, 0)

        if moment >= draw_at and self._state.draw_state.status == DrawStatus.PENDING:
            result = self.orchestrator().attempt_primary_draw()
            if result is not None:
                await self.on_draw(result)

        if self._state.draw_state.status == DrawStatus.HELD:
            result = self.orchestrator().attempt_instant_draw()
            if result is not None:
                await self.on_draw(result)

        if moment >= deadline_at and not self._state.draw_state.deadline_processed:
            cancelled = self.orchestrator().force_deadline_cancel()
            if cancelled:
                self._early_reset_to_next_day()
                await self.on_deadline_cancelled()

        # 부팅 직전 미진행 확정 상태로 종료된 경우 D+1로 전환 (안전망)
        if (
            self._state.draw_state.status == DrawStatus.CANCELLED
            and moment >= deadline_at
        ):
            self._early_reset_to_next_day()

    def _reset_to(self, scrim_date: str) -> bool:
        """주어진 scrim_date로 application/draw_state 리셋. 같은 일자면 no-op.

        실제로 초기화했으면 True, 이미 같은 일자라 no-op이면 False를 반환한다.
        """
        if (
            self._state.applications.scrim_date == scrim_date
            and self._state.draw_state.scrim_date == scrim_date
        ):
            return False
        self._state.applications.reset(scrim_date)
        self._state.draw_state.reset(scrim_date)
        return True

    def _early_reset_to_next_day(self) -> None:
        """데드라인 미진행 확정 직후 D+1로 즉시 전환하고 취소 결과를 남긴다."""
        current = self._state.draw_state.scrim_date
        applicants = self._state.applications.count_active()
        next_date = (date_cls.fromisoformat(current) + timedelta(days=1)).isoformat()
        logger.info("[스케줄] 미진행 확정, D+1 즉시 리셋: %s에서 %s로", current, next_date)
        self._reset_to(next_date)
        self._state.draw_state.previous_cancel = {"scrim_date": current, "applicants": applicants}
        self._state.draw_state.save()
        self._state.priorities.purge_outdated(next_date)
        self._state.audit.purge_outdated(next_date)

    # 이벤트 트리거 ---------------------------------------------------------
    async def trigger_instant_draw_if_ready(self) -> None:
        async with self._lock:
            result = self.orchestrator().attempt_instant_draw()
        if result is not None:
            await self.on_draw(result)

    # 스케줄러 라이프사이클 -------------------------------------------------
    def start(self) -> None:
        if not self._reset_task.is_running():
            self._reset_task.start()
        if not self._draw_task.is_running():
            self._draw_task.start()
        if not self._deadline_task.is_running():
            self._deadline_task.start()

    def stop(self) -> None:
        for t in (self._reset_task, self._draw_task, self._deadline_task):
            if t.is_running():
                t.cancel()

    async def _run_reset(self) -> None:
        async with self._lock:
            scrim_date = current_scrim_date(self.settings.reset_hour).isoformat()
            changed = self._reset_to(scrim_date)
            self._state.priorities.purge_outdated(scrim_date)
            self._state.audit.purge_outdated(scrim_date)
            if changed:
                logger.info("[스케줄] 일일 리셋 실행: %s", scrim_date)
            else:
                logger.info("[스케줄] 일일 리셋 스킵: 조기 초기화로 이미 %s", scrim_date)
        # 실제 초기화된 경우에만 대시보드 메시지 재생성 (조기초기화로 no-op이면 스킵)
        if changed:
            await self.on_reset()

    async def _run_draw(self) -> None:
        async with self._lock:
            draw_state = self._state.draw_state
            already = draw_state.status
            result = self.orchestrator().attempt_primary_draw()
            if result is not None:
                logger.info(
                    "[스케줄] 1차 추첨 실행 - 선정: %d, 탈락: %d", len(result.selected), len(result.rejected)
                )
            elif draw_state.status == DrawStatus.HELD:
                logger.info(
                    "[스케줄] 1차 추첨 보류 - 신청: %d/%d",
                    self._state.applications.count_active(),
                    self.settings.team_slots,
                )
            else:
                logger.info("[스케줄] 1차 추첨 스킵 - 상태: %s", already.value)
        if result is not None:
            await self.on_draw(result)
        else:
            await self.on_state_changed()

    async def _run_deadline(self) -> None:
        async with self._lock:
            status = self._state.draw_state.status
            processed = self._state.draw_state.deadline_processed
            cancelled = self.orchestrator().force_deadline_cancel()
            if cancelled:
                logger.info("[스케줄] 데드라인 처리 - 미달 취소, 신청: %d", self._state.applications.count_active())
                self._early_reset_to_next_day()
            elif processed:
                logger.info("[스케줄] 데드라인 스킵 - 이미 처리됨")
            else:
                logger.info("[스케줄] 데드라인 처리 - 취소 없음, 상태: %s", status.value)
        if cancelled:
            await self.on_reset()
        else:
            await self.on_state_changed()
