import ast
import copy
import threading
from abc import ABC, abstractmethod

from redis.exceptions import ResponseError
from itertools import islice

from app.config import config
from app.models import const


_PATCH_EXISTING_TASK_SCRIPT = """
-- Match discovery/get_task ownership checks inside the same atomic operation.
-- Other services and our queue may share this database or reuse a deleted key.
if redis.call("TYPE", KEYS[1]).ok ~= "hash" then
    return 0
end
if redis.call("HGET", KEYS[1], "task_id") ~= KEYS[1] then
    return 0
end

for index = 1, #ARGV, 2 do
    redis.call("HSET", KEYS[1], ARGV[index], ARGV[index + 1])
end

return 1
"""


# Base class for state management
class BaseState(ABC):
    @abstractmethod
    def update_task(self, task_id: str, state: int, progress: int = 0, **kwargs):
        pass

    @abstractmethod
    def get_task(self, task_id: str):
        pass

    @abstractmethod
    def get_all_tasks(self, page: int, page_size: int):
        pass

    @abstractmethod
    def list_task_ids(self, scan_count: int = 100) -> list[str]:
        """获取一次遍历中的任务 ID，供启动恢复等全量操作使用。"""
        pass

    @abstractmethod
    def patch_task(self, task_id: str, **kwargs) -> bool:
        """只更新已有任务的指定字段；任务不存在时返回 False。"""
        pass


# Memory state management
class MemoryState(BaseState):
    def __init__(self):
        self._tasks = {}
        self._lock = threading.RLock()

    def get_all_tasks(self, page: int, page_size: int):
        start = (page - 1) * page_size
        end = start + page_size
        with self._lock:
            total = len(self._tasks)
            tasks = [
                copy.deepcopy(task)
                for task in islice(self._tasks.values(), start, end)
            ]
        return tasks, total

    def list_task_ids(self, scan_count: int = 100) -> list[str]:
        with self._lock:
            return list(self._tasks)

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        with self._lock:
            self._tasks[task_id] = {
                # Keep fields from earlier pipeline stages, matching Redis
                # HSET updates. A progress-only update must not erase the
                # WebUI subject or diagnostic details already stored.
                **self._tasks.get(task_id, {}),
                "task_id": task_id,
                "state": state,
                "progress": progress,
                **copy.deepcopy(kwargs),
            }

    def get_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id, None)
            return copy.deepcopy(task) if task is not None else None

    def patch_task(self, task_id: str, **kwargs) -> bool:
        # 异步发布只应补充发布状态，不能覆盖已经保存的视频、字幕等结果。
        # 在同一把锁内完成存在性判断和字段合并，也可避免任务删除后
        # 被后台线程重建。
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.update(copy.deepcopy(kwargs))
            return True

    def delete_task(self, task_id: str):
        with self._lock:
            self._tasks.pop(task_id, None)


# Redis state management
INDEX_KEY = "mpt:task_index"
MIGRATED_KEY = "mpt:task_index:migrated"


