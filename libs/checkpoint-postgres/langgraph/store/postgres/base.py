import asyncio
import json
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
    cast,
)

import orjson
from langchain_core.embeddings import Embeddings
from psycopg import Capabilities, Connection, Cursor, Pipeline
from psycopg.errors import UndefinedTable
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from typing_extensions import TypedDict

from langgraph.checkpoint.postgres import _ainternal as _ainternal
from langgraph.checkpoint.postgres import _internal as _pg_internal
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

logger = logging.getLogger(__name__)


class EmbeddingConfig(TypedDict, total=False):
    """Configuration for vector embeddings in PostgreSQL store."""

    dims: int
    """Number of dimensions in the embedding vectors.
    
    Common embedding models have the following dimensions:
        - OpenAI text-embedding-3-large: 256, 1024, or 3072
        - OpenAI text-embedding-3-small: 512 or 1536
        - OpenAI text-embedding-ada-002: 1536
        - Cohere embed-english-v3.0: 1024
        - Cohere embed-english-light-v3.0: 384
        - Cohere embed-multilingual-v3.0: 1024
        - Cohere embed-multilingual-light-v3.0: 384
    """

    embed: Embeddings
    """Optional function to generate embeddings from text."""

    text_fields: Optional[list[str]]
    """Fields to extract text from for embedding generation.
    
    Defaults to ["__root__"], which embeds the json object as a whole.
    """


@dataclass
class Migration:
    """A database migration with optional conditions and parameters."""

    sql: str
    condition: Optional[Callable[[Any], bool]] = None
    acondition: Optional[Callable[[Any], Awaitable[bool]]] = None
    params: Optional[dict[str, Any]] = None


def check_vector_available(store: Any) -> bool:
    """Check if vector operations are available in the database."""
    if store.embedding_config is None:
        # Need the dims to initialize the table
        return False
    try:
        with store._cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
            """
            )
            result = bool(cur.fetchone())
            if not result:
                logger.warning("Vector extension is not available in the database.")
            return result
    except Exception as e:
        logger.warning(f"Failed to check vector extension availability: {e}")
        return False


async def acheck_vector_available(store: Any) -> bool:
    if store.embedding_config is None:
        # Need the dims to initialize the table
        return False
    try:
        async with store._cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
            """
            )
            result = bool(await cur.fetchone())
            if not result:
                logger.warning("Vector extension is not available in the database.")
            return result
    except Exception as e:
        logger.warning(f"Failed to check vector extension availability: {e}")
        return False


MIGRATIONS: Sequence[Union[str, Migration]] = [
    """
CREATE TABLE IF NOT EXISTS store (
    -- 'prefix' represents the doc's 'namespace'
    prefix text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prefix, key)
);
""",
    """
-- For faster lookups by prefix
CREATE INDEX IF NOT EXISTS store_prefix_idx ON store USING btree (prefix text_pattern_ops);
""",
    Migration(
        """
CREATE EXTENSION IF NOT EXISTS vector;
""",
        condition=check_vector_available,
        acondition=acheck_vector_available,
    ),
    Migration(
        """
CREATE TABLE IF NOT EXISTS store_vectors (
    prefix text NOT NULL,
    key text NOT NULL,
    field_name text NOT NULL,
    embedding vector(%(dims)s),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prefix, key, field_name),
    FOREIGN KEY (prefix, key) REFERENCES store(prefix, key) ON DELETE CASCADE
);
""",
        condition=check_vector_available,
        acondition=acheck_vector_available,
        params={"dims": lambda store: store.embedding_config["dims"]},
    ),
    Migration(
        """
CREATE INDEX IF NOT EXISTS store_vectors_embedding_idx ON store_vectors 
    USING ivfflat (embedding vector_cosine_ops);
""",
        condition=check_vector_available,
        acondition=acheck_vector_available,
    ),
]

C = TypeVar("C", bound=Union[_pg_internal.Conn, _ainternal.Conn])


