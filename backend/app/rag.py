from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class KnowledgeHit:
    source: str
    section: str
    score: float
    content: str


class KnowledgeBase:
    def __init__(self) -> None:
        self._ready = False
        self._last_error: str | None = None
        self._fingerprint: str | None = None

    @property
    def knowledge_dir(self) -> Path:
        return Path(os.getenv("KNOWLEDGE_DIR", "/knowledge"))

    @property
    def qdrant_url(self) -> str:
        return os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")

    @property
    def collection(self) -> str:
        return os.getenv("QDRANT_COLLECTION", "knowledge_base")

    @property
    def embedding_model(self) -> str:
        return os.getenv("EMBEDDING_MODEL", "nomic-embed-text:latest")

    @property
    def embedding_base_url(self) -> str:
        raw = os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL") or "http://host.docker.internal:11434"
        base = raw.rstrip("/")
        if base.endswith("/v1"):
            return base[: -len("/v1")]
        return base

    def _docs_fingerprint(self) -> str:
        if not self.knowledge_dir.exists():
            return "missing"

        md_files = sorted(self.knowledge_dir.rglob("*.md"))
        parts: list[str] = []
        for f in md_files:
            st = f.stat()
            parts.append(f"{f.relative_to(self.knowledge_dir)}:{int(st.st_mtime)}:{st.st_size}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _load_sections(self) -> list[dict[str, str]]:
        if not self.knowledge_dir.exists():
            return []

        sections: list[dict[str, str]] = []
        for md in sorted(self.knowledge_dir.rglob("*.md")):
            rel = str(md.relative_to(self.knowledge_dir))
            text = md.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()

            heading_stack: list[str] = []
            current_title = "Introduction"
            current_body: list[str] = []
            in_code_block = False

            def flush() -> None:
                body = "\n".join(current_body).strip()
                if body:
                    sections.append(
                        {
                            "source": rel,
                            "section": current_title,
                            "content": body,
                        }
                    )

            for line in lines:
                stripped = line.strip()
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    current_body.append(line)
                    continue

                if not in_code_block and line.startswith("#"):
                    flush()
                    level = len(line) - len(line.lstrip("#"))
                    title = line.lstrip("#").strip() or "Untitled"
                    if level <= 0:
                        level = 1
                    while len(heading_stack) >= level:
                        heading_stack.pop()
                    heading_stack.append(title)
                    current_title = " > ".join(heading_stack)
                    current_body = []
                else:
                    current_body.append(line)
            flush()
        return sections

    async def _embed(self, client: httpx.AsyncClient, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}

        endpoints = [
            (f"{self.embedding_base_url}/api/embed", {"model": self.embedding_model, "input": text}),
            (f"{self.embedding_base_url}/api/embeddings", {"model": self.embedding_model, "prompt": text}),
        ]

        last_error: str | None = None
        for url, payload in endpoints:
            try:
                response = await client.post(url, headers=headers, json=payload, timeout=45.0)
                if response.status_code >= 400:
                    body = response.text.strip().replace("\n", " ")
                    last_error = f"{url} returned {response.status_code}: {body[:180]}"
                    continue

                data = response.json()
                if "embeddings" in data and data["embeddings"]:
                    vector = data["embeddings"][0]
                    if isinstance(vector, list) and vector:
                        return [float(x) for x in vector]
                if "embedding" in data and isinstance(data["embedding"], list) and data["embedding"]:
                    return [float(x) for x in data["embedding"]]
                last_error = f"{url} returned unexpected embedding format"
            except httpx.HTTPError as exc:
                last_error = f"{url} failed: {str(exc)}"

        raise RuntimeError(last_error or "embedding request failed")

    async def _recreate_collection(self, client: httpx.AsyncClient, vector_size: int) -> None:
        await client.delete(f"{self.qdrant_url}/collections/{self.collection}", timeout=30.0)

        create_payload = {
            "vectors": {
                "size": vector_size,
                "distance": "Cosine",
            }
        }
        response = await client.put(
            f"{self.qdrant_url}/collections/{self.collection}",
            json=create_payload,
            timeout=30.0,
        )
        response.raise_for_status()

    async def build_index_if_needed(self) -> None:
        fingerprint = self._docs_fingerprint()
        if self._ready and self._fingerprint == fingerprint:
            return

        sections = self._load_sections()
        if not sections:
            self._ready = False
            self._last_error = "No markdown files found in knowledge directory"
            self._fingerprint = fingerprint
            return

        async with httpx.AsyncClient() as client:
            # Use the first chunk to determine vector dimensions.
            first_vector = await self._embed(client, sections[0]["content"])
            await self._recreate_collection(client, len(first_vector))

            points: list[dict[str, Any]] = []
            for i, section in enumerate(sections):
                vector = first_vector if i == 0 else await self._embed(client, section["content"])
                point_id = int(hashlib.sha256(f"{section['source']}::{section['section']}::{i}".encode("utf-8")).hexdigest()[:16], 16)
                points.append(
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": section,
                    }
                )

            upsert = await client.put(
                f"{self.qdrant_url}/collections/{self.collection}/points?wait=true",
                json={"points": points},
                timeout=120.0,
            )
            upsert.raise_for_status()

        self._ready = True
        self._last_error = None
        self._fingerprint = fingerprint

    async def retrieve(self, query: str, top_k: int = 4) -> tuple[list[KnowledgeHit], str | None]:
        try:
            await self.build_index_if_needed()
        except Exception as exc:
            self._ready = False
            self._last_error = str(exc)
            return [], self._last_error

        if not self._ready:
            return [], self._last_error

        async with httpx.AsyncClient() as client:
            try:
                vector = await self._embed(client, query)
                search_payload = {
                    "vector": vector,
                    "limit": top_k,
                    "with_payload": True,
                }
                response = await client.post(
                    f"{self.qdrant_url}/collections/{self.collection}/points/search",
                    json=search_payload,
                    timeout=30.0,
                )
                response.raise_for_status()
            except Exception as exc:
                return [], str(exc)

        data = response.json()
        results = data.get("result", [])
        hits: list[KnowledgeHit] = []
        for item in results:
            payload = item.get("payload") or {}
            hits.append(
                KnowledgeHit(
                    source=str(payload.get("source", "unknown")),
                    section=str(payload.get("section", "Unknown")),
                    content=str(payload.get("content", "")),
                    score=float(item.get("score", 0.0)),
                )
            )

        return hits, None


knowledge_base = KnowledgeBase()
