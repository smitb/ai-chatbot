"""Implementation of a langgraph checkpoint saver using Redis."""
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator, Union, Optional
import redis
from redis.asyncio import Redis as AsyncRedis, ConnectionPool as AsyncConnectionPool
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint import BaseCheckpointSaver
from langgraph.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, CheckpointTuple
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JsonAndBinarySerializer(JsonPlusSerializer):
    def _default(self, obj: Any) -> Any:
        if isinstance(obj, (bytes, bytearray)):
            return self._encode_constructor_args(
                obj.__class__, method="fromhex", args=[obj.hex()]
            )
        return super()._default(obj)

    def dumps(self, obj: Any) -> str:
        try:
            if isinstance(obj, (bytes, bytearray)):
                return obj.hex()
            return super().dumps(obj)
        except Exception as e:
            logger.error(f"Serialization error: {e}")
            raise

    def loads(self, s: str, is_binary: bool = False) -> Any:
        try:
            if is_binary:
                return bytes.fromhex(s)
            return super().loads(s)
        except Exception as e:
            logger.error(f"Deserialization error: {e}")
            raise


def initialize_sync_pool(
    host: str = "localhost", port: int = 6379, db: int = 0, **kwargs
) -> redis.ConnectionPool:
    """Initialize a synchronous Redis connection pool."""
    try:
        pool = redis.ConnectionPool(host=host, port=port, db=db, **kwargs)
        logger.info(
            f"Synchronous Redis pool initialized with host={host}, port={port}, db={db}"
        )
        return pool
    except Exception as e:
        logger.error(f"Error initializing sync pool: {e}")
        raise


def initialize_async_pool(
    url: str = "redis://localhost", **kwargs
) -> AsyncConnectionPool:
    """Initialize an asynchronous Redis connection pool."""
    try:
        pool = AsyncConnectionPool.from_url(url, **kwargs)
        logger.info(f"Asynchronous Redis pool initialized with url={url}")
        return pool
    except Exception as e:
        logger.error(f"Error initializing async pool: {e}")
        raise


@contextmanager
def _get_sync_connection(
    connection: Union[redis.Redis, redis.ConnectionPool, None]
) -> Generator[redis.Redis, None, None]:
    conn = None
    try:
        if isinstance(connection, redis.Redis):
            yield connection
        elif isinstance(connection, redis.ConnectionPool):
            conn = redis.Redis(connection_pool=connection)
            yield conn
        else:
            raise ValueError("Invalid sync connection object.")
    except redis.ConnectionError as e:
        logger.error(f"Sync connection error: {e}")
        raise
    finally:
        if conn:
            conn.close()


@asynccontextmanager
async def _get_async_connection(
    connection: Union[AsyncRedis, AsyncConnectionPool, None]
) -> AsyncGenerator[AsyncRedis, None]:
    conn = None
    try:
        if isinstance(connection, AsyncRedis):
            yield connection
        elif isinstance(connection, AsyncConnectionPool):
            conn = AsyncRedis(connection_pool=connection)
            yield conn
        else:
            raise ValueError("Invalid async connection object.")
    except redis.ConnectionError as e:
        logger.error(f"Async connection error: {e}")
        raise
    finally:
        if conn:
            await conn.aclose()


