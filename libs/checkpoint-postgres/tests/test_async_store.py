# type: ignore
import sys
import uuid
from typing import AsyncIterator

import pytest
from conftest import DEFAULT_URI  # type: ignore
from psycopg import AsyncConnection
from test_store import CharacterEmbeddings

from langgraph.store.base import GetOp, Item, ListNamespacesOp, PutOp, SearchOp
from langgraph.store.postgres import AsyncPostgresStore


@pytest.fixture(scope="function", params=["default", "pipe", "pool"])
async def store(request) -> AsyncIterator[AsyncPostgresStore]:
    if sys.version_info < (3, 10):
        pytest.skip("Async Postgres tests require Python 3.10+")

    database = f"test_{uuid.uuid4().hex[:16]}"
    uri_parts = DEFAULT_URI.split("/")
    uri_base = "/".join(uri_parts[:-1])
    query_params = ""
    if "?" in uri_parts[-1]:
        db_name, query_params = uri_parts[-1].split("?", 1)
        query_params = "?" + query_params

    conn_string = f"{uri_base}/{database}{query_params}"
    admin_conn_string = DEFAULT_URI

    async with await AsyncConnection.connect(
        admin_conn_string, autocommit=True
    ) as conn:
        await conn.execute(f"CREATE DATABASE {database}")
    try:
        async with AsyncPostgresStore.from_conn_string(conn_string) as store:
            await store.setup()

        if request.param == "pipe":
            async with AsyncPostgresStore.from_conn_string(
                conn_string, pipeline=True
            ) as store:
                yield store
        elif request.param == "pool":
            async with AsyncPostgresStore.from_conn_string(
                conn_string, use_pool=True, max_size=10
            ) as store:
                await store.setup()
                yield store
        else:  # default
            async with AsyncPostgresStore.from_conn_string(conn_string) as store:
                yield store
    finally:
        async with await AsyncConnection.connect(
            admin_conn_string, autocommit=True
        ) as conn:
            await conn.execute(f"DROP DATABASE {database}")


async def test_abatch_order(store: AsyncPostgresStore) -> None:
    # Setup test data
    await store.aput(("test", "foo"), "key1", {"data": "value1"})
    await store.aput(("test", "bar"), "key2", {"data": "value2"})

    ops = [
        GetOp(namespace=("test", "foo"), key="key1"),
        PutOp(namespace=("test", "bar"), key="key2", value={"data": "value2"}),
        SearchOp(
            namespace_prefix=("test",), filter={"data": "value1"}, limit=10, offset=0
        ),
        ListNamespacesOp(match_conditions=None, max_depth=None, limit=10, offset=0),
        GetOp(namespace=("test",), key="key3"),
    ]

    results = await store.abatch(ops)
    assert len(results) == 5
    assert isinstance(results[0], Item)
    assert isinstance(results[0].value, dict)
    assert results[0].value == {"data": "value1"}
    assert results[0].key == "key1"
    assert results[1] is None
    assert isinstance(results[2], list)
    assert len(results[2]) == 1
    assert isinstance(results[3], list)
    assert ("test", "foo") in results[3] and ("test", "bar") in results[3]
    assert results[4] is None

    ops_reordered = [
        SearchOp(namespace_prefix=("test",), filter=None, limit=5, offset=0),
        GetOp(namespace=("test", "bar"), key="key2"),
        ListNamespacesOp(match_conditions=None, max_depth=None, limit=5, offset=0),
        PutOp(namespace=("test",), key="key3", value={"data": "value3"}),
        GetOp(namespace=("test", "foo"), key="key1"),
    ]

    results_reordered = await store.abatch(ops_reordered)
    assert len(results_reordered) == 5
    assert isinstance(results_reordered[0], list)
    assert len(results_reordered[0]) == 2
    assert isinstance(results_reordered[1], Item)
    assert results_reordered[1].value == {"data": "value2"}
    assert results_reordered[1].key == "key2"
    assert isinstance(results_reordered[2], list)
    assert ("test", "foo") in results_reordered[2] and (
        "test",
        "bar",
    ) in results_reordered[2]
    assert results_reordered[3] is None
    assert isinstance(results_reordered[4], Item)
    assert results_reordered[4].value == {"data": "value1"}
    assert results_reordered[4].key == "key1"


