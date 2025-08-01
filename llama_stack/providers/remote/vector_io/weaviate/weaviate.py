# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.
import json
import logging
from typing import Any

import weaviate
import weaviate.classes as wvc
from numpy.typing import NDArray
from weaviate.classes.init import Auth
from weaviate.classes.query import Filter

from llama_stack.apis.common.content_types import InterleavedContent
from llama_stack.apis.common.errors import VectorStoreNotFoundError
from llama_stack.apis.files.files import Files
from llama_stack.apis.vector_dbs import VectorDB
from llama_stack.apis.vector_io import Chunk, QueryChunksResponse, VectorIO
from llama_stack.core.request_headers import NeedsRequestProviderData
from llama_stack.providers.datatypes import Api, VectorDBsProtocolPrivate
from llama_stack.providers.utils.kvstore import kvstore_impl
from llama_stack.providers.utils.kvstore.api import KVStore
from llama_stack.providers.utils.memory.openai_vector_store_mixin import (
    OpenAIVectorStoreMixin,
)
from llama_stack.providers.utils.memory.vector_store import (
    EmbeddingIndex,
    VectorDBWithIndex,
)
from llama_stack.providers.utils.vector_io.vector_utils import sanitize_collection_name

from .config import WeaviateVectorIOConfig

log = logging.getLogger(__name__)

VERSION = "v3"
VECTOR_DBS_PREFIX = f"vector_dbs:weaviate:{VERSION}::"
VECTOR_INDEX_PREFIX = f"vector_index:weaviate:{VERSION}::"
OPENAI_VECTOR_STORES_PREFIX = f"openai_vector_stores:weaviate:{VERSION}::"
OPENAI_VECTOR_STORES_FILES_PREFIX = f"openai_vector_stores_files:weaviate:{VERSION}::"
OPENAI_VECTOR_STORES_FILES_CONTENTS_PREFIX = f"openai_vector_stores_files_contents:weaviate:{VERSION}::"


class WeaviateIndex(EmbeddingIndex):
    def __init__(
        self,
        client: weaviate.Client,
        collection_name: str,
        kvstore: KVStore | None = None,
    ):
        self.client = client
        self.collection_name = sanitize_collection_name(collection_name, weaviate_format=True)
        self.kvstore = kvstore

    async def initialize(self):
        pass

    async def add_chunks(self, chunks: list[Chunk], embeddings: NDArray):
        assert len(chunks) == len(embeddings), (
            f"Chunk length {len(chunks)} does not match embedding length {len(embeddings)}"
        )

        data_objects = []
        for i, chunk in enumerate(chunks):
            data_objects.append(
                wvc.data.DataObject(
                    properties={
                        "chunk_content": chunk.model_dump_json(),
                    },
                    vector=embeddings[i].tolist(),
                )
            )

        # Inserting chunks into a prespecified Weaviate collection
        collection = self.client.collections.get(self.collection_name)

        # TODO: make this async friendly
        collection.data.insert_many(data_objects)

    async def delete_chunk(self, chunk_id: str) -> None:
        sanitized_collection_name = sanitize_collection_name(self.collection_name, weaviate_format=True)
        collection = self.client.collections.get(sanitized_collection_name)
        collection.data.delete_many(where=Filter.by_property("id").contains_any([chunk_id]))

    async def query_vector(self, embedding: NDArray, k: int, score_threshold: float) -> QueryChunksResponse:
        sanitized_collection_name = sanitize_collection_name(self.collection_name, weaviate_format=True)
        collection = self.client.collections.get(sanitized_collection_name)

        results = collection.query.near_vector(
            near_vector=embedding.tolist(),
            limit=k,
            return_metadata=wvc.query.MetadataQuery(distance=True),
        )

        chunks = []
        scores = []
        for doc in results.objects:
            chunk_json = doc.properties["chunk_content"]
            try:
                chunk_dict = json.loads(chunk_json)
                chunk = Chunk(**chunk_dict)
            except Exception:
                log.exception(f"Failed to parse document: {chunk_json}")
                continue

            chunks.append(chunk)
            scores.append(1.0 / doc.metadata.distance if doc.metadata.distance != 0 else float("inf"))

        return QueryChunksResponse(chunks=chunks, scores=scores)

    async def delete(self, chunk_ids: list[str] | None = None) -> None:
        """
        Delete chunks by IDs if provided, otherwise drop the entire collection.
        """
        sanitized_collection_name = sanitize_collection_name(self.collection_name, weaviate_format=True)
        if chunk_ids is None:
            # Drop entire collection if it exists
            if self.client.collections.exists(sanitized_collection_name):
                self.client.collections.delete(sanitized_collection_name)
            return
        collection = self.client.collections.get(sanitized_collection_name)
        collection.data.delete_many(where=Filter.by_property("id").contains_any(chunk_ids))

    async def query_keyword(
        self,
        query_string: str,
        k: int,
        score_threshold: float,
    ) -> QueryChunksResponse:
        raise NotImplementedError("Keyword search is not supported in Weaviate")

    async def query_hybrid(
        self,
        embedding: NDArray,
        query_string: str,
        k: int,
        score_threshold: float,
        reranker_type: str,
        reranker_params: dict[str, Any] | None = None,
    ) -> QueryChunksResponse:
        raise NotImplementedError("Hybrid search is not supported in Weaviate")


