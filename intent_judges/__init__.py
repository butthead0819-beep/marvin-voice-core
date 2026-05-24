"""Parallel judges race — bus 入口前的多路 race，winner 直接 dispatch。

設計見 memory/speculative_stt_pipeline.md。

Judges：
  J1 RegexJudge      ── 純 regex schema match（這個檔）
  J2 SmallLLMJudge   ── Groq 8B classifier（TODO）
  J3 ClenerJudge     ── 現有 stt_cleaner 包裝（TODO）

Race coordinator：TODO（asyncio.wait + cancel）。本 package 暫不接 bus；
完成 unit test 後再寫 voice_controller 入口的 race call。
"""
