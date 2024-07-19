from abc import ABC, abstractmethod
from collections import defaultdict
from enum import Enum, auto
import uuid
from typing import Any, Callable, Dict, List, Optional
import chromadb
from lazyllm import LOG, config
from chromadb.api.models.Collection import Collection

LAZY_ROOT_NAME = "lazyllm_root"
config.add("rag_store", str, "map", "RAG_STORE")  # "map", "chroma"
config.add("rag_persistent_path", str, "./lazyllm_chroma", "RAG_PERSISTENT_PATH")


class MetadataMode(str, Enum):
    ALL = auto()
    EMBED = auto()
    LLM = auto()
    NONE = auto()


class DocNode:
    def __init__(
        self,
        group: str,
        uid: Optional[str] = None,
        text: Optional[str] = None,
        embedding: Optional[List[float]] = None,
        parent: Optional["DocNode"] = None,
    ) -> None:
        self.uid: str = uid if uid else str(uuid.uuid4())
        self.text: Optional[str] = text
        self.group: str = group
        self.embedding: Optional[List[float]] = embedding or None
        self._metadata: Dict[str, Any] = {}
        # Metadata keys that are excluded from text for the embed model.
        self._excluded_embed_metadata_keys: List[str] = []
        # Metadata keys that are excluded from text for the LLM.
        self._excluded_llm_metadata_keys: List[str] = []
        self.parent = parent
        self.children: Dict[str, List["DocNode"]] = defaultdict(list)
        self.is_saved = False

    @property
    def root_node(self) -> Optional["DocNode"]:
        root = self.parent
        while root and root.parent:
            root = root.parent
        return root or self

    @property
    def metadata(self) -> Dict:
        return self.root_node._metadata

    @metadata.setter
    def metadata(self, metadata: Dict) -> None:
        self._metadata = metadata

    @property
    def excluded_embed_metadata_keys(self) -> List:
        return self.root_node._excluded_embed_metadata_keys

    @excluded_embed_metadata_keys.setter
    def excluded_embed_metadata_keys(self, excluded_embed_metadata_keys: List) -> None:
        self._excluded_embed_metadata_keys = excluded_embed_metadata_keys

    @property
    def excluded_llm_metadata_keys(self) -> List:
        return self.root_node._excluded_llm_metadata_keys

    @excluded_llm_metadata_keys.setter
    def excluded_llm_metadata_keys(self, excluded_llm_metadata_keys: List) -> None:
        self._excluded_llm_metadata_keys = excluded_llm_metadata_keys

    def get_children_str(self) -> str:
        return str(
            {key: [node.uid for node in nodes] for key, nodes in self.children.items()}
        )

    def __str__(self) -> str:
        return (
            f"DocNode(id: {self.uid}, group: {self.group}, text: {self.get_content()}) parent: "
            f"{self.parent.uid if self.parent else None}, children: {self.get_children_str()} "
            f"is_embed: {self.has_embedding()}"
        )

    def __repr__(self) -> str:
        return str(self)

    def has_embedding(self) -> bool:
        return self.embedding and self.embedding[0] != -1   # placeholder

    def do_embedding(self, embed: Callable) -> None:
        self.embedding = embed(self.text)
        self.is_saved = False

    def get_content(self, metadata_mode: MetadataMode = MetadataMode.NONE) -> str:
        metadata_str = self.get_metadata_str(mode=metadata_mode).strip()
        if not metadata_str:
            return self.text if self.text else ""
        return f"{metadata_str}\n\n{self.text}".strip()

    def get_metadata_str(self, mode: MetadataMode = MetadataMode.ALL) -> str:
        """Metadata info string."""
        if mode == MetadataMode.NONE:
            return ""

        metadata_keys = set(self.metadata.keys())
        if mode == MetadataMode.LLM:
            for key in self.excluded_llm_metadata_keys:
                if key in metadata_keys:
                    metadata_keys.remove(key)
        elif mode == MetadataMode.EMBED:
            for key in self.excluded_embed_metadata_keys:
                if key in metadata_keys:
                    metadata_keys.remove(key)

        return "\n".join([f"{key}: {self.metadata[key]}" for key in metadata_keys])

    def get_text(self) -> str:
        return self.get_content(metadata_mode=MetadataMode.NONE)


