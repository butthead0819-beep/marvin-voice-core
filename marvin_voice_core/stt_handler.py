import asyncio
import json
import os
from utils import is_whisper_hallucination

# Prompt used when checking Swift output for hallucinations.
# Mirrors the context strings injected into STT_CONTEXT_STRINGS so we can
# detect when Swift echoes back its own hint words instead of real speech.
_SWIFT_HAL_PROMPT = "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 嗨馬文, 馬問, 麻文, 碼文, 麻文"

_META_PREFIX = "__META__ "


class STTHandler:
    def __init__(self, whisper_model=None):
        self.whisper_model = whisper_model
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.swift_script = os.path.join(self.base_dir, "macos_stt.swift")

    # ── STTService Protocol ───────────────────────────────────────────────────

    async def transcribe(
        self,
        wav_path: str,
        *,
        speaker: str = "Unknown",
        context: str = "",
    ) -> tuple[str, str, dict]:
        """STTService Protocol entry point. Returns (text, engine_name, meta)."""
        return await self.transcribe_hybrid(wav_path, speaker_name=speaker, game_dict_string=context)

    # ── Implementation ────────────────────────────────────────────────────────

    async def transcribe_hybrid(
        self,
        wav_path: str,
        speaker_name: str = "Unknown",
        game_dict_string: str = "",
        initial_prompt: str = "",
    ) -> tuple[str, str, dict]:
        """Hybrid STT: macOS Swift first, Faster-Whisper fallback.

        meta dict contains Swift acoustic/prosody features (confidence, pause, speaking_rate)
        on macOS 13+; empty for Whisper fallback.
        """
        raw_text = ""
        used_engine = "None"
        meta: dict = {}

        # 1. macOS Native Swift STT
        print(f"🎙️ [Core_STT] 啟動 macOS Native Swift STT (Speaker: {speaker_name})...", flush=True)
        try:
            env = os.environ.copy()
            base_context = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin,Siri,阿公,瑪利歐"
            env["STT_CONTEXT_STRINGS"] = (
                f"{base_context},{game_dict_string}" if game_dict_string else base_context
            )
            process = await asyncio.create_subprocess_exec(
                "swift", self.swift_script, wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                _skip = ("🔍", "✅", "❌", "DEBUG:", "📚", _META_PREFIX)
                for line in stdout.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(_META_PREFIX):
                        try:
                            meta = json.loads(line[len(_META_PREFIX):])
                        except json.JSONDecodeError:
                            pass
                        continue
                    if any(line.startswith(p) for p in _skip):
                        continue
                    raw_text = line
                if raw_text:
                    if is_whisper_hallucination(raw_text, _SWIFT_HAL_PROMPT):
                        print(f"🚫 [Core_STT] {speaker_name}: Swift 幻覺轉錄已過濾 '{raw_text[:40]}'", flush=True)
                        raw_text = ""
                        meta = {}
                    else:
                        used_engine = "Swift"
                        print(f"✅ [Core_STT] {speaker_name}: {raw_text} (Swift)", flush=True)
            else:
                print(f"❌ [Core_STT] Swift 失敗 (code {process.returncode})", flush=True)
        except Exception as exc:
            print(f"🚨 [Core_STT] Swift 崩潰: {exc}", flush=True)

        # 2. Faster-Whisper fallback
        if not raw_text and self.whisper_model:
            print(f"🎙️ [Core_STT] 啟動備援 Faster-Whisper (Speaker: {speaker_name})...", flush=True)
            try:
                prompt = initial_prompt or "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 幫忙, 玩家對話。"
                if game_dict_string:
                    prompt += f", {game_dict_string}"
                segments, _ = await asyncio.to_thread(
                    self.whisper_model.transcribe,
                    wav_path,
                    beam_size=1,
                    language=None,
                    initial_prompt=prompt,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                raw_text = "".join(s.text for s in segments).strip()
                if raw_text:
                    used_engine = "Whisper"
                    meta = {}  # Whisper path: no acoustic meta
                    print(f"✅ [Core_STT] {speaker_name}: {raw_text} (Whisper)", flush=True)
            except Exception as exc:
                print(f"🚨 [Core_STT] Whisper 崩潰: {exc}", flush=True)

        return raw_text, used_engine, meta