class WeaviateVectorIOAdapter(
    OpenAIVectorStoreMixin,
    VectorIO,
    NeedsRequestProviderData,
    VectorDBsProtocolPrivate,
):
    def __init__(
        self,
        config: WeaviateVectorIOConfig,
        inference_api: Api.inference,
        files_api: Files | None,
    ) -> None:
        self.config = config
        self.inference_api = inference_api
        self.client_cache = {}
        self.cache = {}
        self.files_api = files_api
        self.kvstore: KVStore | None = None
        self.vector_db_store = None
        self.openai_vector_stores: dict[str, dict[str, Any]] = {}
        self.metadata_collection_name = "openai_vector_stores_metadata"

    def _get_client(self) -> weaviate.Client:
        if "localhost" in self.config.weaviate_cluster_url:
            log.info("using Weaviate locally in container")
            host, port = self.config.weaviate_cluster_url.split(":")
            key = "local_test"
            client = weaviate.connect_to_local(
                host=host,
                port=port,
            )
        else:
            log.info("Using Weaviate remote cluster with URL")
            key = f"{self.config.weaviate_cluster_url}::{self.config.weaviate_api_key}"
            if key in self.client_cache:
                return self.client_cache[key]
            client = weaviate.connect_to_weaviate_cloud(
                cluster_url=self.config.weaviate_cluster_url,
                auth_credentials=Auth.api_key(self.config.weaviate_api_key),
            )
        self.client_cache[key] = client
        return client

    async def initialize(self) -> None:
        """Set up KV store and load existing vector DBs and OpenAI vector stores."""
        # Initialize KV store for metadata if configured
        if self.config.kvstore is not None:
            self.kvstore = await kvstore_impl(self.config.kvstore)
        else:
            self.kvstore = None
            log.info("No kvstore configured, registry will not persist across restarts")

        # Load existing vector DB definitions
        if self.kvstore is not None:
            start_key = VECTOR_DBS_PREFIX
            end_key = f"{VECTOR_DBS_PREFIX}\xff"
            stored = await self.kvstore.values_in_range(start_key, end_key)
            for raw in stored:
                vector_db = VectorDB.model_validate_json(raw)
                client = self._get_client()
                idx = WeaviateIndex(
                    client=client,
                    collection_name=vector_db.identifier,
                    kvstore=self.kvstore,
                )
                self.cache[vector_db.identifier] = VectorDBWithIndex(
                    vector_db=vector_db,
                    index=idx,
                    inference_api=self.inference_api,
                )

            # Load OpenAI vector stores metadata into cache
            await self.initialize_openai_vector_stores()

    async def shutdown(self) -> None:
        for client in self.client_cache.values():
            client.close()

    async def register_vector_db(
        self,
        vector_db: VectorDB,
    ) -> None:
        client = self._get_client()
        sanitized_collection_name = sanitize_collection_name(vector_db.identifier, weaviate_format=True)
        # Create collection if it doesn't exist
        if not client.collections.exists(sanitized_collection_name):
            client.collections.create(
                name=sanitized_collection_name,
                vectorizer_config=wvc.config.Configure.Vectorizer.none(),
                properties=[
                    wvc.config.Property(
                        name="chunk_content",
                        data_type=wvc.config.DataType.TEXT,
                    ),
                ],
            )

        self.cache[sanitized_collection_name] = VectorDBWithIndex(
            vector_db,
            WeaviateIndex(client=client, collection_name=sanitized_collection_name),
            self.inference_api,
        )

    async def unregister_vector_db(self, vector_db_id: str) -> None:
        client = self._get_client()
        sanitized_collection_name = sanitize_collection_name(vector_db_id, weaviate_format=True)
        if sanitized_collection_name not in self.cache or client.collections.exists(sanitized_collection_name) is False:
            log.warning(f"Vector DB {sanitized_collection_name} not found")
            return
        client.collections.delete(sanitized_collection_name)
        await self.cache[sanitized_collection_name].index.delete()
        del self.cache[sanitized_collection_name]

    async def _get_and_cache_vector_db_index(self, vector_db_id: str) -> VectorDBWithIndex | None:
        sanitized_collection_name = sanitize_collection_name(vector_db_id, weaviate_format=True)
        if sanitized_collection_name in self.cache:
            return self.cache[sanitized_collection_name]

        vector_db = await self.vector_db_store.get_vector_db(sanitized_collection_name)
        if not vector_db:
            raise VectorStoreNotFoundError(vector_db_id)

        client = self._get_client()
        if not client.collections.exists(vector_db.identifier):
            raise ValueError(f"Collection with name `{sanitized_collection_name}` not found")

        index = VectorDBWithIndex(
            vector_db=vector_db,
            index=WeaviateIndex(client=client, collection_name=sanitized_collection_name),
            inference_api=self.inference_api,
        )
        self.cache[sanitized_collection_name] = index
        return index

    async def insert_chunks(
        self,
        vector_db_id: str,
        chunks: list[Chunk],
        ttl_seconds: int | None = None,
    ) -> None:
        sanitized_collection_name = sanitize_collection_name(vector_db_id, weaviate_format=True)
        index = await self._get_and_cache_vector_db_index(sanitized_collection_name)
        if not index:
            raise VectorStoreNotFoundError(vector_db_id)

        await index.insert_chunks(chunks)

    async def query_chunks(
        self,
        vector_db_id: str,
        query: InterleavedContent,
        params: dict[str, Any] | None = None,
    ) -> QueryChunksResponse:
        sanitized_collection_name = sanitize_collection_name(vector_db_id, weaviate_format=True)
        index = await self._get_and_cache_vector_db_index(sanitized_collection_name)
        if not index:
            raise VectorStoreNotFoundError(vector_db_id)

        return await index.query_chunks(query, params)

    async def delete_chunks(self, store_id: str, chunk_ids: list[str]) -> None:
        sanitized_collection_name = sanitize_collection_name(store_id, weaviate_format=True)
        index = await self._get_and_cache_vector_db_index(sanitized_collection_name)
        if not index:
            raise ValueError(f"Vector DB {sanitized_collection_name} not found")

        await index.delete(chunk_ids)
