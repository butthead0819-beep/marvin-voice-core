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


def build_song_embed(controller) -> discord.Embed:
    """🎵 歌曲資訊卡（小、精緻、每首一則、持久保留）：歌名 + 馬文評論 + 歌手·時長 + 點播者 + 小封面。

    純資訊、無按鈕；串流迴圈每首歌貼一則新的、不刪＝頻道裡留下播放紀錄。
    """
    c = controller
    info = c._current_stream_info
    if not info and c.stream_mode and c.stream_queue:   # 首曲還沒 pop → 借佇列第一首 preview
        info = c.stream_queue[0]
    embed = discord.Embed(color=discord.Color.blurple(), timestamp=datetime.datetime.now())
    if not info:
        embed.description = "目前沒有歌曲在播放。"
        return embed
    embed.title = f"🎵 {info['title']}"
    comment = c._current_stream_comment
    if comment:
        embed.description = f"「{comment}」"          # 馬文對這首的評論
    if info.get('thumbnail'):
        embed.set_thumbnail(url=info['thumbnail'])    # 小縮圖（右上角）非滿寬大圖 → 精緻
    dur = info.get('duration', 0)
    dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
    embed.add_field(name="👤 歌手", value=f"`{info['uploader']}`", inline=True)
    embed.add_field(name="⏱️ 時長", value=f"`{dur_str}`", inline=True)
    requester = info.get('requested_by', '')
    if requester:
        req_display = requester
        try:
            mm = getattr(getattr(c, 'bot', None), 'music_memory', None)
            if mm is not None and not requester.startswith('Marvin'):
                song_data = mm._data.get('songs', {}).get(mm._key(info), {})
                play_count = song_data.get('requesters', {}).get(requester, 0)
                slots = [p['time_slot'] for p in song_data.get('plays', []) if p['by'] == requester]
                common_slot = max(set(slots), key=slots.count) if slots else None
                if play_count > 1:
                    req_display += f"　第 {play_count} 次"
                if common_slot:
                    req_display += f" · 常在{common_slot}聽"
        except Exception:
            pass
        embed.add_field(name="🙋 點播", value=f"`{req_display}`", inline=False)
    return embed


def build_control_embed(controller) -> discord.Embed:
    """🎛️ 控制台狀態（刪舊貼新、永遠在最下面）：音量 + 狀態 + 佇列。不含歌曲資訊/推薦主導/歌詞。"""
    c = controller
    vol_pct = int(c.stream_volume * 100)
    pending_first = (not c._current_stream_info and c.stream_mode and c.stream_queue)
    state = "⏸️ 暫停中" if c.stream_paused else (
        "⏳ 載入中" if pending_first else ("▶️ 播放中" if c.stream_mode else "⏹️ 停止"))
    embed = discord.Embed(title="🎛️ 控制台", color=discord.Color.blurple(),
                          timestamp=datetime.datetime.now())
    embed.add_field(name="🔊 音量", value=f"`{vol_pct}%`", inline=True)
    embed.add_field(name="狀態", value=state, inline=True)
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
    embed.set_footer(text=f"待播 {len(q)} 首 | 歷史 {len(c.stream_history)} 首")
    return embed