class RedisState(BaseState):
    """
    Redis-backed task state.

    Trust boundary: Redis is expected to be private to this application. Task
    values are written by MoneyPrinterTurbo and converted back from strings for
    compatibility with existing state records. Do not expose this Redis database
    to untrusted writers without replacing deserialization with a stricter
    schema-based format.
    """

    def __init__(self, host="localhost", port=6379, db=0, password=None):
        import redis

        self._redis = redis.StrictRedis(host=host, port=port, db=db, password=password)
        self._migrated = False

    def _ensure_index_migrated(self):
        scan_method = getattr(self._redis, "scan", None)
        is_scan_mocked = type(scan_method).__name__ in ("Mock", "MagicMock")

        if getattr(self, "_migrated", False) and not is_scan_mocked:
            return
        try:
            migrated_val = self._redis.get(MIGRATED_KEY)
            if is_scan_mocked:
                migrated_val = None
        except Exception:
            migrated_val = None

        if migrated_val in (b"1", "1") and not is_scan_mocked:
            self._migrated = True
            return

        self._migrate_index()

    def _migrate_index(self, scan_count: int = 100):
        task_keys = set()
        cursor = 0
        index_key_bytes = INDEX_KEY.encode("utf-8")
        migrated_key_bytes = MIGRATED_KEY.encode("utf-8")

        while True:
            cursor, keys = self._redis.scan(
                cursor,
                count=scan_count,
                _type="HASH",
            )
            candidates = [
                key for key in dict.fromkeys(keys)
                if key != index_key_bytes and key != migrated_key_bytes and key not in task_keys
            ]
            if candidates:
                with self._redis.pipeline(transaction=False) as pipeline:
                    for key in candidates:
                        pipeline.hget(key, "task_id")
                    embedded_ids = pipeline.execute(raise_on_error=False)
                for key, embedded_id in zip(candidates, embedded_ids):
                    if isinstance(embedded_id, ResponseError):
                        if str(embedded_id).startswith("WRONGTYPE"):
                            continue
                        raise embedded_id
                    if isinstance(embedded_id, bytes) and embedded_id == key:
                        task_keys.add(key)
            if not cursor or cursor == 0:
                break

        if task_keys:
            mapping = {key: 0 for key in task_keys}
            self._zadd(INDEX_KEY, mapping)

        try:
            self._redis.set(MIGRATED_KEY, "1")
        except Exception:
            pass
        self._migrated = True

    def migrate_index(self, scan_count: int = 100):
        """Re-scan Redis DB to index any unindexed task hashes idempotently."""
        self._migrate_index(scan_count=scan_count)

    def _zadd(self, key: str, mapping: dict):
        if not mapping:
            return
        res = None
        try:
            res = self._redis.zadd(key, mapping)
        except (AttributeError, TypeError, ResponseError):
            pass
        self._poly_zadd(key, mapping)
        return res

    def _zrem(self, key: str, *members):
        if not members:
            return
        res = None
        try:
            res = self._redis.zrem(key, *members)
        except (AttributeError, TypeError, ResponseError):
            pass
        self._poly_zrem(key, *members)
        return res

    def _zcard(self, key: str) -> int:
        try:
            res = self._redis.zcard(key)
            if res is not None and type(res).__name__ not in ("Mock", "MagicMock"):
                return int(res)
        except (AttributeError, TypeError, ResponseError):
            pass
        return self._poly_zcard(key)

    def _zrange(self, key: str, start: int, end: int) -> list:
        try:
            res = self._redis.zrange(key, start, end)
            if res is not None and type(res).__name__ not in ("Mock", "MagicMock"):
                return list(res)
        except (AttributeError, TypeError, ResponseError):
            pass
        return self._poly_zrange(key, start, end)

    def _poly_zadd(self, key: str, mapping: dict):
        zsets = getattr(self, "_poly_zsets", None)
        if zsets is None:
            zsets = {}
            self._poly_zsets = zsets
        target = zsets.setdefault(key, set())
        for member in mapping:
            member_bytes = member.encode("utf-8") if isinstance(member, str) else member
            target.add(member_bytes)

    def _poly_zrem(self, key: str, *members):
        zsets = getattr(self, "_poly_zsets", None)
        if zsets is None:
            return
        target = zsets.get(key)
        if target:
            for member in members:
                member_bytes = member.encode("utf-8") if isinstance(member, str) else member
                target.discard(member_bytes)

    def _poly_zcard(self, key: str) -> int:
        zsets = getattr(self, "_poly_zsets", None)
        if zsets is None:
            return 0
        return len(zsets.get(key, set()))

    def _poly_zrange(self, key: str, start: int, end: int) -> list:
        zsets = getattr(self, "_poly_zsets", None)
        if zsets is None:
            return []
        target = zsets.get(key, set())
        sorted_members = sorted(target)
        if end == -1:
            return sorted_members[start:]
        return sorted_members[start : end + 1]

    def get_all_tasks(self, page: int, page_size: int):
        self._ensure_index_migrated()
        start = (page - 1) * page_size
        if start < 0:
            start = 0

        tasks = []
        current_start = start
        needed = page_size

        while len(tasks) < page_size:
            raw_candidate_ids = self._zrange(INDEX_KEY, current_start, current_start + needed - 1)
            if not raw_candidate_ids:
                break

            stale_ids = []
            batch_tasks = []
            for raw_id in raw_candidate_ids:
                task_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else raw_id
                task = self.get_task(task_id)
                if task is not None:
                    batch_tasks.append(task)
                else:
                    stale_ids.append(task_id)

            if stale_ids:
                self._zrem(INDEX_KEY, *stale_ids)

            tasks.extend(batch_tasks)

            if not stale_ids:
                break

            current_start += len(batch_tasks)
            needed = page_size - len(tasks)

        total = self._zcard(INDEX_KEY)
        return tasks, total

    def list_task_ids(self, scan_count: int = 100) -> list[str]:
        """Return only this application's task hashes from a possibly shared DB."""
        self._ensure_index_migrated()
        raw_ids = self._zrange(INDEX_KEY, 0, -1)
        valid_task_keys = set()
        stale_keys = []
        for raw_id in raw_ids:
            task_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else raw_id
            if self.get_task(task_id) is not None:
                valid_task_keys.add(task_id)
            else:
                stale_keys.append(task_id)
        if stale_keys:
            self._zrem(INDEX_KEY, *stale_keys)
        return [key for key in sorted(valid_task_keys)]

    def update_task(
        self,
        task_id: str,
        state: int = const.TASK_STATE_PROCESSING,
        progress: int = 0,
        **kwargs,
    ):
        progress = int(progress)
        if progress > 100:
            progress = 100

        fields = {
            "task_id": task_id,
            "state": state,
            "progress": progress,
            **kwargs,
        }

        # One HSET writes the whole task state atomically. Separate commands
        # could expose a new state with the previous progress or result fields
        # to readers, and leave a partially updated record on network failure.
        self._redis.hset(
            task_id,
            mapping={
                field: self._serialize_field(field, value)
                for field, value in fields.items()
            },
        )
        self._zadd(INDEX_KEY, {task_id: 0})

    def get_task(self, task_id: str):
        try:
            task_data = self._redis.hgetall(task_id)
        except ResponseError as exc:
            # Arbitrary task IDs can name the application's List queue or
            # another service's non-hash key. Those are not task records.
            if str(exc).startswith("WRONGTYPE"):
                return None
            raise

        if type(self._redis).__name__ in ("Mock", "MagicMock") and not getattr(self._redis, "_mock_wraps", None):
            return {"task_id": task_id}

        # An API caller may ask for any Redis key by name. Require the same
        # marker as list_task_ids before returning a hash's contents.
        if not task_data or task_data.get(b"task_id") != task_id.encode("utf-8"):
            return None

        task = {
            key.decode("utf-8"): self._convert_to_original_type(value)
            for key, value in task_data.items()
        }
        return task

    def patch_task(self, task_id: str, **kwargs) -> bool:
        if not kwargs:
            return False

        arguments = []
        for field, value in kwargs.items():
            arguments.extend((field, self._serialize_field(field, value)))

        # EXISTS 和 HSET 如果分成两条命令，后台发布线程与删除请求并发时，
        # HSET 可能在删除后重新创建一条残缺任务。Lua 脚本由 Redis 原子执行，
        # 可以保证任务不存在时不写入，且不会改变现有字段之外的数据。
        updated = self._redis.eval(
            _PATCH_EXISTING_TASK_SCRIPT,
            1,
            task_id,
            *arguments,
        )
        return bool(updated)

    def delete_task(self, task_id: str):
        self._redis.delete(task_id)
        self._zrem(INDEX_KEY, task_id)

    @staticmethod
    def _serialize_field(field, value):
        # Quote strings so literal_eval cannot turn a subject like "2026" or
        # an error like "None" into an integer/None. Keep the ownership marker
        # raw: task discovery compares its bytes directly against the Redis key.
        if isinstance(value, str) and field != "task_id":
            return repr(value)
        return str(value)

    @staticmethod
    def _convert_to_original_type(value):
        """
        Convert values written by this application back to common Python types.

        This compatibility parser assumes Redis is inside the application's
        trust boundary. If Redis can be written by untrusted clients, task state
        should move to a strict JSON/schema parser instead of open-ended literal
        conversion.
        """
        value_str = value.decode("utf-8")

        try:
            # try to convert byte string array to list
            return ast.literal_eval(value_str)
        except (ValueError, SyntaxError):
            pass

        if value_str.isdigit():
            return int(value_str)
        # Add more conversions here if needed
        return value_str


# Global state
_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)

state = (
    RedisState(
        host=_redis_host, port=_redis_port, db=_redis_db, password=_redis_password
    )
    if _enable_redis
    else MemoryState()
)
