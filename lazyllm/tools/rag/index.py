from typing import List
from .store import DocNode
import numpy as np


class DefaultIndex:
    """Default Index, registered for similarity functions"""

    registered_similarity = dict()

    def __init__(self, embed, **kwargs):
        self.embed = embed

    @classmethod
    def register_similarity(cls, func=None, mode=None, descend=True):
        def decorator(f):
            cls.registered_similarity[f.__name__] = (f, mode, descend)
            return f

        return decorator(func) if func else decorator

    def query(
        self,
        query: str,
        nodes: List[DocNode],
        similarity_name: str,
        topk: int,
        **kwargs,
    ) -> List[DocNode]:
        similarity_func, mode, descend = self.registered_similarity[similarity_name]

        if mode == "embedding":
            assert self.embed, "Chosen similarity needs embed model."
            assert len(query) > 0, "Query should not be empty."
            query_embedding = self.embed(query)
            for node in nodes:
                if not node.has_embedding():
                    node.do_embedding(self.embed)
            similarities = [
                (node, similarity_func(query_embedding, node.embedding, **kwargs))
                for node in nodes
            ]
        elif mode == "text":
            similarities = [
                (node, similarity_func(query, node, **kwargs)) for node in nodes
            ]
        else:
            raise NotImplementedError(f"Mode {mode} is not supported.")

        similarities.sort(key=lambda x: x[1], reverse=descend)
        if topk is not None:
            similarities = similarities[:topk]
        return [node for node, _ in similarities]


@DefaultIndex.register_similarity(mode="text", descend=True)
def dummy(query, node, **kwargs):
    return len(node.text)


@DefaultIndex.register_similarity(mode="embedding", descend=True)
def cosine(embedding1, embedding2):
    product = np.dot(embedding1, embedding2)
    norm = np.linalg.norm(embedding1) * np.linalg.norm(embedding2)
    return product / norm
