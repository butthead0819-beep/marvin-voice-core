"""🎛️ Discord UI views — 從 voice_controller.py 抽離（Phase 0）。

PlayControlView：串流播放控制台 + 佇列管理。
ConsentView：一次性同意 / 拒絕按鈕。

兩者皆 constructor inject ref：PlayControlView 收 controller、ConsentView 收
consent_manager。PlayControlView 在建構時自註冊進 controller._active_views，
on_timeout 時移除，使 cog_unload 能 stop 所有存活 view、斷開 view→cog 強引用，
防 hot reload 殘留雙 cog 實例。
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from consent_manager import ConsentManager
    from cogs.voice_controller import VoiceController


def build_song_embed(info: dict | None, *, image_url: str | None = None) -> discord.Embed:
    """🎵 歌曲卡（精簡）：只留「可點連結（→video）」＋「全幅封面」。

    誰點的用頭像表示——overlay 到封面上的合成圖由 caller 傳 image_url=attachment://…；
    無合成圖時退純封面(info['thumbnail'])。每首一則、不刪＝頻道留播放紀錄。
    吃 info dict（非 controller）→ 背景 task 用快照不受下一首影響、也好測。
    """
    # accent 色條＝封面抽出的主色（palette[0]）；無/壞 → 退 blurple
    color = discord.Color.blurple()
    pal = (info or {}).get('palette') or []
    if pal:
        _h = (pal[0] or '').lstrip('#')
        if len(_h) == 6:
            try:
                color = discord.Color(int(_h, 16))
            except ValueError:
                pass
    embed = discord.Embed(color=color)
    if not info:
        embed.description = "目前沒有歌曲在播放。"
        return embed
    embed.title = (info.get('title') or '🎵')[:250]
    wp = info.get('webpage_url')
    if wp:
        embed.url = wp                              # 標題可點 → 影片(video id)
    img = image_url or info.get('thumbnail')
    if img:
        embed.set_image(url=img)                    # 全幅封面（或封面+頭像合成圖）
    return embed


def build_control_embed(controller) -> discord.Embed:
    """🎛️ 控制台（刪舊貼新、永遠在最下面）：資訊只留佇列。音量在按鈕、狀態/主導/歌詞都不放。"""
    c = controller
    embed = discord.Embed(title="🎛️ 控制台", color=discord.Color.blurple(),
                          timestamp=datetime.datetime.now())
    q = c.stream_queue
    if q:
        lines = []
        for i, item in enumerate(q[:10], 1):
            dur = item.get('duration', 0)
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            by = item.get('requested_by', '')
            by_tag = (f" *by {by}*" if by and not by.startswith('Marvin')
                      else (" *🔮推薦*" if by.startswith('Marvin') else ""))
            lines.append(f"`{i}.` {item['title'][:45]} `[{dur_str}]`{by_tag}")
        if len(q) > 10:
            lines.append(f"*...以及另外 {len(q)-10} 首*")
        embed.add_field(name="📋 佇列", value="\n".join(lines), inline=False)
    else:
        embed.description = "佇列空。"
    return embed


class PlayControlView(discord.ui.View):
    """🎵 [Unified Stream Control] 播放控制台 + 佇列管理，合一版。"""

    VOL_STEP = 0.05   # 按鈕增減步進 5%（2026-06-04 改回 5% 細調；語音步進仍 10%）
    VOL_MIN  = 0.01
    VOL_MAX  = 1.00

    def __init__(self, controller: "VoiceController"):
        super().__init__(timeout=3600)
        self.controller = controller
        controller._active_views.add(self)

    def _build_embed(self) -> discord.Embed:
        """控制台 embed（歌曲資訊已拆到 build_song_embed 獨立貼文）。保留此名讓既有
        refresh 呼叫點沿用；內容＝控制台狀態（音量/狀態/佇列）。"""
        return build_control_embed(self.controller)

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    def _skip_current(self, vc):
        """跳歌：Plan 12 清 mixer 音樂層（_mixer_play_music 結束→播下一首）；舊路徑停 vc。"""
        c = self.controller
        if getattr(c, "_plan12", False) and getattr(c, "_mixer", None) is not None:
            c._mixer.clear_music()
        elif vc and vc.is_playing():
            vc.stop_playing()

    @discord.ui.button(label="🔉 -", style=discord.ButtonStyle.secondary, row=0)
    async def vol_down_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        c.stream_volume = max(self.VOL_MIN, round(c.stream_volume - self.VOL_STEP, 2))
        await self._refresh(interaction)

    @discord.ui.button(label="🔊 +", style=discord.ButtonStyle.secondary, row=0)
    async def vol_up_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        c.stream_volume = min(self.VOL_MAX, round(c.stream_volume + self.VOL_STEP, 2))
        await self._refresh(interaction)

    @discord.ui.button(label="⏭️ 下一首", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        if not c.stream_mode:
            await interaction.response.send_message("沒有歌曲在播放。", ephemeral=True)
            return
        self._skip_current(interaction.guild.voice_client)
        await self._refresh(interaction)

    @discord.ui.button(label="❤️ 喜歡", style=discord.ButtonStyle.success, row=0)
    async def like_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """對正在播的歌按讚（可 toggle）。likes 讓喜好擴散到多人、餵 autopilot 候選（次於點播者）。"""
        c = self.controller
        info = c._current_stream_info
        if not c.stream_mode or not info:
            await interaction.response.send_message("現在沒有正在播放的歌可以喜歡。", ephemeral=True)
            return
        mm = getattr(c.bot, "music_memory", None)
        liker = interaction.user.display_name
        state = mm.toggle_like(info, liker) if mm is not None else None
        title = (info.get("title") or "這首")[:40]
        if state is True:
            msg = f"❤️ 記下了，{liker} 喜歡「{title}」——之後會更常推這類的歌給你。"
        elif state is False:
            msg = f"💔 取消了對「{title}」的喜歡。"
        else:
            msg = "這首還沒被記錄，等它播一下再試。"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🙈 誤點刪除", style=discord.ButtonStyle.danger, row=0)
    async def misclick_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        """把「正在播放」的誤點歌從記憶抹除 + 加永久黑名單 + 跳下一首。

        誤點 ≠ skip：只反轉這次 record_play 的口味訊號，並用 video-id 黑名單擋掉
        之後自動點；不做 artist_skip（一次手滑不該連坐整個藝人方向）。
        """
        c = self.controller
        cur = c._current_stream_info
        if not c.stream_mode or not cur:
            await interaction.response.send_message("現在沒有正在播放的歌可以抹除。", ephemeral=True)
            return
        title = cur.get("title", "這首")
        mm = getattr(c.bot, "music_memory", None)
        if mm is not None:
            try:
                mm.undo_play(cur)
                url = cur.get("webpage_url") or cur.get("url") or ""
                if url:
                    mm.record_skipped_video_id(url)
            except Exception:
                pass
        self._skip_current(interaction.guild.voice_client)
        await self._refresh(interaction)
        await interaction.followup.send(
            f"🙈 已把「{title[:40]}」從記憶抹除，之後不會再自動點。", ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        self.controller._active_views.discard(self)


class ConsentView(discord.ui.View):
    """一次性同意 / 拒絕按鈕，只有目標成員可以操作。"""

    def __init__(self, consent_manager: "ConsentManager", display_name: str):
        super().__init__(timeout=600)
        self.cm = consent_manager
        self.display_name = display_name

    def _check_user(self, interaction: discord.Interaction) -> bool:
        return interaction.user.display_name == self.display_name

    @discord.ui.button(label="✅ 我同意", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._check_user(interaction):
            await interaction.response.send_message(
                f"這個同意請求是給 **{self.display_name}** 的。", ephemeral=True
            )
            return
        self.cm.set_consent(self.display_name, True)
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"✅ **{self.display_name}** 已同意，馬文開始處理你的語音。\n"
                f"使用 `/marvin_optout` 可隨時撤回。"
            ),
            view=None,
        )

    @discord.ui.button(label="❌ 拒絕", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._check_user(interaction):
            await interaction.response.send_message(
                f"這個同意請求是給 **{self.display_name}** 的。", ephemeral=True
            )
            return
        self.cm.set_consent(self.display_name, False)
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"🔇 **{self.display_name}** 已拒絕，馬文不會處理你的語音。\n"
                f"使用 `/marvin_optin` 可隨時同意。"
            ),
            view=None,
        )
