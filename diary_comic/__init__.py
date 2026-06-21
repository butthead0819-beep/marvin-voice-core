"""diary_comic — 語音日誌 → 不對等漫畫頁（B 架構骨架）。

資料流：
  records/chat_summary_log.txt
    → parser.parse_log()  : 結構化 DiaryEntry + heat_score + group_by_hour
    → (每格出圖 stub)      : 一篇日記一格（之後接 nano-banana）
    → layout.compose_page(): 不對等拼版，大小=熱度，馬文碎念疊字
"""