class BaseStore(ABC):
    def __init__(self, node_groups: List[str]) -> None:
        self._store: Dict[str, Dict[str, DocNode]] = {
            group: {} for group in node_groups
        }
        
    def _add_nodes(self, group: str, nodes: List[DocNode]) -> None:
        if group not in self._store:
            self._store[group] = {}
        for node in nodes:
            self._store[group][node.uid] = node

    def add_nodes(self, group: str, nodes: List[DocNode]) -> None:
        self._add_nodes(group, nodes)
        self.save_nodes(group, nodes)

    def has_nodes(self, group: str) -> bool:
        return len(self._store[group]) > 0

    def get_node(self, group: str, node_id: str) -> Optional[DocNode]:
        return self._store.get(group, {}).get(node_id)

    def traverse_nodes(self, group: str) -> List[DocNode]:
        return list(self._store.get(group, {}).values())

    @abstractmethod
    def save_nodes(self, group: str, nodes: List[DocNode]) -> None:
        raise NotImplementedError("Not implemented yet.")

    @abstractmethod
    def try_load_store(self) -> None:
        raise NotImplementedError("Not implemented yet.")


class MapStore(BaseStore):
    def __init__(self, node_groups: List[str], *args, **kwargs):
        super().__init__(node_groups, *args, **kwargs)

    def save_nodes(self, group: str, nodes: List[DocNode]) -> None:
        pass

    def try_load_store(self) -> None:
        pass


class ChromadbStore(BaseStore):
    def __init__(
        self, node_groups: List[str], embed: Callable, *args, **kwargs
    ) -> None:
        super().__init__(node_groups, *args, **kwargs)
        self._db_client = chromadb.PersistentClient(path=config["rag_persistent_path"])
        LOG.success(f"Initialzed chromadb in path: {config['rag_persistent_path']}")
        self._collections: Dict[str, Collection] = {
            group: self._db_client.get_or_create_collection(group)
            for group in node_groups
        }
        self.embed = embed
        self.placeholder_length = len(embed("a"))
        self.try_load_store()

    def try_load_store(self) -> None:
        if not self._collections[LAZY_ROOT_NAME].peek(1)["ids"]:
            LOG.info("No persistent data found, skip the rebuilding phrase.")
            return

        # Restore all nodes
        for group in self._collections.keys():
            results = self._peek_all_documents(group)
            nodes = self._build_nodes_from_chroma(results)
            self.add_nodes(group, nodes)

        # Rebuild relationships
        for group, nodes_dict in self._store.items():
            for node in nodes_dict.values():
                if node.parent:
                    parent_uid = node.parent
                    parent_node = self._find_node_by_uid(parent_uid)
                    node.parent = parent_node
                    parent_node.children[node.group].append(node)
            LOG.debug(f"build {group} nodes from chromadb: {nodes_dict.values()}")
        LOG.success("Successfully Built nodes from chromadb.")

    def save_nodes(self, group: str, nodes: List[DocNode]) -> None:
        ids, embeddings, metadatas, documents = [], [], [], []
        collection = self._collections.get(group)
        assert collection, f"Group {group} is not found in collections {self._collections}"
        for node in nodes:
            if node.is_saved:
                continue
            if not node.has_embedding():
                node.embedding = [-1] * self.placeholder_length
            ids.append(node.uid)
            embeddings.append(node.embedding)
            metadatas.append(self._make_chroma_metadata(node))
            documents.append(node.get_content(metadata_mode=MetadataMode.NONE))
            node.is_saved = True
        if ids:
            collection.upsert(
                embeddings=embeddings,
                ids=ids,
                metadatas=metadatas,
                documents=documents,
            )
            LOG.debug(f"Saved {group} nodes {ids} to chromadb.")

    def _find_node_by_uid(self, uid: str) -> Optional[DocNode]:
        for nodes_by_category in self._store.values():
            if uid in nodes_by_category:
                return nodes_by_category[uid]
        raise ValueError(f"UID {uid} not found in store.")

    def _build_nodes_from_chroma(self, results: Dict[str, List]) -> List[DocNode]:
        nodes: List[DocNode] = []
        for i, uid in enumerate(results["ids"]):
            chroma_metadata = results["metadatas"][i]
            node = DocNode(
                uid=uid,
                text=results["documents"][i],
                group=chroma_metadata["group"],
                embedding=results["embeddings"][i],
                parent=chroma_metadata["parent"],
            )
            node.is_saved = True
            nodes.append(node)
        return nodes

    def _make_chroma_metadata(self, node: DocNode) -> Dict[str, Any]:
        metadata = {
            "group": node.group,
            "parent": node.parent.uid if node.parent else "",
        }
        return metadata

    def _peek_all_documents(self, group: str) -> Dict[str, List]:
        assert group in self._collections, f"group {group} not found."
        collection = self._collections[group]
        return collection.peek(collection.count())
