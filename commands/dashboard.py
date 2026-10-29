"""대시보드 setup/refresh와 신청, 취소 버튼 콜백."""
from __future__ import annotations

import logging
from datetime import date as date_cls, timedelta
from pathlib import Path

import discord

from commands.ui.layout_helpers import error_view, info_view, success_view
from commands.ui.views import (
    CancelConfirmView,
    DashboardSnapshot,
    DashboardView,
    PriorityAddModal,
    PriorityManageView,
)
from config.settings import Settings
from models.application import ApplicationError
from models.draw_state import DrawStatus
from models.schedule_manager import ScheduleManager
from services.discord_service import (
    fetch_message_safe,
    fetch_text_channel,
    load_message_id,
    save_message_id,
)
from utils.nickname import NicknameFormatError, parse as parse_nickname
from utils.time import KST, kst_at, now_kst

logger = logging.getLogger("kel-bot.dashboard")

_STALE_MESSAGE = "추첨이 진행되어 우선권 상태가 변경되었습니다.\n다시 시도해주세요."


class DashboardController:
    """대시보드 메시지 관리 + 버튼 콜백 + 추첨 결과 공지."""

    def __init__(self, bot: discord.Client, settings: Settings, schedule: ScheduleManager) -> None:
        self.bot = bot
        self.settings = settings
        self.schedule = schedule
        self._dashboard_path: Path = settings.data_dir / "dashboard_message.json"
        self._dashboard_message: discord.Message | None = None

    # 부팅 시 초기화 --------------------------------------------------------
    async def setup(self) -> None:
        channel = await fetch_text_channel(self.bot, self.settings.apply_channel_id)
        message_id = load_message_id(self._dashboard_path)
        message: discord.Message | None = None
        if message_id:
            message = await fetch_message_safe(channel, message_id)
        if message is None:
            view = self._build_dashboard_view()
            message = await channel.send(view=view)
            save_message_id(self._dashboard_path, message.id)
            logger.info("대시보드 메시지 신규 생성: %s", message.id)
        else:
            await self._edit_message(message)
            logger.info("기존 대시보드 메시지 재바인딩: %s", message.id)
        self._dashboard_message = message

    def register_persistent_view(self) -> None:
        # 부팅 시 setup이 실패해도 기존 메시지의 버튼이 응답하도록 선등록
        self.bot.add_view(
            self._build_dashboard_view(), message_id=load_message_id(self._dashboard_path)
        )

    # 갱신 ----------------------------------------------------------------
    async def refresh(self) -> None:
        if self._dashboard_message is None:
            return
        try:
            await self._edit_message(self._dashboard_message)
        except (discord.NotFound, discord.Forbidden):
            logger.warning("대시보드 메시지 수정 실패, 재생성")
            self._dashboard_message = None
            await self.setup()

    async def _edit_message(self, message: discord.Message) -> None:
        view = self._build_dashboard_view()
        await message.edit(view=view, content=None)

    async def recreate(self) -> None:
        """일일 또는 조기 초기화 때 기존 대시보드 메시지를 지우고 새로 보낸다."""
        channel = await fetch_text_channel(self.bot, self.settings.apply_channel_id)
        old = self._dashboard_message
        if old is not None:
            try:
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("기존 대시보드 메시지 삭제 실패, 새로 전송")
        view = self._build_dashboard_view()
        message = await channel.send(view=view)
        save_message_id(self._dashboard_path, message.id)
        self._dashboard_message = message
        logger.info("대시보드 메시지 재생성: %s", message.id)

    def _build_dashboard_view(self) -> DashboardView:
        snapshot = self._current_snapshot()
        return DashboardView(
            snapshot=snapshot,
            apply_handler=self.handle_apply,
            cancel_handler=self.handle_cancel,
            manage_priority_handler=self.handle_manage_priority,
        )

    def _current_snapshot(self) -> DashboardSnapshot:
        state = self.schedule.state
        scrim_date = state.draw_state.scrim_date
        priority_regions = state.priorities.regions_for(scrim_date)
        next_date = (date_cls.fromisoformat(scrim_date) + timedelta(days=1)).isoformat()
        next_day_priority_regions = state.priorities.dated_regions_for(next_date)
        application_open = self._is_application_window_open(scrim_date)
        removal_log = sorted(
            state.audit.entries_for(scrim_date) + state.audit.entries_for(next_date),
            key=lambda e: e.removed_at,
        )
        s = self.settings
        return DashboardSnapshot(
            scrim_date=scrim_date,
            team_slots=self.settings.team_slots,
            applications=state.applications.active(),
            priority_regions=priority_regions,
            next_day_priority_regions=next_day_priority_regions,
            super_regions=state.priorities.super_regions_for(scrim_date),
            next_day_super_regions=state.priorities.super_regions_for(next_date),
            draw_status=state.draw_state.status,
            drawn_at=state.draw_state.drawn_at,
            application_open=application_open,
            reset_at=f"{s.reset_hour:02d}:00",
            draw_at=f"{s.draw_hour:02d}:{s.draw_minute:02d}",
            deadline_at=f"{s.deadline_hour:02d}:00",
            previous_cancel=state.draw_state.previous_cancel,
            removal_log=removal_log,
        )

    def _closed_message(self) -> str:
        return (
            "신청이 마감되었습니다.\n"
            f"다음 스크림은 `{self.settings.reset_hour:02d}:00`부터 신청할 수 있습니다."
        )

    def _is_application_window_open(self, scrim_date: str) -> bool:
        state = self.schedule.state
        if state.draw_state.is_application_closed():
            return False
        moment = now_kst()
        d = date_cls.fromisoformat(scrim_date)
        deadline_at = kst_at(d, self.settings.deadline_hour, 0)
        return moment < deadline_at

    # 버튼 콜백 ------------------------------------------------------------
    async def handle_apply(self, interaction: discord.Interaction) -> None:
        scrim_date = self.schedule.state.draw_state.scrim_date
        if not self._is_application_window_open(scrim_date):
            await interaction.response.send_message(
                view=info_view(self._closed_message()),
                ephemeral=True,
            )
            return
        member = interaction.user
        display_name = getattr(member, "display_name", None) or member.name
        try:
            parsed = parse_nickname(display_name)
        except NicknameFormatError as exc:
            await interaction.response.send_message(
                view=error_view(str(exc)),
                ephemeral=True,
            )
            return

        async with self.schedule.lock:
            if not self._is_application_window_open(self.schedule.state.draw_state.scrim_date):
                await interaction.response.send_message(
                    view=info_view(self._closed_message()),
                    ephemeral=True,
                )
                return
            store = self.schedule.state.applications
            priorities = self.schedule.state.priorities
            try:
                scrim_date = self.schedule.state.draw_state.scrim_date
                had_priority = priorities.has(parsed.region, scrim_date)
                had_super = parsed.region in priorities.super_regions_for(scrim_date)
                store.add(
                    region=parsed.region,
                    applicant_id=str(member.id),
                    applicant_display=display_name,
                    had_priority=had_priority,
                    had_super=had_super,
                )
            except ApplicationError as exc:
                await interaction.response.send_message(
                    view=info_view(str(exc)),
                    ephemeral=True,
                )
                return

        logger.info(
            "[신청] %s, %s(%s)%s",
            parsed.region,
            display_name,
            member.id,
            " [최우선]" if had_super else " [우선권]" if had_priority else "",
        )
        if had_super:
            body = f"`{parsed.region}` 지역으로 신청했습니다.\n최우선이 적용되었습니다."
        elif had_priority:
            body = f"`{parsed.region}` 지역으로 신청했습니다.\n우선권이 적용되었습니다."
        else:
            body = f"`{parsed.region}` 지역으로 신청했습니다."
        await interaction.response.send_message(
            view=success_view(body),
            ephemeral=True,
        )
        await self.schedule.trigger_instant_draw_if_ready()
        await self.refresh()

    async def handle_cancel(self, interaction: discord.Interaction) -> None:
        state = self.schedule.state
        if state.draw_state.is_application_closed():
            await interaction.response.send_message(
                view=info_view("추첨이 완료되어 취소할 수 없습니다."),
                ephemeral=True,
            )
            return
        existing = state.applications.by_user(str(interaction.user.id))
        if existing is None:
            await interaction.response.send_message(
                view=info_view("등록된 신청이 없습니다."),
                ephemeral=True,
            )
            return

        confirm_view = CancelConfirmView(
            region=existing.region,
            on_confirm=self._confirm_cancel,
        )
        await interaction.response.send_message(view=confirm_view, ephemeral=True)
        confirm_view.origin = interaction

    async def _confirm_cancel(self, interaction: discord.Interaction) -> None:
        async with self.schedule.lock:
            if self.schedule.state.draw_state.is_application_closed():
                await interaction.response.edit_message(
                    view=info_view("추첨이 완료되어 취소할 수 없습니다.")
                )
                return
            store = self.schedule.state.applications
            try:
                app = store.cancel_by_user(str(interaction.user.id))
            except ApplicationError as exc:
                await interaction.response.edit_message(view=info_view(str(exc)))
                return
        display = getattr(interaction.user, "display_name", None) or interaction.user.name
        logger.info("[취소] %s, %s(%s)", app.region, display, interaction.user.id)
        await interaction.response.edit_message(
            view=success_view(f"`{app.region}` 신청을 취소했습니다."),
        )
        await self.refresh()

    # 우선권 관리 ----------------------------------------------------------
    def _removable_priority(self) -> tuple[str, list[str]]:
        """현재 제거 가능한 우선권의 대상 일자와 지역 목록.

        추첨 후(DONE)엔 내일(D+1) 우선권, 그 외엔 오늘(D) 우선권을 대상으로 한다.
        """
        state = self.schedule.state
        scrim_date = state.draw_state.scrim_date
        if state.draw_state.status is DrawStatus.DONE:
            target = (date_cls.fromisoformat(scrim_date) + timedelta(days=1)).isoformat()
        else:
            target = scrim_date
        return target, sorted(state.priorities.dated_regions_for(target))

    async def handle_manage_priority(self, interaction: discord.Interaction) -> None:
        # 패널을 연 시점의 대상 일자를 고정. 그 사이 추첨이나 리셋으로 바뀌면 확정 단계에서 거부
        opened_target, regions = self._removable_priority()
        super_regions = self.schedule.state.priorities.super_regions_for(opened_target)
        target_label = self._target_label(opened_target)

        async def on_add(it: discord.Interaction) -> None:
            await self._open_add_modal(it, opened_target)

        async def on_remove_select(it: discord.Interaction, region: str) -> None:
            await self._confirm_remove_priority(it, region, opened_target)

        view = PriorityManageView(
            target_label=target_label,
            removable_regions=regions,
            super_regions=super_regions,
            on_add=on_add,
            on_remove_select=on_remove_select,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.origin = interaction

    def _target_label(self, target: str) -> str:
        """관리 패널 헤더용 대상 일자 라벨. 예: `7/12 오늘`."""
        scrim_date = self.schedule.state.draw_state.scrim_date
        d = date_cls.fromisoformat(target)
        when = "오늘" if target == scrim_date else "내일"
        return f"{d.month}/{d.day} {when}"

    async def _open_add_modal(self, interaction: discord.Interaction, opened_target: str) -> None:
        async def on_submit_region(it: discord.Interaction, raw_region: str) -> None:
            await self._confirm_add_priority(it, raw_region, opened_target)

        await interaction.response.send_modal(
            PriorityAddModal(on_submit_region=on_submit_region)
        )

    async def _confirm_add_priority(
        self, interaction: discord.Interaction, raw_region: str, opened_target: str
    ) -> None:
        region = raw_region.strip()
        if not region:
            await interaction.response.send_message(
                view=info_view("지역명을 입력해주세요."),
                ephemeral=True,
            )
            return
        async with self.schedule.lock:
            state = self.schedule.state
            current_target, _ = self._removable_priority()
            stale = current_target != opened_target
            if stale:
                await interaction.response.send_message(
                    view=info_view(_STALE_MESSAGE),
                    ephemeral=True,
                )
            elif not state.priorities.grant_one(region, opened_target):
                await interaction.response.send_message(
                    view=info_view(f"`{region}` 우선권이 이미 있습니다."),
                    ephemeral=True,
                )
                return
            else:
                display = getattr(interaction.user, "display_name", None) or interaction.user.name
                state.audit.record(
                    region=region,
                    actor_id=str(interaction.user.id),
                    actor_name=display,
                    scrim_date=opened_target,
                    action="grant",
                )
        if stale:
            await self.refresh()
            return
        logger.info(
            "[우선권 부여] %s, %s(%s), 대상 %s",
            region, display, interaction.user.id, opened_target,
        )
        await interaction.response.send_message(
            view=success_view(f"`{region}` 우선권을 추가했습니다."),
            ephemeral=True,
        )
        await self.refresh()

    async def _confirm_remove_priority(
        self, interaction: discord.Interaction, region: str, opened_target: str
    ) -> None:
        async with self.schedule.lock:
            state = self.schedule.state
            current_target, _ = self._removable_priority()
            stale = current_target != opened_target
            if stale:
                await interaction.response.edit_message(view=info_view(_STALE_MESSAGE))
            elif not state.priorities.revoke(region, opened_target):
                await interaction.response.edit_message(
                    view=info_view(f"`{region}`에는 제거할 우선권이 없습니다.")
                )
                return
            else:
                display = getattr(interaction.user, "display_name", None) or interaction.user.name
                state.audit.record(
                    region=region,
                    actor_id=str(interaction.user.id),
                    actor_name=display,
                    scrim_date=opened_target,
                    action="revoke",
                )
        if stale:
            await self.refresh()
            return
        logger.info(
            "[우선권 제거] %s, %s(%s), 대상 %s",
            region, display, interaction.user.id, opened_target,
        )
        await interaction.response.edit_message(
            view=success_view(f"`{region}` 우선권을 제거했습니다."),
        )
        await self.refresh()

    # 추첨/데드라인 이벤트는 대시보드만 갱신
    async def announce_draw(self) -> None:
        await self.refresh()

    async def announce_deadline_cancelled(self) -> None:
        await self.refresh()