class PlayControlView(discord.ui.View):
    """🎵 [Unified Stream Control] 播放控制台 + 佇列管理，合一版。"""

    VOL_STEP = 0.05   # 按鈕增減步進 5%（2026-06-04 改回 5% 細調；語音步進仍 10%）
    VOL_MIN  = 0.01
    VOL_MAX  = 1.00

    def __init__(self, controller: "VoiceController"):
        super().__init__(timeout=3600)
        self.controller = controller
        self._selected_index: int | None = None
        self._update_pause_label()
        self._rebuild_select()
        controller._active_views.add(self)

    def _rebuild_select(self):
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        q = self.controller.stream_queue
        if q:
            options = [
                discord.SelectOption(
                    label=f"{i+1}. {item['title'][:80]}",
                    description=(item['uploader'] or '')[:50],
                    value=str(i),
                )
                for i, item in enumerate(q[:25])
            ]
            select = discord.ui.Select(placeholder="從佇列選擇歌曲…", options=options, row=0)
            select.callback = self._on_select
            self.add_item(select)
        self.jump_button.disabled = not bool(q)
        self.delete_button.disabled = not bool(q)

    async def _on_select(self, interaction: discord.Interaction):
        self._selected_index = int(interaction.data['values'][0])
        await interaction.response.defer()

    def _update_pause_label(self):
        self.pause_resume_button.label = "▶️ 播放" if self.controller.stream_paused else "⏸️ 暫停"

    def _build_embed(self) -> discord.Embed:
        """控制台 embed（歌曲資訊已拆到 build_song_embed 獨立貼文）。保留此名讓既有
        refresh 呼叫點沿用；內容＝控制台狀態（音量/狀態/佇列）。"""
        return build_control_embed(self.controller)

    async def _refresh(self, interaction: discord.Interaction):
        self._update_pause_label()
        self._rebuild_select()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    def _skip_current(self, vc):
        """跳歌：Plan 12 清 mixer 音樂層（_mixer_play_music 結束→播下一首）；舊路徑停 vc。"""
        c = self.controller
        if getattr(c, "_plan12", False) and getattr(c, "_mixer", None) is not None:
            c._mixer.clear_music()
        elif vc and vc.is_playing():
            vc.stop_playing()

    @discord.ui.button(label="⏮️ 上一首", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        if len(c.stream_history) < 2:
            await interaction.response.send_message("沒有上一首了。歷史，就像宇宙一樣，在此終結。", ephemeral=True)
            return
        if c._current_stream_info:
            c.stream_queue.insert(0, c._current_stream_info)
            c.stream_history.pop()
        prev = c.stream_history.pop()
        c.stream_queue.insert(0, prev)
        self._skip_current(interaction.guild.voice_client)
        await self._refresh(interaction)

    @discord.ui.button(label="⏸️ 暫停", style=discord.ButtonStyle.primary, row=1)
    async def pause_resume_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("沒有連線中的語音頻道。", ephemeral=True)
            return
        _p12 = getattr(c, "_plan12", False) and getattr(c, "_mixer", None) is not None
        if c.stream_paused:
            c._mixer.set_paused(False) if _p12 else vc.resume()
            c.stream_paused = False
        else:
            c._mixer.set_paused(True) if _p12 else vc.pause()
            c.stream_paused = True
        await self._refresh(interaction)

    @discord.ui.button(label="⏭️ 下一首", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        if not c.stream_mode:
            await interaction.response.send_message("沒有歌曲在播放。", ephemeral=True)
            return
        self._skip_current(interaction.guild.voice_client)
        await self._refresh(interaction)

    @discord.ui.button(label="🔉 -10%", style=discord.ButtonStyle.secondary, row=2)
    async def vol_down_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        c.stream_volume = max(self.VOL_MIN, round(c.stream_volume - self.VOL_STEP, 2))
        await self._refresh(interaction)

    @discord.ui.button(label="🔊 +10%", style=discord.ButtonStyle.secondary, row=2)
    async def vol_up_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        c = self.controller
        c.stream_volume = min(self.VOL_MAX, round(c.stream_volume + self.VOL_STEP, 2))
        await self._refresh(interaction)

    @discord.ui.button(label="⏭️ 跳到此曲", style=discord.ButtonStyle.primary, row=2)
    async def jump_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self._selected_index is None:
            await interaction.response.send_message("請先從選單選擇一首歌曲。", ephemeral=True)
            return
        idx = self._selected_index
        q = self.controller.stream_queue
        if idx >= len(q):
            await interaction.response.send_message("該歌曲已不在佇列中。", ephemeral=True)
            return
        self.controller.stream_queue = q[idx:]
        self._skip_current(interaction.guild.voice_client)
        self._selected_index = None
        await self._refresh(interaction)

    @discord.ui.button(label="🗑️ 刪除", style=discord.ButtonStyle.danger, row=2)
    async def delete_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self._selected_index is None:
            await interaction.response.send_message("請先從選單選擇一首歌曲。", ephemeral=True)
            return
        idx = self._selected_index
        q = self.controller.stream_queue
        if idx >= len(q):
            await interaction.response.send_message("該歌曲已不在佇列中。", ephemeral=True)
            return
        q.pop(idx)
        self._selected_index = None
        await self._refresh(interaction)

    @discord.ui.button(label="🙈 誤點抹除", style=discord.ButtonStyle.danger, row=3)
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
