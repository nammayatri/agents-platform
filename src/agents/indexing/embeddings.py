"""Embedding-based semantic code search using FAISS.

Generates embeddings using sentence-transformers (all-MiniLM-L6-v2, 384d, CPU)
and stores them in a FAISS index for fast similarity search.

Supports incremental updates via mtime-based cache.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories to skip
SKIP_DIRS: set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".agent_index",
}

# Supported extensions for embedding
EMBED_EXTENSIONS: set[str] = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".rb", ".php", ".swift",
    ".kt", ".scala", ".cs", ".sql", ".sh", ".bash", ".zsh",
    ".yaml", ".yml", ".toml", ".json", ".md", ".rst", ".txt",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
}

MAX_FILE_SIZE = 500 * 1024  # 500KB


@dataclass
class SearchResult:
    """A single semantic search result."""
    file_path: str
    line_start: int
    line_end: int
    snippet: str
    score: float


@dataclass
class CodeChunk:
    """A chunk of code for embedding."""
    file_path: str
    line_start: int
    line_end: int
    content: str


class EmbeddingIndex:
    """FAISS-backed semantic search index for a code repository.

    Uses sentence-transformers with all-MiniLM-L6-v2 for embedding
    generation and FAISS for vector similarity search.
    """

    def __init__(self):
        self._model = None
        self._index = None
        self._chunks: list[CodeChunk] = []
        self._mtimes: dict[str, float] = {}
        self._cache_dir: str | None = None
        self._initialized = False

    def _ensure_model(self):
        """Lazy-load the sentence-transformer model."""
        if self._model is not None:
            return

        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("embeddings: loaded all-MiniLM-L6-v2 model")
        except ImportError:
            logger.warning("sentence-transformers not installed, semantic search unavailable")
            raise
        except Exception as e:
            logger.error("Failed to load embedding model: %s", e)
            raise

    def build_index(self, repo_path: str, *, cache_dir: str | None = None) -> None:
        """Build or incrementally update the embedding index for a repository.

        Chunks files into ~30-line blocks with 10-line overlap,
        generates embeddings, and builds a FAISS index.

        Args:
            repo_path: Path to the repository root to index.
            cache_dir: Optional shared cache directory for persistent indexes.
                       If not provided, defaults to ``repo_path/.agent_index/``.
        """
        import numpy as np

        self._ensure_model()

        repo = Path(repo_path)
        self._cache_dir = cache_dir or str(repo / ".agent_index")
        os.makedirs(self._cache_dir, exist_ok=True)

        # Load previous state
        old_mtimes = self._load_mtimes()
        old_chunks = self._load_chunks()

        # Collect files
        files = self._collect_files(repo)
        current_mtimes: dict[str, float] = {}

        # Determine changed files
        changed_files = set()
        for file_path in files:
            try:
                mtime = os.path.getmtime(file_path)
                rel_path = os.path.relpath(file_path, repo_path)
                current_mtimes[rel_path] = mtime
                if rel_path not in old_mtimes or old_mtimes[rel_path] != mtime:
                    changed_files.add(rel_path)
            except OSError:
                continue

        # Keep unchanged chunks, re-chunk changed files
        kept_chunks = [c for c in old_chunks if c.file_path not in changed_files]
        new_chunks = []

        for file_path in files:
            rel_path = os.path.relpath(file_path, repo_path)
            if rel_path not in changed_files:
                continue

            try:
                content = Path(file_path).read_text(errors="replace")
                chunks = self._chunk_file(rel_path, content)
                new_chunks.extend(chunks)
            except Exception as e:
                logger.debug("Failed to chunk %s: %s", file_path, e)

        self._chunks = kept_chunks + new_chunks

        if not self._chunks:
            logger.warning("embeddings: no chunks to index")
            return

        # Generate embeddings for all chunks
        texts = [c.content for c in self._chunks]
        embeddings = self._model.encode(texts, show_progress_bar=False, batch_size=64)
        embeddings = np.array(embeddings, dtype=np.float32)

        # Normalize for cosine similarity
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings = embeddings / norms

        # Build FAISS index
        try:
            import faiss
            dim = embeddings.shape[1]
            self._index = faiss.IndexFlatIP(dim)  # Inner product = cosine after normalization
            self._index.add(embeddings)
        except ImportError:
            logger.warning("faiss not installed, semantic search will be slow")
            self._index = None
            self._embeddings = embeddings  # Fallback to numpy

        # Save state
        self._save_mtimes(current_mtimes)
        self._save_chunks()

        # Persist FAISS index to disk for cross-process cache reuse
        if self._cache_dir:
            self.save(self._cache_dir)

        self._initialized = True
        logger.info(
            "embeddings: indexed %d chunks from %d files (%d changed)",
            len(self._chunks), len(files), len(changed_files),
        )

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """Search the index for chunks semantically similar to the query.

        Returns results sorted by relevance (highest score first).
        """
        if not self._initialized or not self._chunks:
            return []

        import numpy as np

        self._ensure_model()

        # Encode query
        query_embedding = self._model.encode([query], show_progress_bar=False)
        query_embedding = np.array(query_embedding, dtype=np.float32)
        norm = np.linalg.norm(query_embedding)
        if norm > 0:
            query_embedding = query_embedding / norm

        # Search
        if self._index is not None:
            scores, indices = self._index.search(query_embedding, min(top_k, len(self._chunks)))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self._chunks):
                    continue
                chunk = self._chunks[idx]
                results.append(SearchResult(
                    file_path=chunk.file_path,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    snippet=chunk.content[:500],
                    score=float(score),
                ))
        else:
            # Numpy fallback
            scores = np.dot(self._embeddings, query_embedding.T).flatten()
            top_indices = np.argsort(scores)[::-1][:top_k]
            results = []
            for idx in top_indices:
                chunk = self._chunks[idx]
                results.append(SearchResult(
                    file_path=chunk.file_path,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    snippet=chunk.content[:500],
                    score=float(scores[idx]),
                ))

        return results

    def save(self, path: str) -> None:
        """Save the FAISS index to disk."""
        if self._index is not None:
            try:
                import faiss
                faiss.write_index(self._index, os.path.join(path, "faiss.idx"))
            except Exception as e:
                logger.warning("Failed to save FAISS index: %s", e)

    def load(self, path: str) -> bool:
        """Load a previously saved FAISS index."""
        idx_path = os.path.join(path, "faiss.idx")
        if not os.path.exists(idx_path):
            return False

        try:
            import faiss
            self._index = faiss.read_index(idx_path)
            self._chunks = self._load_chunks()
            self._initialized = bool(self._chunks)
            return self._initialized
        except Exception as e:
            logger.warning("Failed to load FAISS index: %s", e)
            return False

    def _chunk_file(
        self,
        rel_path: str,
        content: str,
        chunk_size: int = 30,
        overlap: int = 10,
    ) -> list[CodeChunk]:
        """Split a file into overlapping chunks for embedding."""
        lines = content.split("\n")
        chunks = []

        i = 0
        while i < len(lines):
            end = min(i + chunk_size, len(lines))
            chunk_lines = lines[i:end]
            chunk_content = "\n".join(chunk_lines)

            # Skip nearly empty chunks
            if chunk_content.strip():
                chunks.append(CodeChunk(
                    file_path=rel_path,
                    line_start=i + 1,  # 1-indexed
                    line_end=end,
                    content=f"# {rel_path}:{i+1}-{end}\n{chunk_content}",
                ))

            i += chunk_size - overlap
            if i + overlap >= len(lines):
                break

        return chunks

    def _collect_files(self, repo: Path) -> list[str]:
        """Collect all embeddable files."""
        files = []
        for root, dirs, filenames in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in EMBED_EXTENSIONS:
                    continue
                fp = os.path.join(root, fn)
                try:
                    if os.path.getsize(fp) > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                files.append(fp)
        return sorted(files)

    def _load_mtimes(self) -> dict[str, float]:
        if not self._cache_dir:
            return {}
        try:
            with open(os.path.join(self._cache_dir, "embed_mtimes.json")) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_mtimes(self, mtimes: dict[str, float]) -> None:
        if not self._cache_dir:
            return
        try:
            with open(os.path.join(self._cache_dir, "embed_mtimes.json"), "w") as f:
                json.dump(mtimes, f)
        except OSError:
            pass

    def _load_chunks(self) -> list[CodeChunk]:
        if not self._cache_dir:
            return []
        try:
            with open(os.path.join(self._cache_dir, "chunks.json")) as f:
                data = json.load(f)
                return [CodeChunk(**c) for c in data]
        except (OSError, json.JSONDecodeError):
            return []

    def _save_chunks(self) -> None:
        if not self._cache_dir:
            return
        try:
            data = [
                {
                    "file_path": c.file_path,
                    "line_start": c.line_start,
                    "line_end": c.line_end,
                    "content": c.content,
                }
                for c in self._chunks
            ]
            with open(os.path.join(self._cache_dir, "chunks.json"), "w") as f:
                json.dump(data, f)
        except OSError:
            pass
