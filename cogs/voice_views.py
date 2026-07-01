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
        c = self.controller
        info = c._current_stream_info
        # 串流已啟動但第一首還沒被 loop pop 到時，借用佇列第一首當 preview
        pending_first = (not info and c.stream_mode and c.stream_queue)
        if pending_first:
            info = c.stream_queue[0]
        vol_pct = int(c.stream_volume * 100)
        state = "⏸️ 暫停中" if c.stream_paused else ("⏳ 載入中" if pending_first else ("▶️ 播放中" if c.stream_mode else "⏹️ 停止"))
        comment = c._current_stream_comment
        embed = discord.Embed(
            title="🎛️ 串流控制台",
            description=f"「{comment}」" if comment else None,
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now()
        )
        if info:
            dur = info.get('duration', 0)
            dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            if info.get('thumbnail'):
                embed.set_image(url=info['thumbnail'])  # 串流歌曲封面（滿寬幅大圖）
            embed.add_field(name="🎵 歌曲", value=f"`{info['title']}`", inline=False)
            embed.add_field(name="👤 頻道", value=f"`{info['uploader']}`", inline=True)
            embed.add_field(name="⏱️ 時長", value=f"`{dur_str}`", inline=True)
            # 點播者 + 歷史統計
            requester = info.get('requested_by', '')
            if requester:
                req_display = requester
                if hasattr(c, 'bot') and hasattr(c.bot, 'music_memory') and not requester.startswith('Marvin'):
                    mm = c.bot.music_memory
                    key = mm._key(info)
                    song_data = mm._data.get('songs', {}).get(key, {})
                    play_count = song_data.get('requesters', {}).get(requester, 0)
                    user_plays = [p for p in song_data.get('plays', []) if p['by'] == requester]
                    slots = [p['time_slot'] for p in user_plays]
                    common_slot = max(set(slots), key=slots.count) if slots else None
                    if play_count > 1:
                        req_display += f"　第 {play_count} 次"
                    if common_slot:
                        req_display += f" · 常在{common_slot}聽"
                embed.add_field(name="🙋 點播", value=f"`{req_display}`", inline=False)
        else:
            embed.description = "目前沒有歌曲在播放。"
        embed.add_field(name="🔊 音量", value=f"`{vol_pct}%`", inline=True)
        embed.add_field(name="狀態", value=state, inline=True)
        # 🎚️ 誰的風格在主導自動推薦（多人種子輪替）
        try:
            mc = c.bot.cogs.get('MusicCog') if hasattr(c, 'bot') else None
            members = c.get_online_members() if hasattr(c, 'get_online_members') else []
            if mc and members and c.stream_mode:
                import seed_rotation
                _swap = 3
                _epoch = getattr(mc, '_seed_epoch', 0)
                _since = getattr(mc, '_auto_since_manual', 99)
                if _since < _swap and getattr(mc, '_last_user_song_seed', None):
                    _req = getattr(mc, '_last_user_song_requester', '') or members[0]
                    dom = f"🔥 跟 `{_req}` 最近點歌（{_swap - _since} 首後開始輪替）"
                else:
                    _primary = seed_rotation.primary_member(members, _epoch, _swap) or members[0]
                    dom = f"`{_primary}` 的口味（{_swap - (_epoch % _swap)} 首後換人）"
                embed.add_field(name="🎚️ 推薦主導", value=dom, inline=False)
        except Exception:
            pass
        q = c.stream_queue
        if q:
            lines = []
            for i, item in enumerate(q[:10], 1):
                dur = item.get('duration', 0)
                dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
                by = item.get('requested_by', '')
                by_tag = f" *by {by}*" if by and not by.startswith('Marvin') else (" *🔮推薦*" if by.startswith('Marvin') else "")
                lines.append(f"`{i}.` {item['title'][:45]} `[{dur_str}]`{by_tag}")
            if len(q) > 10:
                lines.append(f"*...以及另外 {len(q)-10} 首*")
            embed.add_field(name="📋 佇列", value="\n".join(lines), inline=False)
        if c._current_lyrics and not pending_first:
            MAX = 900
            text = c._current_lyrics
            if len(text) > MAX:
                text = text[:MAX].rsplit('\n', 1)[0] + "\n*...（更多歌詞請自行查詢）*"
            embed.add_field(name="📝 歌詞", value=text, inline=False)
        embed.set_footer(text=f"待播 {len(q)} 首 | 歷史 {len(c.stream_history)} 首")
        return embed

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
