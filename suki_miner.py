import aiofiles
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# [Operation Distillation] 全局資料集路徑
DATASET_PATH = "records/suki_golden_dataset.jsonl"

async def log_distillation_data(system_prompt: str, user_prompt: str, assistant_response: str):
    """
    [Operation Distillation] 
    非同步寫入推論結果至黃金資料集。遵循 Messages 格式以利未來微調。
    """
    try:
        # 確保目錄存在
        os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
        
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": assistant_response}
            ],
            "timestamp": datetime.now().isoformat()
        }
        
        # 🧪 [Strict Async I/O] 使用 aiofiles 進行非阻塞附加寫入
        async with aiofiles.open(DATASET_PATH, mode="a", encoding="utf-8") as f:
            await f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            
    except Exception as e:
        logger.error(f"❌ [Distillation Failed] 無法寫入資料集: {e}")
