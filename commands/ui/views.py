"""대시보드/공지/취소 확인 LayoutView."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Awaitable, Callable, Dict, List, Optional

import discord
from discord import ButtonStyle, Color
from discord.ui import (
    ActionRow,
    Button,
    Container,
    LayoutView,
    Modal,
    Select,
    Separator,
    TextDisplay,
    TextInput,
)

from discord.utils import escape_markdown

from commands.ui.layout_helpers import (
    FOOTER_TEXT,
    error_view,
    info_view,
)
from models.application import Application, ApplicationStatus
from models.draw_orchestrator import TWO_COMPANY_TEAMS
from models.draw_state import DrawStatus
from models.priority_audit import RemovalEntry

_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

logger = logging.getLogger("kel-bot.views")

ApplyHandler = Callable[[discord.Interaction], Awaitable[None]]
CancelHandler = Callable[[discord.Interaction], Awaitable[None]]
ManagePriorityHandler = Callable[[discord.Interaction], Awaitable[None]]
RemoveSelectHandler = Callable[[discord.Interaction, str], Awaitable[None]]
AddSubmitHandler = Callable[[discord.Interaction, str], Awaitable[None]]


# 버튼 쿨다운 ---------------------------------------------------------------
_button_cooldowns: Dict[int, float] = {}
_COOLDOWN_SECONDS = 1.5


def check_cooldown(user_id: int) -> bool:
    """True면 쿨다운 통과(허용), False면 차단."""
    now = time.monotonic()
    last = _button_cooldowns.get(user_id, 0.0)
    if now - last < _COOLDOWN_SECONDS:
        return False
    _button_cooldowns[user_id] = now
    if len(_button_cooldowns) > 200:
        expired = [uid for uid, t in _button_cooldowns.items() if now - t > 60]
        for uid in expired:
            _button_cooldowns.pop(uid, None)
    return True


# 대시보드 ----------------------------------------------------------------
@dataclass
class DashboardSnapshot:
    scrim_date: str
    team_slots: int
    applications: List[Application]
    priority_regions: set[str]
    next_day_priority_regions: set[str]
    draw_status: DrawStatus
    drawn_at: Optional[str]
    application_open: bool
    reset_at: str = "21:00"
    draw_at: str = "00:30"
    deadline_at: str = "17:00"
    previous_cancel: Optional[dict] = None
    removal_log: List[RemovalEntry] = field(default_factory=list)


def _format_pending_lines(snapshot: DashboardSnapshot) -> str:
    apps = [a for a in snapshot.applications if a.status is ApplicationStatus.PENDING]
    if not apps:
        return "_아직 신청한 팀이 없습니다._"
    lines = []
    for idx, app in enumerate(apps, start=1):
        marker = "★ " if app.region in snapshot.priority_regions else "　 "
        lines.append(f"`{idx:>2}.` {marker}{escape_markdown(app.applicant_display)}")
    return "\n".join(lines)


def _format_selected_lines(snapshot: DashboardSnapshot) -> str:
    apps = [a for a in snapshot.applications if a.status is ApplicationStatus.SELECTED]
    if not apps:
        return "_없음_"
    lines = []
    for idx, app in enumerate(apps, start=1):
        marker = "★ " if app.had_priority else "　 "
        lines.append(f"`{idx:>2}.` {marker}{escape_markdown(app.applicant_display)}")
    return "\n".join(lines)


def _format_group_lines(snapshot: DashboardSnapshot, group_label: str) -> str:
    apps = [
        a for a in snapshot.applications
        if a.status is ApplicationStatus.SELECTED and a.group == group_label
    ]
    if not apps:
        return "_없음_"
    lines = []
    for idx, app in enumerate(apps, start=1):
        marker = "★ " if app.had_priority else "　 "
        lines.append(f"`{idx:>2}.` {marker}{escape_markdown(app.applicant_display)}")
    return "\n".join(lines)


def _has_groups(snapshot: DashboardSnapshot) -> bool:
    return any(
        a.group for a in snapshot.applications
        if a.status is ApplicationStatus.SELECTED
    )


def _format_rejected_lines(snapshot: DashboardSnapshot) -> str:
    apps = [a for a in snapshot.applications if a.status is ApplicationStatus.REJECTED]
    if not apps:
        return "_없음_"
    return "\n".join(
        f"`{idx:>2}.` {escape_markdown(a.applicant_display)}" for idx, a in enumerate(apps, start=1)
    )


def _format_removal_log_lines(entries: List[RemovalEntry]) -> str:
    lines = []
    for e in entries:
        when = format_kst_short(e.removed_at) or e.removed_at
        mark = "➕" if getattr(e, "action", "revoke") == "grant" else "➖"
        lines.append(f"`{when}` {mark} {escape_markdown(e.region)} <@{e.actor_id}>")
    return "\n".join(lines)


def _draw_status_label(snapshot: DashboardSnapshot) -> str:
    apps = len(snapshot.applications)
    if snapshot.draw_status is DrawStatus.DONE:
        when = format_kst_short(snapshot.drawn_at)
        return f"✅ {when} 추첨 완료" if when else "✅ 추첨 완료"
    if snapshot.draw_status is DrawStatus.CANCELLED:
        if apps == 0:
            return "🌙 오늘 신청 없이 마감"
        return f"❌ {snapshot.team_slots}팀 미달로 취소"
    if snapshot.draw_status is DrawStatus.HELD:
        return "⏸️ 추첨 보류 중"
    if not snapshot.application_open:
        return "🌙 신청 마감"
    return "🟢 신청 접수 중"


def _format_count(snapshot: DashboardSnapshot) -> str:
    total = len(snapshot.applications)
    if snapshot.draw_status is not DrawStatus.CANCELLED and (
        _has_groups(snapshot) or total >= TWO_COMPANY_TEAMS
    ):
        return f"`{total}`팀, 두 중대 편성"
    return f"`{total} / {snapshot.team_slots}`팀"


def _format_previous_cancel(snapshot: DashboardSnapshot) -> Optional[str]:
    prev = snapshot.previous_cancel
    if not prev:
        return None
    try:
        d = date_cls.fromisoformat(prev["scrim_date"])
    except (KeyError, ValueError):
        return None
    applicants = prev.get("applicants", 0)
    if applicants == 0:
        return f"🌙 {d.month}/{d.day} 스크림은 신청 팀이 없어 열리지 않았습니다."
    return (
        f"❌ {d.month}/{d.day} 스크림은 {applicants}팀만 신청해 "
        f"{snapshot.team_slots}팀 미달로 취소되었습니다."
    )


def _format_scrim_title(scrim_date: str) -> str:
    try:
        d = date_cls.fromisoformat(scrim_date)
    except ValueError:
        return f"🏆 KEL 스크림 {scrim_date}"
    weekday = _WEEKDAYS[d.weekday()]
    return f"🏆 {d.month}/{d.day} ({weekday}) KEL 스크림"


def format_kst_short(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%H:%M")


class DashboardView(LayoutView):
    """영구 대시보드 메시지의 LayoutView. 버튼 콜백은 외부 주입."""

    def __init__(
        self,
        snapshot: DashboardSnapshot,
        apply_handler: ApplyHandler,
        cancel_handler: CancelHandler,
        manage_priority_handler: ManagePriorityHandler,
    ) -> None:
        super().__init__(timeout=None)
        self.snapshot = snapshot
        self.apply_handler = apply_handler
        self.cancel_handler = cancel_handler
        self.manage_priority_handler = manage_priority_handler

        window_closed = not snapshot.application_open
        self.apply_button = Button(
            label="신청",
            style=ButtonStyle.primary,
            emoji="✏️",
            custom_id="kel:apply",
            disabled=window_closed,
        )
        self.apply_button.callback = self._on_apply
        self.cancel_button = Button(
            label="취소",
            style=ButtonStyle.secondary,
            emoji="🗑️",
            custom_id="kel:cancel",
            disabled=window_closed,
        )
        self.cancel_button.callback = self._on_cancel
        self.priority_button = Button(
            label="우선권",
            style=ButtonStyle.secondary,
            emoji="⭐",
            custom_id="kel:priority",
        )
        self.priority_button.callback = self._on_priority

        title = _format_scrim_title(snapshot.scrim_date)
        status_label = _draw_status_label(snapshot)
        header_status = f"{status_label} {_format_count(snapshot)}"

        is_done = snapshot.draw_status is DrawStatus.DONE

        header_lines = [f"## {title}"]
        previous_cancel = _format_previous_cancel(snapshot)
        if previous_cancel:
            header_lines.append(previous_cancel)
        header_lines.append(header_status)
        if not is_done and snapshot.priority_regions:
            priority_text = escape_markdown(", ".join(sorted(snapshot.priority_regions)))
            header_lines.append(f"⭐ 우선권: {priority_text}")

        children = [
            TextDisplay(content="\n".join(header_lines)),
            Separator(),
        ]

        if is_done:
            if _has_groups(snapshot):
                children.append(
                    TextDisplay(content=f"### 1️⃣ 1중대\n{_format_group_lines(snapshot, 'A')}")
                )
                children.append(
                    TextDisplay(content=f"### 2️⃣ 2중대\n{_format_group_lines(snapshot, 'B')}")
                )
            else:
                selected = _format_selected_lines(snapshot)
                children.append(
                    TextDisplay(content=f"### 선정 {snapshot.team_slots}팀\n{selected}")
                )
            if any(a.status is ApplicationStatus.REJECTED for a in snapshot.applications):
                rejected = _format_rejected_lines(snapshot)
                children.append(TextDisplay(content=f"### 탈락 팀\n{rejected}"))
            if snapshot.next_day_priority_regions:
                next_priority_text = escape_markdown(", ".join(sorted(snapshot.next_day_priority_regions)))
                children.append(TextDisplay(content=f"-# 내일 우선권: {next_priority_text}"))
        else:
            teams = _format_pending_lines(snapshot)
            children.append(TextDisplay(content=f"### 신청 팀\n{teams}"))

        if snapshot.removal_log:
            children.append(Separator())
            children.append(
                TextDisplay(
                    content=f"### 우선권 변경 로그\n{_format_removal_log_lines(snapshot.removal_log)}"
                )
            )

        children.append(
            ActionRow(self.apply_button, self.cancel_button, self.priority_button)
        )
        children.append(
            TextDisplay(
                content=(
                    f"-# `{snapshot.reset_at}` 초기화 / `{snapshot.draw_at}` 추첨 / "
                    f"`{snapshot.deadline_at}` 마감 / 닉네임 `지역) 이름` 필수"
                )
            )
        )

        self.add_item(Container(*children, accent_colour=Color.green()))

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message(
                view=info_view("잠시 후 다시 시도해주세요."),
                ephemeral=True,
            )
            return
        try:
            await self.apply_handler(interaction)
        except Exception:  # noqa: BLE001
            logger.exception("apply_handler 예외")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=error_view("처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요."),
                    ephemeral=True,
                )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message(
                view=info_view("잠시 후 다시 시도해주세요."),
                ephemeral=True,
            )
            return
        try:
            await self.cancel_handler(interaction)
        except Exception:  # noqa: BLE001
            logger.exception("cancel_handler 예외")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=error_view("처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요."),
                    ephemeral=True,
                )

    async def _on_priority(self, interaction: discord.Interaction) -> None:
        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message(
                view=info_view("잠시 후 다시 시도해주세요."),
                ephemeral=True,
            )
            return
        try:
            await self.manage_priority_handler(interaction)
        except Exception:  # noqa: BLE001
            logger.exception("manage_priority_handler 예외")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=error_view("처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요."),
                    ephemeral=True,
                )


class _EphemeralPanel(LayoutView):
    """시간이 지나면 닫히는 ephemeral 패널. 응답 직후 origin 지정 필요."""

    origin: Optional[discord.Interaction] = None

    async def on_timeout(self) -> None:
        if self.origin is None:
            return
        try:
            await self.origin.edit_original_response(
                view=info_view("제한 시간이 지났습니다.\n버튼을 다시 눌러주세요.")
            )
        except discord.HTTPException:
            pass


# 취소 확인 -----------------------------------------------------------------
class CancelConfirmView(_EphemeralPanel):
    """취소 확인 LayoutView (ephemeral)."""

    def __init__(
        self,
        region: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=60)
        self.region = region
        self.on_confirm = on_confirm

        self.confirm_button = Button(
            label="신청 취소하기",
            style=ButtonStyle.danger,
            emoji="⚠️",
        )
        self.confirm_button.callback = self._confirm
        self.back_button = Button(
            label="아니요",
            style=ButtonStyle.secondary,
        )
        self.back_button.callback = self._back

        container = Container(
            TextDisplay(
                content=(
                    f"## 🚫 신청 취소 확인\n"
                    f"`{region}` 지역 신청을 취소하시겠습니까?"
                )
            ),
            Separator(),
            ActionRow(self.confirm_button, self.back_button),
            TextDisplay(content=FOOTER_TEXT),
            accent_colour=Color.orange(),
        )
        self.add_item(container)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.on_confirm(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=info_view("신청을 취소하지 않았습니다."),
        )


# 우선권 관리 ---------------------------------------------------------------
class PriorityManageView(_EphemeralPanel):
    """우선권 관리 패널 (ephemeral): 추가 버튼 + 제거 지역 Select."""

    def __init__(
        self,
        *,
        target_label: str,
        removable_regions: List[str],
        on_add: ManagePriorityHandler,
        on_remove_select: RemoveSelectHandler,
    ) -> None:
        super().__init__(timeout=60)
        self.on_add = on_add
        self.on_remove_select = on_remove_select

        self.add_button = Button(
            label="추가",
            style=ButtonStyle.success,
            emoji="➕",
        )
        self.add_button.callback = self._on_add

        current = escape_markdown(", ".join(removable_regions)) if removable_regions else "없음"
        children = [
            TextDisplay(content=f"## ⭐ 우선권 {target_label}\n현재 {current}"),
            Separator(),
            ActionRow(self.add_button),
        ]
        if removable_regions:
            self.select: Optional[Select] = Select(
                placeholder="제거할 지역",
                min_values=1,
                max_values=1,
                options=[discord.SelectOption(label=r, value=r) for r in removable_regions],
            )
            self.select.callback = self._on_remove
            children.append(ActionRow(self.select))
        else:
            self.select = None
            children.append(TextDisplay(content="-# 제거할 우선권 없음"))

        self.add_item(Container(*children, accent_colour=Color.blurple()))

    async def _on_add(self, interaction: discord.Interaction) -> None:
        await self.on_add(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        assert self.select is not None
        self.stop()
        await self.on_remove_select(interaction, self.select.values[0])


# 우선권 추가 모달 ----------------------------------------------------------
class PriorityAddModal(Modal):
    """우선권 추가 지역 입력 모달 (ephemeral)."""

    def __init__(self, on_submit_region: AddSubmitHandler) -> None:
        super().__init__(title="우선권 추가")
        self.on_submit_region = on_submit_region
        self.region_input = TextInput(
            label="지역",
            placeholder="예: 광주",
            min_length=1,
            max_length=10,
            required=True,
        )
        self.add_item(self.region_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_submit_region(interaction, self.region_input.value)
