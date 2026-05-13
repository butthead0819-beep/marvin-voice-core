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
