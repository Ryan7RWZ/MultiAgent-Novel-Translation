"""向量检索存储（RAG 底座）：FAISS 优先，缺失时降级为 numpy 余弦相似度。

设计说明：
- numpy 属于基础依赖，顶层导入；**faiss 为可选依赖，延迟导入** ——
  仅在真正构建索引时尝试 ``import faiss``，未安装则自动降级为
  numpy 暴力余弦相似度，并打印一次安装提示（``pip install faiss-cpu``）。
- embedding 函数是显式 TODO 接口：构造时通过 ``embed_fn`` 注入真实的
  向量模型（如 OpenAI text-embedding / 本地 sentence-transformers）；
  未注入时使用确定性的哈希词袋占位实现，保证骨架阶段离线可跑。
- 持久化：``save`` 写出向量矩阵 (vectors.npy) + 文档元数据 (docs.json)；
  ``load`` 读回后按需重建索引。向量维数由首次 add 时的 embed 结果决定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np  # numpy 为基础依赖，允许顶层导入

__all__ = ["VectorStore", "VectorHit"]

# embedding 函数签名约定：输入文本列表，输出 (n, dim) 的 float32 矩阵
EmbedFn = Callable[[list[str]], "np.ndarray"]

# faiss 安装提示（仅在降级时打印一次）
_FAISS_HINT = (
    "[mant.memory.vectorstore] 未检测到 faiss，已降级为 numpy 余弦相似度检索；"
    "如需大规模向量检索请安装：pip install faiss-cpu"
)


@dataclass
class VectorHit:
    """向量检索命中结果（本模块内部轻量模型，非跨模块共享契约）。

    属性:
        text: 命中文本的原文。
        metadata: 写入时附带的元数据（如 {"work_id", "chapter_id", "segment_id"}）。
        score: 余弦相似度得分（-1~1，越高越相似）。
    """

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


class VectorStore:
    """向量存储：文本向量的写入、检索与持久化（骨架实现）。

    参数:
        dim: 向量维数；为 None 时在首次 add 时根据 embed 结果自动确定。
        index_dir: 默认持久化目录（save/load 未显式传 path 时使用）。
        embed_fn: 真实 embedding 模型（TODO 注入点）；为 None 时使用
            确定性哈希词袋占位实现。
    """

    def __init__(
        self,
        dim: int | None = None,
        index_dir: str | Path | None = None,
        embed_fn: Optional[EmbedFn] = None,
    ) -> None:
        self.dim = dim
        self.index_dir = Path(index_dir) if index_dir else None
        self.embed_fn = embed_fn
        # 已存储向量矩阵 (n, dim) 与对应文档；骨架阶段全量驻留内存
        self._vectors: np.ndarray | None = None
        self._docs: list[dict[str, Any]] = []
        self._faiss_index: Any = None  # faiss 索引对象（可用时重建）
        self._faiss_checked = False
        self._faiss_module: Any = None

    # ------------------------------------------------------------------
    # faiss 延迟导入与降级
    # ------------------------------------------------------------------
    def _get_faiss(self) -> Any:
        """延迟导入 faiss；失败返回 None 并打印一次安装提示。"""
        if not self._faiss_checked:
            self._faiss_checked = True
            try:
                import faiss  # noqa: F401  # 可选依赖，仅此处导入

                self._faiss_module = faiss
            except ImportError:
                self._faiss_module = None
                print(_FAISS_HINT)
        return self._faiss_module

    # ------------------------------------------------------------------
    # embedding
    # ------------------------------------------------------------------
    def _embed(self, texts: list[str]) -> np.ndarray:
        """对文本列表做向量化。

        TODO(接入真实模型)：通过构造函数 embed_fn 注入真实 embedding 模型；
        默认占位实现为确定性哈希词袋（见 _hash_embed），仅用于流程打通。
        """
        if self.embed_fn is not None:
            vecs = np.asarray(self.embed_fn(texts), dtype=np.float32)
        else:
            vecs = self._hash_embed(texts)
        if vecs.ndim != 2 or vecs.shape[0] != len(texts):
            raise ValueError("embed_fn 返回形状应为 (len(texts), dim)")
        if self.dim is None:
            self.dim = int(vecs.shape[1])
        if vecs.shape[1] != self.dim:
            raise ValueError(f"向量维数 {vecs.shape[1]} 与存储维数 {self.dim} 不一致")
        return vecs

    def _hash_embed(self, texts: list[str]) -> np.ndarray:
        """占位 embedding：字符 bigram 哈希词袋（确定性、仅 stdlib+numpy）。

        注意：该实现无语义，仅保证骨架可运行，严禁用于实验对照数据。
        """
        dim = self.dim or 384
        vecs = np.zeros((len(texts), dim), dtype=np.float32)
        for i, text in enumerate(texts):
            lowered = text.lower()
            tokens = [lowered[j : j + 2] for j in range(max(len(lowered) - 1, 1))]
            for token in tokens:
                # 用 md5 保证跨进程确定性（内置 hash() 对 str 有随机盐）
                bucket = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % dim
                vecs[i, bucket] += 1.0
        return vecs

    @staticmethod
    def _normalize(vecs: np.ndarray) -> np.ndarray:
        """按行做 L2 归一化（余弦相似度前置步骤；零向量保持为零）。"""
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        return vecs / norms

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add(self, texts: list[str], metadatas: list[dict[str, Any]] | None = None) -> list[int]:
        """写入一批文本及其元数据，返回分配的整数 id 列表。

        TODO(大规模优化)：当前每次 add 后重建 faiss 索引；语料增长后
        改为增量 add 并评估 IndexIVFFlat 等近似索引。
        """
        if not texts:
            return []
        if metadatas is not None and len(metadatas) != len(texts):
            raise ValueError("metadatas 长度必须与 texts 一致")
        metadatas = metadatas or [{} for _ in texts]

        vecs = self._normalize(self._embed(texts))
        start_id = len(self._docs)
        self._vectors = (
            vecs if self._vectors is None else np.vstack([self._vectors, vecs])
        )
        self._docs.extend(
            {"text": t, "metadata": m} for t, m in zip(texts, metadatas)
        )
        self._rebuild_index()
        return list(range(start_id, start_id + len(texts)))

    def search(self, query: str, k: int = 5) -> list[VectorHit]:
        """检索与 query 最相似的 k 条文本（按余弦相似度降序）。"""
        if self._vectors is None or len(self._docs) == 0:
            return []
        k = max(1, min(k, len(self._docs)))
        q = self._normalize(self._embed([query]))  # (1, dim)

        faiss = self._get_faiss()
        if faiss is not None and self._faiss_index is not None:
            # faiss 路径：IndexFlatIP + 归一化向量 == 余弦相似度
            scores, idxs = self._faiss_index.search(q, k)
            pairs = zip(idxs[0].tolist(), scores[0].tolist())
        else:
            # numpy 降级路径：暴力余弦相似度取 top-k
            sims = (self._vectors @ q[0]).astype(np.float64)
            top = np.argsort(-sims)[:k]
            pairs = ((int(i), float(sims[i])) for i in top)

        hits = []
        for idx, score in pairs:
            if idx < 0:  # faiss 结果不足 k 时以 -1 填充
                continue
            doc = self._docs[idx]
            hits.append(
                VectorHit(text=doc["text"], metadata=doc["metadata"], score=float(score))
            )
        return hits

    def save(self, path: str | Path | None = None) -> Path:
        """持久化到目录：vectors.npy（向量矩阵）+ docs.json（文本与元数据）。

        TODO(大规模优化)：faiss 可用时可另存 faiss.write_index 二进制索引，
        避免 load 时全量重建。
        """
        target = Path(path) if path else self.index_dir
        if target is None:
            raise ValueError("未指定持久化目录（构造时传 index_dir 或调用时传 path）")
        target.mkdir(parents=True, exist_ok=True)
        if self._vectors is not None:
            np.save(target / "vectors.npy", self._vectors)
        (target / "docs.json").write_text(
            json.dumps(self._docs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path, embed_fn: Optional[EmbedFn] = None) -> "VectorStore":
        """从 save 写出的目录恢复 VectorStore。

        注意：加载方需保证 embed_fn（或占位实现）的向量维数与已存向量一致。
        """
        target = Path(path)
        store = cls(index_dir=target, embed_fn=embed_fn)
        vec_file = target / "vectors.npy"
        docs_file = target / "docs.json"
        if vec_file.exists():
            store._vectors = np.load(vec_file)
            store.dim = int(store._vectors.shape[1])
        if docs_file.exists():
            store._docs = json.loads(docs_file.read_text(encoding="utf-8"))
        if store._vectors is not None and len(store._docs) != len(store._vectors):
            raise ValueError("vectors.npy 与 docs.json 数量不一致，持久化数据损坏")
        store._rebuild_index()
        return store

    # ------------------------------------------------------------------
    # 内部：索引维护
    # ------------------------------------------------------------------
    def _rebuild_index(self) -> None:
        """faiss 可用时基于当前向量重建 IndexFlatIP；不可用则保持 numpy 路径。"""
        faiss = self._get_faiss()
        if faiss is None or self._vectors is None or self.dim is None:
            self._faiss_index = None
            return
        index = faiss.IndexFlatIP(self.dim)
        index.add(self._vectors)
        self._faiss_index = index

    def __len__(self) -> int:
        """已存储的文档数。"""
        return len(self._docs)
