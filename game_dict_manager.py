import os
import google.generativeai as genai
from openai import AsyncOpenAI
import logging

logger = logging.getLogger(__name__)

class GameDictManager:
    """
    動態黑話字典管理器 (Operation Jargon Override)
    負責維護遊戲專屬的詞彙庫，避免 STT 辨識錯誤。
    """
    def __init__(self):
        self.dict_dir = "dictionaries"
        os.makedirs(self.dict_dir, exist_ok=True)
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.google_key = os.getenv("GOOGLE_API_KEY")
        
        # 🧪 [Brain Transplant] 配置不同 SDK
        if self.google_key:
            genai.configure(api_key=self.google_key)
            
        # 優先使用 Groq / Llama-3 來極速產生字典
        self.client = AsyncOpenAI(api_key=self.groq_key, base_url="https://api.groq.com/openai/v1") if self.groq_key else None

    async def get_or_create_dict(self, game_name: str) -> str:
        """取的或生成遊戲的字典字串"""
        if not game_name:
            return ""
        
        game_name = game_name.strip()
        file_path = os.path.abspath(os.path.join(self.dict_dir, f"{game_name}.txt"))
        print(f"🔍 [DictManager] 正在搜尋遊戲字典: {file_path}")
        
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                print(f"📖 [DictManager] 從本地快取載入成功。")
                return content
                
        logger.info(f"📚 [DictManager] 正在為《{game_name}》生成動態黑話字典...")
        prompt_template = f"""
        妳現在是一位專業的遊戲語音辨識 (STT) 聲學優化專家。
        請為遊戲《{game_name}》生成一份專用於語音辨識校正的「核心黑話字典」。

        【嚴格收錄限制】：
        妳只能輸出 50 到 80 個最重要的詞彙，並且必須嚴格符合以下三種類型之一：
        1. 音譯詞或特殊角色名 (例如：苦力怕、安德、直布羅陀)
        2. 該遊戲特有的非日常專有名詞 (例如：生怪磚、大電、附魔台)
        3. 玩家常用的中英文縮寫或短術語 (例如：TP、AFK、拉電、推車)

        【強制輸出格式】：
        請「只」回傳以半形逗號分隔的純文字字串。絕對不要出現重複的單字。若不足 50 個也沒關係，嚴格禁止為了湊數而輸出重複字串或相關日常詞彙。
        絕對不要包含任何問候語、解釋、條列符號或 Markdown 格式。
        """.strip()
        
        result = ""
        # 1. Groq API
        if self.client:
            try:
                response = await self.client.chat.completions.create(
                    model=os.getenv("LLM_PRIMARY_MODEL", "openai/gpt-oss-20b"), 
                    messages=[{"role": "user", "content": prompt_template}],
                    temperature=0.3
                )
                result = response.choices[0].message.content.strip()
                logger.info("✅ [DictManager] Groq (Llama-3.3) 字典生成成功。")
            except Exception as e:
                logger.warning(f"⚠️ [DictManager] Groq API 失敗，將降級至 Gemini: {e}")
        
        # 2. Fallback to Gemini 
        if not result:
            print(f"⚠️ [DictManager] Groq 失敗，嘗試使用 Gemini...")
            try:
                # 使用 .env 指定的 Flash 模型作為最穩定的高速生成模型
                gemini_flash_model = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
                gemini_model = genai.GenerativeModel(gemini_flash_model)
                response = await gemini_model.generate_content_async(prompt_template)
                result = response.text.strip()
                logger.info(f"✅ [DictManager] Gemini ({gemini_flash_model}) 字典生成成功。")
            except Exception as e:
                logger.error(f"❌ [DictManager] Gemini 生成失敗: {e}")
                print(f"❌ [DictManager] Gemini 亦生成失敗: {e}")
                return ""
                
        # 3. 儲存與去重
        if result:
            try:
                # 🚀 [Operation Jargon Override] 強制去重與清理
                raw_words = [w.strip() for w in result.replace("`", "").split(",") if w.strip()]
                unique_words = []
                seen = set()
                for w in raw_words:
                    if w not in seen:
                        unique_words.append(w)
                        seen.add(w)
                
                final_result = ",".join(unique_words)
                
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(final_result)
                print(f"💾 [DictManager] 字典已成功存入 (去重後共 {len(unique_words)} 詞): {file_path}")
                return final_result
            except Exception as e:
                print(f"❌ [DictManager] 檔案寫入失敗: {e}")
        return result