async def test_batch_get_ops(store: AsyncPostgresStore) -> None:
    # Setup test data
    await store.aput(("test",), "key1", {"data": "value1"})
    await store.aput(("test",), "key2", {"data": "value2"})

    ops = [
        GetOp(namespace=("test",), key="key1"),
        GetOp(namespace=("test",), key="key2"),
        GetOp(namespace=("test",), key="key3"),
    ]

    results = await store.abatch(ops)

    assert len(results) == 3
    assert results[0] is not None
    assert results[1] is not None
    assert results[2] is None
    assert results[0].key == "key1"
    assert results[1].key == "key2"


async def test_batch_put_ops(store: AsyncPostgresStore) -> None:
    ops = [
        PutOp(namespace=("test",), key="key1", value={"data": "value1"}),
        PutOp(namespace=("test",), key="key2", value={"data": "value2"}),
        PutOp(namespace=("test",), key="key3", value=None),
    ]

    results = await store.abatch(ops)

    assert len(results) == 3
    assert all(result is None for result in results)

    # Verify the puts worked
    items = await store.asearch(["test"], limit=10)
    assert len(items) == 2  # key3 had None value so wasn't stored


async def test_batch_search_ops(store: AsyncPostgresStore) -> None:
    # Setup test data
    await store.aput(("test", "foo"), "key1", {"data": "value1"})
    await store.aput(("test", "bar"), "key2", {"data": "value2"})

    ops = [
        SearchOp(
            namespace_prefix=("test",), filter={"data": "value1"}, limit=10, offset=0
        ),
        SearchOp(namespace_prefix=("test",), filter=None, limit=5, offset=0),
    ]

    results = await store.abatch(ops)

    assert len(results) == 2
    assert len(results[0]) == 1  # Filtered results
    assert len(results[1]) == 2  # All results


async def test_batch_list_namespaces_ops(store: AsyncPostgresStore) -> None:
    # Setup test data
    await store.aput(("test", "namespace1"), "key1", {"data": "value1"})
    await store.aput(("test", "namespace2"), "key2", {"data": "value2"})

    ops = [ListNamespacesOp(match_conditions=None, max_depth=None, limit=10, offset=0)]

    results = await store.abatch(ops)

    assert len(results) == 1
    assert len(results[0]) == 2
    assert ("test", "namespace1") in results[0]
    assert ("test", "namespace2") in results[0]


@pytest.fixture
async def vector_store(
    fake_embeddings: CharacterEmbeddings,
) -> AsyncIterator[AsyncPostgresStore]:
    """Create a store with vector search enabled."""
    if sys.version_info < (3, 10):
        pytest.skip("Async Postgres tests require Python 3.10+")

    database = f"test_{uuid.uuid4().hex[:16]}"
    uri_parts = DEFAULT_URI.split("/")
    uri_base = "/".join(uri_parts[:-1])
    query_params = ""
    if "?" in uri_parts[-1]:
        db_name, query_params = uri_parts[-1].split("?", 1)
        query_params = "?" + query_params

    conn_string = f"{uri_base}/{database}{query_params}"
    admin_conn_string = DEFAULT_URI

    async with await AsyncConnection.connect(
        admin_conn_string, autocommit=True
    ) as conn:
        await conn.execute(f"CREATE DATABASE {database}")
    try:
        async with AsyncPostgresStore.from_conn_string(
            conn_string,
            embedding={"dims": fake_embeddings.dims, "embed": fake_embeddings},
        ) as store:
            await store.setup()
            yield store
    finally:
        async with await AsyncConnection.connect(
            admin_conn_string, autocommit=True
        ) as conn:
            await conn.execute(f"DROP DATABASE {database}")


async def test_vector_store_initialization(
    vector_store: AsyncPostgresStore, fake_embeddings: CharacterEmbeddings
) -> None:
    """Test store initialization with embedding config."""
    assert vector_store.embedding_config is not None
    assert vector_store.embedding_config["dims"] == fake_embeddings.dims
    assert vector_store.embedding_config["embed"] == fake_embeddings


