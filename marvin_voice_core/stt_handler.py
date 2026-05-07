import asyncio
import os
import time

class STTHandler:
    def __init__(self, whisper_model=None):
        self.whisper_model = whisper_model
        # 取得當前檔案路徑，以便定位 swift 腳本
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.swift_script = os.path.join(self.base_dir, "macos_stt.swift")

    async def transcribe_hybrid(self, wav_path, speaker_name="Unknown", game_dict_string="", initial_prompt=""):
        """
        混合型 STT 處理器：優先使用 macOS Swift，失敗或無結果則回退至 Faster-Whisper。
        """
        raw_text = ""
        used_engine = "None"
        
        # 1. 優先嘗試 macOS Native Swift STT
        print(f"🎙️ [Core_STT] 啟動 macOS Native Swift STT (Speaker: {speaker_name})...", flush=True)
        try:
            env = os.environ.copy()
            base_context = "Marvin,馬文,碼文,麻文,艾馬文,馬問,馬門,嗨馬文,Hi Marvin"
            if game_dict_string:
                env["STT_CONTEXT_STRINGS"] = f"{base_context},{game_dict_string}"
            else:
                env["STT_CONTEXT_STRINGS"] = base_context
                
            process = await asyncio.create_subprocess_exec(
                "swift", self.swift_script, wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                output_lines = stdout.decode('utf-8').splitlines()
                for line in output_lines:
                    line = line.strip()
                    if line and not (line.startswith("🔍") or line.startswith("✅") or line.startswith("❌") or line.startswith("DEBUG:") or line.startswith("📚")):
                        raw_text = line
                if raw_text:
                    used_engine = "Swift"
                    print(f"✅ [Core_STT Output] {speaker_name}: {raw_text} (Engine: Swift)", flush=True)
            else:
                print(f"❌ [Core_STT Swift Error] macOS STT 執行失敗 (Code: {process.returncode})", flush=True)
        except Exception as e:
            print(f"🚨 [Core_STT Swift Exception] macOS STT 過程崩潰: {e}", flush=True)

        # 2. 備援方案：Faster-Whisper (若 Swift 無結果)
        if not raw_text and self.whisper_model:
            print(f"🎙️ [Core_STT] 啟動備援 Faster-Whisper 辨識 (Speaker: {speaker_name})...", flush=True)
            try:
                whisper_prompt = initial_prompt or "Marvin, Hi Marvin, 馬文, 艾馬文, 艾瑪文, 幫忙, 玩家對話。"
                if game_dict_string:
                    whisper_prompt += f", {game_dict_string}"
                
                # 🚀 [Optimization] beam_size=1 確保極速辨識模式
                segments, info = await asyncio.to_thread(
                    self.whisper_model.transcribe, 
                    wav_path, 
                    beam_size=1,
                    language="zh",
                    initial_prompt=whisper_prompt,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500)
                )
                raw_text = "".join([segment.text for segment in segments]).strip()
                if raw_text:
                    used_engine = "Whisper"
                    print(f"✅ [Core_STT Output] {speaker_name}: {raw_text} (Engine: Whisper)", flush=True)
            except Exception as e:
                print(f"🚨 [Core_STT Whisper Error] 辨識過程崩潰: {e}", flush=True)

        return raw_text, used_engine