class RedisSaver(BaseCheckpointSaver):
    sync_connection: Optional[Union[redis.Redis, redis.ConnectionPool]] = None
    async_connection: Optional[Union[AsyncRedis, AsyncConnectionPool]] = None

    def __init__(
        self,
        sync_connection: Optional[Union[redis.Redis, redis.ConnectionPool]] = None,
        async_connection: Optional[Union[AsyncRedis, AsyncConnectionPool]] = None,
    ):
        super().__init__(serde=JsonAndBinarySerializer())
        self.sync_connection = sync_connection
        self.async_connection = async_connection

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        parent_ts = config["configurable"].get("thread_ts")
        key = f"checkpoint:{thread_id}:{checkpoint['ts']}"
        try:
            with _get_sync_connection(self.sync_connection) as conn:
                conn.hset(
                    key,
                    mapping={
                        "checkpoint": self.serde.dumps(checkpoint),
                        "metadata": self.serde.dumps(metadata),
                        "parent_ts": parent_ts if parent_ts else "",
                    },
                )
                logger.info(
                    f"Checkpoint stored successfully for thread_id: {thread_id}, ts: {checkpoint['ts']}"
                )
        except Exception as e:
            logger.error(f"Failed to put checkpoint: {e}")
            raise
        return {
            "configurable": {
                "thread_id": thread_id,
                "thread_ts": checkpoint["ts"],
            },
        }

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        parent_ts = config["configurable"].get("thread_ts")
        key = f"checkpoint:{thread_id}:{checkpoint['ts']}"
        try:
            async with _get_async_connection(self.async_connection) as conn:
                await conn.hset(
                    key,
                    mapping={
                        "checkpoint": self.serde.dumps(checkpoint),
                        "metadata": self.serde.dumps(metadata),
                        "parent_ts": parent_ts if parent_ts else "",
                    },
                )
                logger.info(
                    f"Checkpoint stored successfully for thread_id: {thread_id}, ts: {checkpoint['ts']}"
                )
        except Exception as e:
            logger.error(f"Failed to aput checkpoint: {e}")
            raise
        return {
            "configurable": {
                "thread_id": thread_id,
                "thread_ts": checkpoint["ts"],
            },
        }

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        thread_ts = config["configurable"].get("thread_ts", None)
        try:
            with _get_sync_connection(self.sync_connection) as conn:
                if thread_ts:
                    key = f"checkpoint:{thread_id}:{thread_ts}"
                else:
                    all_keys = conn.keys(f"checkpoint:{thread_id}:*")
                    if not all_keys:
                        logger.info(f"No checkpoints found for thread_id: {thread_id}")
                        return None
                    latest_key = max(all_keys, key=lambda k: k.decode().split(":")[-1])
                    key = latest_key.decode()
                checkpoint_data = conn.hgetall(key)
                if not checkpoint_data:
                    logger.info(f"No valid checkpoint data found for key: {key}")
                    return None
                checkpoint = self.serde.loads(checkpoint_data[b"checkpoint"].decode())
                metadata = self.serde.loads(checkpoint_data[b"metadata"].decode())
                parent_ts = checkpoint_data.get(b"parent_ts", b"").decode()
                parent_config = (
                    {"configurable": {"thread_id": thread_id, "thread_ts": parent_ts}}
                    if parent_ts
                    else None
                )
                logger.info(
                    f"Checkpoint retrieved successfully for thread_id: {thread_id}, ts: {thread_ts}"
                )
                return CheckpointTuple(
                    config=config,
                    checkpoint=checkpoint,
                    metadata=metadata,
                    parent_config=parent_config,
                )
        except Exception as e:
            logger.error(f"Failed to get checkpoint tuple: {e}")
            raise

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        thread_ts = config["configurable"].get("thread_ts", None)
        try:
            async with _get_async_connection(self.async_connection) as conn:
                if thread_ts:
                    key = f"checkpoint:{thread_id}:{thread_ts}"
                else:
                    all_keys = await conn.keys(f"checkpoint:{thread_id}:*")
                    if not all_keys:
                        logger.info(f"No checkpoints found for thread_id: {thread_id}")
                        return None
                    latest_key = max(all_keys, key=lambda k: k.decode().split(":")[-1])
                    key = latest_key.decode()
                checkpoint_data = await conn.hgetall(key)
                if not checkpoint_data:
                    logger.info(f"No valid checkpoint data found for key: {key}")
                    return None
                checkpoint = self.serde.loads(checkpoint_data[b"checkpoint"].decode())
                metadata = self.serde.loads(checkpoint_data[b"metadata"].decode())
                parent_ts = checkpoint_data.get(b"parent_ts", b"").decode()
                parent_config = (
                    {"configurable": {"thread_id": thread_id, "thread_ts": parent_ts}}
                    if parent_ts
                    else None
                )
                logger.info(
                    f"Checkpoint retrieved successfully for thread_id: {thread_id}, ts: {thread_ts}"
                )
                return CheckpointTuple(
                    config=config,
                    checkpoint=checkpoint,
                    metadata=metadata,
                    parent_config=parent_config,
                )
        except Exception as e:
            logger.error(f"Failed to get checkpoint tuple: {e}")
            raise

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Generator[CheckpointTuple, None, None]:
        thread_id = config["configurable"]["thread_id"] if config else "*"
        pattern = f"checkpoint:{thread_id}:*"
        try:
            with _get_sync_connection(self.sync_connection) as conn:
                keys = conn.keys(pattern)
                if before:
                    keys = [
                        k
                        for k in keys
                        if k.decode().split(":")[-1] < before["configurable"]["thread_ts"]
                    ]
                keys = sorted(
                    keys, key=lambda k: k.decode().split(":")[-1], reverse=True
                )
                if limit:
                    keys = keys[:limit]
                for key in keys:
                    data = conn.hgetall(key)
                    if data and "checkpoint" in data and "metadata" in data:
                        thread_ts = key.decode().split(":")[-1]
                        yield CheckpointTuple(
                            config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "thread_ts": thread_ts,
                                }
                            },
                            checkpoint=self.serde.loads(data["checkpoint"].decode()),
                            metadata=self.serde.loads(data["metadata"].decode()),
                            parent_config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "thread_ts": data.get("parent_ts", b"").decode(),
                                }
                            }
                            if data.get("parent_ts")
                            else None,
                        )
                        logger.info(
                            f"Checkpoint listed for thread_id: {thread_id}, ts: {thread_ts}"
                        )
        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            raise

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncGenerator[CheckpointTuple, None]:
        thread_id = config["configurable"]["thread_id"] if config else "*"
        pattern = f"checkpoint:{thread_id}:*"
        try:
            async with _get_async_connection(self.async_connection) as conn:
                keys = await conn.keys(pattern)
                if before:
                    keys = [
                        k
                        for k in keys
                        if k.decode().split(":")[-1] < before["configurable"]["thread_ts"]
                    ]
                keys = sorted(
                    keys, key=lambda k: k.decode().split(":")[-1], reverse=True
                )
                if limit:
                    keys = keys[:limit]
                for key in keys:
                    data = await conn.hgetall(key)
                    if data and "checkpoint" in data and "metadata" in data:
                        thread_ts = key.decode().split(":")[-1]
                        yield CheckpointTuple(
                            config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "thread_ts": thread_ts,
                                }
                            },
                            checkpoint=self.serde.loads(data["checkpoint"].decode()),
                            metadata=self.serde.loads(data["metadata"].decode()),
                            parent_config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "thread_ts": data.get("parent_ts", b"").decode(),
                                }
                            }
                            if data.get("parent_ts")
                            else None,
                        )
                        logger.info(
                            f"Checkpoint listed for thread_id: {thread_id}, ts: {thread_ts}"
                        )
        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            raise