async def test_vector_insert_with_auto_embedding(
    vector_store: AsyncPostgresStore,
) -> None:
    """Test inserting items that get auto-embedded."""
    docs = [
        ("doc1", {"text": "short text"}),
        ("doc2", {"text": "longer text document"}),
        ("doc3", {"text": "longest text document here"}),
        ("doc4", {"description": "text in description field"}),
        ("doc5", {"content": "text in content field"}),
        ("doc6", {"body": "text in body field"}),
    ]

    for key, value in docs:
        await vector_store.aput(("test",), key, value)

    results = await vector_store.asearch(("test",), query="long text")
    assert len(results) > 0

    doc_order = [r.key for r in results]
    assert "doc2" in doc_order
    assert "doc3" in doc_order


async def test_vector_update_with_embedding(vector_store: AsyncPostgresStore) -> None:
    """Test that updating items properly updates their embeddings."""
    await vector_store.aput(("test",), "doc1", {"text": "initial text about cats"})
    await vector_store.aput(("test",), "doc2", {"text": "something about dogs"})
    await vector_store.aput(("test",), "doc3", {"text": "text about birds"})

    results_initial = await vector_store.asearch(("test",), query="cats")
    assert len(results_initial) > 0
    assert results_initial[0].key == "doc1"
    initial_score = results_initial[0].response_metadata["score"]

    await vector_store.aput(("test",), "doc1", {"text": "new text about dogs"})

    results_after = await vector_store.asearch(("test",), query="cats")
    after_score = next(
        (r.response_metadata["score"] for r in results_after if r.key == "doc1"), 0.0
    )
    assert after_score < initial_score

    results_new = await vector_store.asearch(("test",), query="dogs")
    for r in results_new:
        if r.key == "doc1":
            assert r.response_metadata["score"] > after_score


async def test_vector_search_with_filters(vector_store: AsyncPostgresStore) -> None:
    """Test combining vector search with filters."""
    docs = [
        ("doc1", {"text": "red apple", "color": "red", "score": 4.5}),
        ("doc2", {"text": "red car", "color": "red", "score": 3.0}),
        ("doc3", {"text": "green apple", "color": "green", "score": 4.0}),
        ("doc4", {"text": "blue car", "color": "blue", "score": 3.5}),
    ]

    for key, value in docs:
        await vector_store.aput(("test",), key, value)

    results = await vector_store.asearch(
        ("test",), query="apple", filter={"color": "red"}
    )
    assert len(results) == 2
    assert results[0].key == "doc1"

    results = await vector_store.asearch(
        ("test",), query="car", filter={"color": "red"}
    )
    assert len(results) == 2
    assert results[0].key == "doc2"

    results = await vector_store.asearch(
        ("test",), query="bbbbluuu", filter={"score": {"$gt": 3.2}}
    )
    assert len(results) == 3
    assert results[0].key == "doc4"

    results = await vector_store.asearch(
        ("test",), query="apple", filter={"score": {"$gte": 4.0}, "color": "green"}
    )
    assert len(results) == 1
    assert results[0].key == "doc3"


async def test_vector_search_pagination(vector_store: AsyncPostgresStore) -> None:
    """Test pagination with vector search."""
    for i in range(5):
        await vector_store.aput(
            ("test",), f"doc{i}", {"text": f"test document number {i}"}
        )

    results_page1 = await vector_store.asearch(("test",), query="test", limit=2)
    results_page2 = await vector_store.asearch(
        ("test",), query="test", limit=2, offset=2
    )

    assert len(results_page1) == 2
    assert len(results_page2) == 2
    assert results_page1[0].key != results_page2[0].key

    all_results = await vector_store.asearch(("test",), query="test", limit=10)
    assert len(all_results) == 5


async def test_vector_search_edge_cases(vector_store: AsyncPostgresStore) -> None:
    """Test edge cases in vector search."""
    await vector_store.aput(("test",), "doc1", {"text": "test document"})

    perfect_match = await vector_store.asearch(("test",), query="text test document")
    perfect_score = perfect_match[0].response_metadata["score"]

    results = await vector_store.asearch(("test",), query="")
    assert len(results) == 1
    assert "score" not in results[0].response_metadata

    results = await vector_store.asearch(("test",), query=None)
    assert len(results) == 1
    assert "score" not in results[0].response_metadata

    long_query = "foo " * 100
    results = await vector_store.asearch(("test",), query=long_query)
    assert len(results) == 1
    assert results[0].response_metadata["score"] < perfect_score

    special_query = "test!@#$%^&*()"
    results = await vector_store.asearch(("test",), query=special_query)
    assert len(results) == 1
    assert results[0].response_metadata["score"] < perfect_score