class BasePostgresStore(Generic[C]):
    MIGRATIONS = MIGRATIONS
    conn: C
    _deserializer: Optional[Callable[[Union[bytes, orjson.Fragment]], dict[str, Any]]]
    embedding_config: Optional[EmbeddingConfig]

    def _get_batch_GET_ops_queries(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
    ) -> list[tuple[str, tuple, tuple[str, ...], list]]:
        namespace_groups = defaultdict(list)
        for idx, op in get_ops:
            namespace_groups[op.namespace].append((idx, op.key))
        results = []
        for namespace, items in namespace_groups.items():
            _, keys = zip(*items)
            keys_to_query = ",".join(["%s"] * len(keys))
            query = f"""
                SELECT key, value, created_at, updated_at
                FROM store
                WHERE prefix = %s AND key IN ({keys_to_query})
            """
            params = (_namespace_to_text(namespace), *keys)
            results.append((query, params, namespace, items))
        return results

    def _prepare_batch_PUT_queries(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
    ) -> tuple[
        list[tuple[str, Sequence]],
        Optional[tuple[str, Sequence[tuple[str, str, str, str]]]],
    ]:
        # Last-write wins
        dedupped_ops: dict[tuple[tuple[str, ...], str], PutOp] = {}
        for _, op in put_ops:
            dedupped_ops[(op.namespace, op.key)] = op

        inserts: list[PutOp] = []
        deletes: list[PutOp] = []
        for op in dedupped_ops.values():
            if op.value is None:
                deletes.append(op)
            else:
                inserts.append(op)

        queries: list[tuple[str, Sequence]] = []

        if deletes:
            namespace_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for op in deletes:
                namespace_groups[op.namespace].append(op.key)
            for namespace, keys in namespace_groups.items():
                placeholders = ",".join(["%s"] * len(keys))
                query = (
                    f"DELETE FROM store WHERE prefix = %s AND key IN ({placeholders})"
                )
                params = (_namespace_to_text(namespace), *keys)
                queries.append((query, params))
        embedding_request: Optional[tuple[str, Sequence[tuple[str, str, str, str]]]] = (
            None
        )
        if inserts:
            values = []
            insertion_params = []
            vector_values = []
            embedding_request_params = []

            # First handle main store insertions
            for op in inserts:
                values.append("(%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
                insertion_params.extend(
                    [
                        _namespace_to_text(op.namespace),
                        op.key,
                        Jsonb(cast(dict, op.value).copy()),
                    ]
                )

            # Then handle embeddings if configured
            if self.embedding_config:
                text_fields = self.embedding_config.get("text_fields", ["__root__"])
                if isinstance(text_fields, str):
                    text_fields = [text_fields]
                elif text_fields is None:
                    text_fields = ["__root__"]

                for op in inserts:
                    value = op.value
                    ns = _namespace_to_text(op.namespace)
                    k = op.key

                    for field in text_fields:
                        for text in _extract_text_by_path(value, field):
                            vector_values.append(
                                "(%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                            )
                            embedding_request_params.append((ns, k, field, text))

            values_str = ",".join(values)
            query = f"""
                INSERT INTO store (prefix, key, value, created_at, updated_at)
                VALUES {values_str}
                ON CONFLICT (prefix, key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = CURRENT_TIMESTAMP
            """
            queries.append((query, insertion_params))

            if vector_values:
                values_str = ",".join(vector_values)
                query = f"""
                    INSERT INTO store_vectors (prefix, key, field_name, embedding, created_at, updated_at)
                    VALUES {values_str}
                    ON CONFLICT (prefix, key, field_name) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        updated_at = CURRENT_TIMESTAMP
                """
                embedding_request = (query, embedding_request_params)

        return queries, embedding_request

    def _prepare_batch_search_queries(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
    ) -> tuple[
        list[tuple[str, list[Union[None, str, list[float]]]]],  # queries, params
        list[tuple[int, str]],  # idx, query_text pairs to embed
    ]:
        queries = []
        embedding_requests = []

        for idx, (_, op) in enumerate(search_ops):
            base_query = """
                SELECT prefix, key, value, created_at, updated_at
                FROM store
                WHERE prefix LIKE %s
            """
            params: list = [f"{_namespace_to_text(op.namespace_prefix)}%"]
            needs_vector_search = False

            if op.query and self.embedding_config:
                needs_vector_search = True
                embedding_requests.append((idx, op.query))
                base_query = """
                    SELECT s.prefix, s.key, s.value, s.created_at, s.updated_at,
                           1 - (sv.embedding <=> %s::vector) as score
                    FROM store s
                    JOIN store_vectors sv ON s.prefix = sv.prefix AND s.key = sv.key
                    WHERE s.prefix LIKE %s
                """
                params = [None, f"{_namespace_to_text(op.namespace_prefix)}%"]

            if op.filter:
                filter_conditions = []
                for key, value in op.filter.items():
                    if isinstance(value, dict):
                        for op_name, val in value.items():
                            condition, filter_params = self._get_filter_condition(
                                key, op_name, val
                            )
                            filter_conditions.append(condition)
                            params.extend(filter_params)
                    else:
                        filter_conditions.append("value->%s = %s::jsonb")
                        params.extend([key, json.dumps(value)])

                if filter_conditions:
                    base_query += " AND " + " AND ".join(filter_conditions)

            order_by = (
                "ORDER BY score DESC"
                if needs_vector_search
                else "ORDER BY updated_at DESC"
            )
            base_query += f" {order_by} LIMIT %s OFFSET %s"
            params.extend([op.limit, op.offset])
            queries.append((base_query, params))

        return queries, embedding_requests

    def _get_batch_list_namespaces_queries(
        self,
        list_ops: Sequence[tuple[int, ListNamespacesOp]],
    ) -> list[tuple[str, Sequence]]:
        queries: list[tuple[str, Sequence]] = []
        for _, op in list_ops:
            query = """
                SELECT DISTINCT ON (truncated_prefix) truncated_prefix, prefix
                FROM (
                    SELECT
                        prefix,
                        CASE
                            WHEN %s::integer IS NOT NULL THEN
                                (SELECT STRING_AGG(part, '.' ORDER BY idx)
                                 FROM (
                                     SELECT part, ROW_NUMBER() OVER () AS idx
                                     FROM UNNEST(REGEXP_SPLIT_TO_ARRAY(prefix, '\.')) AS part
                                     LIMIT %s::integer
                                 ) subquery
                                )
                            ELSE prefix
                        END AS truncated_prefix
                    FROM store
            """
            params: list[Any] = [op.max_depth, op.max_depth]

            conditions = []
            if op.match_conditions:
                for condition in op.match_conditions:
                    if condition.match_type == "prefix":
                        conditions.append("prefix LIKE %s")
                        params.append(
                            f"{_namespace_to_text(condition.path, handle_wildcards=True)}%"
                        )
                    elif condition.match_type == "suffix":
                        conditions.append("prefix LIKE %s")
                        params.append(
                            f"%{_namespace_to_text(condition.path, handle_wildcards=True)}"
                        )
                    else:
                        logger.warning(
                            f"Unknown match_type in list_namespaces: {condition.match_type}"
                        )

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += ") AS subquery "

            query += " ORDER BY truncated_prefix LIMIT %s OFFSET %s"
            params.extend([op.limit, op.offset])
            queries.append((query, tuple(params)))

        return queries

    def _get_filter_condition(self, key: str, op: str, value: Any) -> tuple[str, list]:
        """Helper to generate filter conditions."""
        if op == "$eq":
            return "value->%s = %s::jsonb", [key, json.dumps(value)]
        elif op == "$gt":
            return "value->>%s > %s", [key, str(value)]
        elif op == "$gte":
            return "value->>%s >= %s", [key, str(value)]
        elif op == "$lt":
            return "value->>%s < %s", [key, str(value)]
        elif op == "$lte":
            return "value->>%s <= %s", [key, str(value)]
        elif op == "$ne":
            return "value->%s != %s::jsonb", [key, json.dumps(value)]
        else:
            raise ValueError(f"Unsupported operator: {op}")


class PostgresStore(BaseStore, BasePostgresStore[_pg_internal.Conn]):
    __slots__ = ("_deserializer", "pipe", "lock", "supports_pipeline")

    def __init__(
        self,
        conn: _pg_internal.Conn,
        *,
        pipe: Optional[Pipeline] = None,
        deserializer: Optional[
            Callable[[Union[bytes, orjson.Fragment]], dict[str, Any]]
        ] = None,
        embedding: Optional[EmbeddingConfig] = None,
    ) -> None:
        super().__init__()
        self._deserializer = deserializer
        self.conn = conn
        self.pipe = pipe
        self.supports_pipeline = Capabilities().has_pipeline()
        self.lock = threading.Lock()
        self.embedding_config = embedding
        # TODO: Coerce embedding regular functions

    @classmethod
    @contextmanager
    def from_conn_string(
        cls,
        conn_string: str,
        *,
        pipeline: bool = False,
        embedding: Optional[EmbeddingConfig] = None,
    ) -> Iterator["PostgresStore"]:
        """Create a new PostgresStore instance from a connection string.

        Args:
            conn_string (str): The Postgres connection info string.
            pipeline (bool): whether to use Pipeline
            embedding (Optional[EmbeddingConfig]): The embedding config.

        Returns:
            PostgresStore: A new PostgresStore instance.
        """
        with Connection.connect(
            conn_string, autocommit=True, prepare_threshold=0, row_factory=dict_row
        ) as conn:
            if pipeline:
                with conn.pipeline() as pipe:
                    yield cls(conn, pipe=pipe, embedding=embedding)
            else:
                yield cls(conn, embedding=embedding)

    @contextmanager
    def _cursor(self, *, pipeline: bool = False) -> Iterator[Cursor[DictRow]]:
        """Create a database cursor as a context manager.

        Args:
            pipeline (bool): whether to use pipeline for the DB operations inside the context manager.
                Will be applied regardless of whether the PostgresStore instance was initialized with a pipeline.
                If pipeline mode is not supported, will fall back to using transaction context manager.
        """
        with _pg_internal.get_connection(self.conn) as conn:
            if self.pipe:
                # a connection in pipeline mode can be used concurrently
                # in multiple threads/coroutines, but only one cursor can be
                # used at a time
                try:
                    with conn.cursor(binary=True, row_factory=dict_row) as cur:
                        yield cur
                finally:
                    if pipeline:
                        self.pipe.sync()
            elif pipeline:
                # a connection not in pipeline mode can only be used by one
                # thread/coroutine at a time, so we acquire a lock
                if self.supports_pipeline:
                    with self.lock, conn.pipeline(), conn.cursor(
                        binary=True, row_factory=dict_row
                    ) as cur:
                        yield cur
                else:
                    with self.lock, conn.transaction(), conn.cursor(
                        binary=True, row_factory=dict_row
                    ) as cur:
                        yield cur
            else:
                with conn.cursor(binary=True, row_factory=dict_row) as cur:
                    yield cur

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        grouped_ops, num_ops = _group_ops(ops)
        results: list[Result] = [None] * num_ops

        with self._cursor(pipeline=True) as cur:
            if GetOp in grouped_ops:
                self._batch_get_ops(
                    cast(Sequence[tuple[int, GetOp]], grouped_ops[GetOp]), results, cur
                )

            if SearchOp in grouped_ops:
                self._batch_search_ops(
                    cast(Sequence[tuple[int, SearchOp]], grouped_ops[SearchOp]),
                    results,
                    cur,
                )

            if ListNamespacesOp in grouped_ops:
                self._batch_list_namespaces_ops(
                    cast(
                        Sequence[tuple[int, ListNamespacesOp]],
                        grouped_ops[ListNamespacesOp],
                    ),
                    results,
                    cur,
                )
            if PutOp in grouped_ops:
                self._batch_put_ops(
                    cast(Sequence[tuple[int, PutOp]], grouped_ops[PutOp]), cur
                )

        return results

    def _batch_get_ops(
        self,
        get_ops: Sequence[tuple[int, GetOp]],
        results: list[Result],
        cur: Cursor[DictRow],
    ) -> None:
        for query, params, namespace, items in self._get_batch_GET_ops_queries(get_ops):
            cur.execute(query, params)
            rows = cast(list[Row], cur.fetchall())
            key_to_row = {row["key"]: row for row in rows}
            for idx, key in items:
                row = key_to_row.get(key)
                if row:
                    results[idx] = _row_to_item(
                        namespace, row, loader=self._deserializer
                    )
                else:
                    results[idx] = None

    def _batch_put_ops(
        self,
        put_ops: Sequence[tuple[int, PutOp]],
        cur: Cursor[DictRow],
    ) -> None:
        queries, embedding_request = self._prepare_batch_PUT_queries(put_ops)
        if embedding_request:
            if self.embedding_config is None:
                # Should not get here since the embedding config is required
                # to return an embedding_request above
                raise ValueError(
                    "Embedding configuration is required for vector operations "
                    f"(for semantic search). "
                    f"Please provide an EmbeddingConfig when initializing the {self.__class__.__name__}."
                )
            query, txt_params = embedding_request
            # Update the params to replace the raw text with the vectors
            vectors = self.embedding_config["embed"].embed_documents(
                [param[-1] for param in txt_params]
            )
            queries.extend(
                [
                    (query, (ns, key, value, vector))
                    for (ns, key, value, _), vector in zip(txt_params, vectors)
                ]
            )

        for query, params in queries:
            cur.execute(query, params)

    def _batch_search_ops(
        self,
        search_ops: Sequence[tuple[int, SearchOp]],
        results: list[Result],
        cur: Cursor[DictRow],
    ) -> None:
        queries, embedding_requests = self._prepare_batch_search_queries(search_ops)

        if embedding_requests and self.embedding_config:
            embeddings = self.embedding_config["embed"].embed_documents(
                [query for _, query in embedding_requests]
            )
            for (idx, _), embedding in zip(embedding_requests, embeddings):
                queries[idx][1][0] = embedding

        for (idx, _), (query, params) in zip(search_ops, queries):
            cur.execute(query, params)
            rows = cast(list[Row], cur.fetchall())
            items = [
                _row_to_item(
                    _decode_ns_bytes(row["prefix"]),
                    row,
                    loader=self._deserializer,
                    cls=SearchItem,
                )
                for row in rows
            ]
            results[idx] = items

    def _batch_list_namespaces_ops(
        self,
        list_ops: Sequence[tuple[int, ListNamespacesOp]],
        results: list[Result],
        cur: Cursor[DictRow],
    ) -> None:
        for (query, params), (idx, _) in zip(
            self._get_batch_list_namespaces_queries(list_ops), list_ops
        ):
            cur.execute(query, params)
            results[idx] = [_decode_ns_bytes(row["truncated_prefix"]) for row in cur]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.get_running_loop().run_in_executor(None, self.batch, ops)

    def setup(self) -> None:
        """Set up the store database.

        This method creates the necessary tables in the Postgres database if they don't
        already exist and runs database migrations. It MUST be called directly by the user
        the first time the store is used.
        """
        with self._cursor() as cur:
            try:
                cur.execute("SELECT v FROM store_migrations ORDER BY v DESC LIMIT 1")
                row = cast(dict, cur.fetchone())
                if row is None:
                    version = -1
                else:
                    version = row["v"]
            except UndefinedTable:
                version = -1
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS store_migrations (
                        v INTEGER PRIMARY KEY
                    )
                """
                )

            for v, migration in enumerate(
                self.MIGRATIONS[version + 1 :], start=version + 1
            ):
                if isinstance(migration, str):
                    sql = migration
                else:
                    if migration.condition and not migration.condition(self):
                        continue

                    sql = migration.sql
                    if migration.params:
                        params = {
                            k: v(self) if v is not None and callable(v) else v
                            for k, v in migration.params.items()
                        }
                        try:
                            sql = sql % params
                        except Exception as e:
                            logger.warning(f"Failed to format migration {v}: {e}")
                            if migration.condition == check_vector_available:
                                self.embedding_config = None
                            continue

                try:
                    cur.execute(sql)
                    cur.execute("INSERT INTO store_migrations (v) VALUES (%s)", (v,))
                except Exception as e:
                    logger.warning(f"Failed to run migration {v}: {e}")
                    if (
                        not isinstance(migration, str)
                        and migration.condition == check_vector_available
                    ):
                        self.embedding_config = None
                    continue


class Row(TypedDict):
    key: str
    value: Any
    prefix: str
    created_at: datetime
    updated_at: datetime


def _namespace_to_text(
    namespace: tuple[str, ...], handle_wildcards: bool = False
) -> str:
    """Convert namespace tuple to text string."""
    if handle_wildcards:
        namespace = tuple("%" if val == "*" else val for val in namespace)
    return ".".join(namespace)


def _row_to_item(
    namespace: tuple[str, ...],
    row: Row,
    *,
    loader: Optional[Callable[[Union[bytes, orjson.Fragment]], dict[str, Any]]] = None,
    cls: Union[Type[SearchItem], Type[Item]] = Item,
) -> Union[Item, SearchItem]:
    """Convert a row from the database into an Item.

    Args:
        namespace: Item namespace
        row: Database row
        loader: Optional value loader for non-dict values
        cls: Item class to instantiate (Item or SearchItem)
    """
    val = row["value"]
    if not isinstance(val, dict):
        val = (loader or _json_loads)(val)

    kwargs = {
        "key": row["key"],
        "namespace": namespace,
        "value": val,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

    if cls is SearchItem and "score" in row:
        kwargs["response_metadata"] = {"score": float(row["score"])}

    return cls(**kwargs)


def _group_ops(ops: Iterable[Op]) -> tuple[dict[type, list[tuple[int, Op]]], int]:
    grouped_ops: dict[type, list[tuple[int, Op]]] = defaultdict(list)
    tot = 0
    for idx, op in enumerate(ops):
        grouped_ops[type(op)].append((idx, op))
        tot += 1
    return grouped_ops, tot


def _json_loads(content: Union[bytes, orjson.Fragment]) -> Any:
    if isinstance(content, orjson.Fragment):
        if hasattr(content, "buf"):
            content = content.buf
        else:
            if isinstance(content.contents, bytes):
                content = content.contents
            else:
                content = content.contents.encode()
    return orjson.loads(cast(bytes, content))


def _decode_ns_bytes(namespace: Union[str, bytes, list]) -> tuple[str, ...]:
    if isinstance(namespace, list):
        return tuple(namespace)
    if isinstance(namespace, bytes):
        namespace = namespace.decode()[1:]
    return tuple(namespace.split("."))


def _tokenize_path(path: str) -> list[str]:
    """Tokenize a path into components.

    Handles:
    - Simple paths: "field1.field2"
    - Array indexing: "[0]", "[*]", "[-1]"
    - Wildcards: "*"
    - Multi-field selection: "{field1,field2}"
    """
    if not path:
        return []

    tokens = []
    current: List[str] = []
    i = 0
    while i < len(path):
        char = path[i]

        if char == "[":  # Handle array index
            if current:
                tokens.append("".join(current))
                current = []
            bracket_count = 1
            index_chars = ["["]
            i += 1
            while i < len(path) and bracket_count > 0:
                if path[i] == "[":
                    bracket_count += 1
                elif path[i] == "]":
                    bracket_count -= 1
                index_chars.append(path[i])
                i += 1
            tokens.append("".join(index_chars))
            continue

        elif char == "{":  # Handle multi-field selection
            if current:
                tokens.append("".join(current))
                current = []
            brace_count = 1
            field_chars = ["{"]
            i += 1
            while i < len(path) and brace_count > 0:
                if path[i] == "{":
                    brace_count += 1
                elif path[i] == "}":
                    brace_count -= 1
                field_chars.append(path[i])
                i += 1
            tokens.append("".join(field_chars))
            continue

        elif char == ".":
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
        i += 1

    if current:
        tokens.append("".join(current))

    return tokens


def _extract_text_by_path(obj: Any, path: str) -> list[str]:
    """Extract text from an object using a path expression.

    Supports:
    - Simple paths: "field1.field2"
    - Array indexing: "[0]", "[*]", "[-1]"
    - Wildcards: "*"
    - Multi-field selection: "{field1,field2}"
    - Nested paths in multi-field: "{field1,nested.field2}"
    """
    if not path or path == "__root__":
        return [json.dumps(obj, sort_keys=True)]

    def _extract_from_obj(obj: Any, tokens: list[str], pos: int) -> list[str]:
        if pos >= len(tokens):
            if isinstance(obj, (str, int, float, bool)):
                return [str(obj)]
            elif obj is None:
                return []
            elif isinstance(obj, (list, dict)):
                return [json.dumps(obj, sort_keys=True)]
            return []

        token = tokens[pos]
        results = []

        if token.startswith("[") and token.endswith("]"):
            if not isinstance(obj, list):
                return []

            index = token[1:-1]
            if index == "*":
                for item in obj:
                    results.extend(_extract_from_obj(item, tokens, pos + 1))
            else:
                try:
                    idx = int(index)
                    if idx < 0:
                        idx = len(obj) + idx
                    if 0 <= idx < len(obj):
                        results.extend(_extract_from_obj(obj[idx], tokens, pos + 1))
                except (ValueError, IndexError):
                    return []

        elif token.startswith("{") and token.endswith("}"):
            if not isinstance(obj, dict):
                return []

            fields = [f.strip() for f in token[1:-1].split(",")]
            for field in fields:
                nested_tokens = _tokenize_path(field)
                if nested_tokens:
                    current_obj: Optional[dict] = obj
                    for nested_token in nested_tokens:
                        if (
                            isinstance(current_obj, dict)
                            and nested_token in current_obj
                        ):
                            current_obj = current_obj[nested_token]
                        else:
                            current_obj = None
                            break
                    if current_obj is not None:
                        if isinstance(current_obj, (str, int, float, bool)):
                            results.append(str(current_obj))
                        elif isinstance(current_obj, (list, dict)):
                            results.append(json.dumps(current_obj, sort_keys=True))

        # Handle wildcard
        elif token == "*":
            if isinstance(obj, dict):
                for value in obj.values():
                    results.extend(_extract_from_obj(value, tokens, pos + 1))
            elif isinstance(obj, list):
                for item in obj:
                    results.extend(_extract_from_obj(item, tokens, pos + 1))

        # Handle regular field
        else:
            if isinstance(obj, dict) and token in obj:
                results.extend(_extract_from_obj(obj[token], tokens, pos + 1))

        return results

    tokens = _tokenize_path(path)
    return _extract_from_obj(obj, tokens, 0)
