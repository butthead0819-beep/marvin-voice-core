import chromadb
from chromadb.config import Settings


class VectorStore:
    def __init__(self, persist_dir: str = ".chroma_db"):
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection("marvin_transcripts")

    def upsert(self, speaker: str, guild_id: int, text: str, doc_id: str) -> None:
        self._col.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[{"speaker": speaker, "guild_id": str(guild_id)}],
        )

    def search(self, speaker: str, guild_id: int, query: str, top_k: int = 3) -> list[str]:
        count = self._col.count()
        if count == 0:
            return []
        # 過濾特定 speaker + guild
        where = {"$and": [
            {"speaker": {"$eq": speaker}},
            {"guild_id": {"$eq": str(guild_id)}},
        ]}
        # n_results 不能超過實際文件數
        n = min(top_k, count)
        results = self._col.query(
            query_texts=[query],
            n_results=n,
            where=where,
        )
        docs = results.get("documents", [[]])[0]
        return docs if docs else []

    def delete_speaker(self, speaker: str, guild_id: int) -> None:
        results = self._col.get(
            where={"$and": [
                {"speaker": {"$eq": speaker}},
                {"guild_id": {"$eq": str(guild_id)}},
            ]}
        )
        ids = results.get("ids", [])
        if ids:
            self._col.delete(ids=ids)

    # ── Companion bridge 介面（直接暴露 ChromaDB 原生語意）────────────────────

    def get_all(self, speaker: str, guild_id: int, limit: int = 20) -> list[dict]:
        """列出某位說話者在某 guild 的所有記憶，回傳 [{id, document, metadata}, ...]。"""
        results = self._col.get(
            where={"$and": [
                {"speaker": {"$eq": speaker}},
                {"guild_id": {"$eq": str(guild_id)}},
            ]},
            limit=limit,
        )
        ids = results.get("ids", []) or []
        docs = results.get("documents", []) or []
        metas = results.get("metadatas", []) or []
        return [
            {"id": ids[i], "document": docs[i], "metadata": metas[i]}
            for i in range(len(ids))
        ]

    def get_profiles_bulk(self, speaker_ids: list[str], guild_id: str | int) -> list[str]:
        """回傳多個 speaker 的 profile 字串列表，跳過無 profile 的成員。

        Args:
            speaker_ids: 要查詢的 speaker ID 列表。
            guild_id: Guild ID（支援 str 或 int）。

        Returns:
            list[str]：每個元素是 document 文字內容，無 profile 的成員不列入。
        """
        profiles: list[str] = []
        for speaker_id in speaker_ids:
            results = self.get_all(speaker_id, int(guild_id), limit=1)
            if results:
                profiles.append(results[0]["document"])
        return profiles

    def delete(self, doc_id: str) -> None:
        """刪除單一文件；不存在時無動作（ChromaDB delete 對未知 id 本身就是 no-op）。"""
        try:
            self._col.delete(ids=[doc_id])
        except Exception as e:
            # 防禦性容錯：底層例外不丟出，但要 log 出來以免 ChromaDB 故障被吞掉
            import logging
            logging.getLogger(__name__).warning(
                f"[VectorStore.delete] doc_id={doc_id!r} 失敗（已吞例外）: {e}"
            )

    def update(self, doc_id: str, metadata: dict) -> None:
        """更新單一文件的 metadata；保留原有 keys，僅覆蓋傳入的欄位。

        ⚠️ 已知 race window（/review 2026-05-14）：
        這是 get-merge-write 三段式操作，get 與 update 之間若有並行寫入
        會被本次 merge 覆蓋。ChromaDB 沒有 atomic partial update API。
        companion 為單一使用者，實務上極難碰到；多 client 同時改同一筆
        metadata 才會出現，目前 v1 接受此風險。Lane B2 之後若加入多人協作
        場景，這裡需要外掛 asyncio.Lock 或改用 ChromaDB 的 upsert + 文件
        版本欄位來防覆蓋。
        """
        existing = self._col.get(ids=[doc_id])
        existing_metas = existing.get("metadatas", []) or []
        if not existing_metas:
            return
        merged = dict(existing_metas[0] or {})
        merged.update(metadata)
        self._col.update(ids=[doc_id], metadatas=[merged])
