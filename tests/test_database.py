import asyncio
import pytest
import uuid

from sqlalchemy import Table, Column, String, MetaData
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.database import engine, get_db


async def insert_record(session: AsyncSession, id: str) -> None:
    """Insert a record into the test table."""
    await session.execute(text("INSERT INTO test_table (id) VALUES (:id)"), {"id": id})
    await session.commit()


@pytest.mark.asyncio
async def test_sqlite_concurrent_writes():
    """Test that SQLite can handle concurrent writes without locking errors."""
    metadata = MetaData()
    test_table = Table("test_table", metadata, Column("id", String, primary_key=True))

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: test_table.create(sync_conn, checkfirst=True)
        )

    try:
        ids = [str(uuid.uuid4()) for _ in range(20)]

        async def insert_task(id_to_insert: str) -> float:
            start_time = asyncio.get_event_loop().time()
            async with get_db() as session:
                await asyncio.sleep(0.01)
                await insert_record(session, id_to_insert)
            end_time = asyncio.get_event_loop().time()
            return end_time - start_time

        start = asyncio.get_event_loop().time()
        tasks = [insert_task(id) for id in ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_time = asyncio.get_event_loop().time() - start

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Exceptions occurred: {exceptions}"

        timings = [r for r in results if not isinstance(r, Exception)]

        async with get_db() as session:
            result = await session.execute(text("SELECT count(*) FROM test_table"))
            count = result.scalar()
            assert count == len(ids), f"Expected {len(ids)} records, got {count}"

            for id in ids:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM test_table WHERE id = :id"), {"id": id}
                )
                id_count = result.scalar()
                assert id_count == 1, (
                    f"ID {id} was inserted {id_count} times instead of once"
                )

        print("\nConcurrent SQLite operations statistics:")
        print(f"Total records: {len(ids)}")
        print(f"Total execution time: {total_time:.4f}s")
        print(f"Average operation time: {sum(timings) / len(timings):.4f}s")
        print("All operations completed without locking errors")

    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS test_table"))
