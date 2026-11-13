"""BM25 关键词稀疏表示（RAG 模块，成员 C，issue #25）。

M2 混合检索的"关键词"那一路由本模块产出：把文本编码成 (词项索引, 权重) 的稀疏向量，
交给 Qdrant 做点积。**不引入分词库**——中文按字符 bigram 切分，英文数字按词切分，
两者相加即可覆盖"字面匹配"这个用途；引入 jieba 之类的依赖会把评测结论绑在分词器
版本上，收益却未必值得。

权重按标准 BM25 公式计算：

    tf' = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
    w   = tf' * idf(term)

IDF 需要语料统计，而 Qdrant 只做点积、不参与打分，因此**必须在写入时就把 idf 乘进
权重**。这带来一个必须写清楚的限制：IDF 来自哪份语料，决定了稀疏通道的语义。M2 的
做法是用评测语料统计一份固定 IDF 表并随结果留档，保证可复现；生产侧按用户语料统计
是后续工作，不在本任务范围。词项索引由词项的哈希派生，因此不需要维护词表；未在
IDF 表中出现的词项退化为固定权重，不丢词。
"""

import hashlib
import math
import re
from dataclasses import dataclass, field

# 词项哈希的位宽：Qdrant 稀疏向量索引是无符号 32 位整数。
INDEX_BITS = 32
INDEX_MASK = (1 << INDEX_BITS) - 1

# 英文/数字词：允许内部出现 . _ - ，这样 8.0.36、e-mail 这类 token 不会被拆碎。
ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
# 连续汉字：按 bigram 切分；单字成词时保留单字。
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# BM25 的常用缺省参数；改动会影响三档对比的可比性，故随版本号留档。
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


@dataclass(frozen=True)
class SparseVector:
    """Qdrant 稀疏向量的内存表示：并行排列的词项索引与权重。"""

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self):
        if len(self.indices) != len(self.values):
            raise ValueError("稀疏向量的索引与权重长度必须一致")
        if len(set(self.indices)) != len(self.indices):
            raise ValueError("稀疏向量的索引必须唯一")

    def __len__(self) -> int:
        return len(self.indices)

    def as_qdrant(self) -> dict:
        """转成 Qdrant REST 的稀疏向量结构。"""
        return {"indices": list(self.indices), "values": list(self.values)}


def tokenize(text: str) -> list[str]:
    """切分为 BM25 词项：小写英文数字词 + 汉字 bigram（单字保留单字）。"""
    lowered = text.lower()
    tokens = [match.group() for match in ASCII_TOKEN.finditer(lowered)]
    for match in CJK_RUN.finditer(lowered):
        run = match.group()
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def term_index(term: str) -> int:
    """由词项派生稳定的 32 位索引，避免维护词表。"""
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") & INDEX_MASK


def build_idf(documents: list[list[str]]) -> dict[str, float]:
    """按 BM25 的 idf 公式统计语料，返回 term -> idf。

    公式为 ln(1 + (N - df + 0.5) / (df + 0.5))，恒为正，避免高频词出现负权重。
    """
    total = len(documents)
    if total == 0:
        return {}
    df: dict[str, int] = {}
    for tokens in documents:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    return {term: math.log(1 + (total - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}


@dataclass(frozen=True)
class Bm25Encoder:
    """把文本编码成 BM25 权重稀疏向量。idf 与平均文档长度需事先统计。"""

    idf: dict[str, float] = field(default_factory=dict)
    avg_doc_len: float = 1.0
    k1: float = DEFAULT_K1
    b: float = DEFAULT_B
    # 未在 idf 表中出现的词项使用该权重；取语料中"只出现一次"的量级，既不丢词也不放大。
    default_idf: float = 1.0

    @classmethod
    def fit(
        cls, texts: list[str], *, k1: float = DEFAULT_K1, b: float = DEFAULT_B
    ) -> "Bm25Encoder":
        """按一批文档统计 idf 与平均长度，构造可直接使用的编码器。"""
        tokenized = [tokenize(text) for text in texts]
        lengths = [len(tokens) for tokens in tokenized]
        avg = sum(lengths) / len(lengths) if lengths else 1.0
        return cls(idf=build_idf(tokenized), avg_doc_len=avg or 1.0, k1=k1, b=b)

    def _weights(self, tokens: list[str], *, length_norm: bool) -> dict[int, float]:
        if not tokens:
            return {}
        counts: dict[str, int] = {}
        for term in tokens:
            counts[term] = counts.get(term, 0) + 1
        doc_len = len(tokens) if length_norm else self.avg_doc_len
        norm = 1 - self.b + self.b * doc_len / (self.avg_doc_len or 1.0)
        weights: dict[int, float] = {}
        for term, tf in counts.items():
            saturated = tf * (self.k1 + 1) / (tf + self.k1 * norm)
            weight = saturated * self.idf.get(term, self.default_idf)
            index = term_index(term)
            # 哈希碰撞时累加，保持权重总量不因碰撞而丢失。
            weights[index] = weights.get(index, 0.0) + weight
        return weights

    def encode_document(self, text: str) -> SparseVector:
        """文档侧编码：包含长度归一化。"""
        return self._to_sparse(self._weights(tokenize(text), length_norm=True))

    def encode_query(self, text: str) -> SparseVector:
        """查询侧编码：不做长度归一化——查询长度与文档长度无关，归一化会惩罚长问题。"""
        return self._to_sparse(self._weights(tokenize(text), length_norm=False))

    @staticmethod
    def _to_sparse(weights: dict[int, float]) -> SparseVector:
        items = sorted(weights.items())
        return SparseVector(
            indices=tuple(index for index, _ in items),
            values=tuple(value for _, value in items),
        )
