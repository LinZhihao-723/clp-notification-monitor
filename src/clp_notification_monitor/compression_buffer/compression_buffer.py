import time
from datetime import datetime, timedelta
from logging import Logger
from math import floor
from pathlib import PurePath
from threading import Condition, Lock
from typing import Dict, List, Optional, Tuple

from pymongo.collection import Collection


class CompressionBuffer:
    def __init__(
        self,
        logger: Logger,
        jobs_collection: Collection,  # type: ignore
        s3_endpoint: str,
        max_buffer_size: int,
        min_refresh_period: int,
        mnt_prefix: str,
    ):
        self._logger: Logger = logger
        self._jobs_collection: Collection = jobs_collection  # type: ignore
        self._endpoint_url: str = s3_endpoint
        self._mnt_prefix: PurePath = PurePath(mnt_prefix)
        self._path_prefixes: List[Dict[str, str]] = []

        self.__path_list: List[Tuple[str,PurePath]] = []
        self.__total_buffer_size: int = 0
        self.__first_path_timestamp: Optional[datetime] = None

        self.__max_buffer_size: int = max_buffer_size
        self.__min_refresh_period: timedelta = timedelta(milliseconds=min_refresh_period)

        self.__lock: Lock = Lock()
        self.__populate_buffer_cv: Condition = Condition(self.__lock)

    def clear_buffer(self) -> None:
        self.__path_list = []
        self.__total_buffer_size = 0
        self.__first_path_timestamp = None

    def append(
        self,
        s3_bucket: str,
        s3_path: PurePath,
        object_size: int,
        process_timestamp: datetime,
    ) -> None:
        with self.__populate_buffer_cv:
            self.__total_buffer_size += object_size
            if self.__first_path_timestamp is None:
                self.__first_path_timestamp = process_timestamp
            self.__path_list.append((s3_bucket, s3_path))
            self.__populate_buffer_cv.notify_all()

    def wait_for_compression_jobs(self) -> None:
        """
        Block the current process until the buffer is populated.
        """
        with self.__populate_buffer_cv:
            while None is self.__first_path_timestamp:
                self.__populate_buffer_cv.wait()

    def ready_for_compression(self) -> bool:
        if None is self.__first_path_timestamp:
            return False
        if self.__total_buffer_size > self.__max_buffer_size:
            self._logger.info(
                "Ready for compression (max buffer size exceeded). "
                f"Total buffer size: {self.__total_buffer_size}. "
                f"Max buffer size: {self.__max_buffer_size}."
            )
            return True
        total_elapsed_time = datetime.now() - self.__first_path_timestamp
        if total_elapsed_time > self.__min_refresh_period:
            self._logger.info(
                "Ready for compression (min refresh period exceeded). "
                f"Total elapsed time: {total_elapsed_time.total_seconds()} seconds. "
                f"First chunk timestamp: {self.__first_path_timestamp}."
            )
            return True
        return False

    def try_compress(self) -> bool:
        """
        Attempts to send a compression job.
        :return: True on success.
        """
        if not self.ready_for_compression():
            return False

        path_prefixes: List[Dict[str, str]]
        with self.__lock:
            path_prefixes = [
                self.generate_compression_entry_from_s3_path_prefix(s3_path)
                for s3_bucket, s3_path in self.__path_list
            ]
            self.clear_buffer()

        new_job = {
            "input_type": "s3",
            "input_config": {
                "access_key_id": "unused",
                "secret_access_key": "undefined",
                "buckets": path_prefixes,
            },
            "output_config": {},
            "status": "pending",
            "submission_timestamp": floor(time.time() * 1000),
        }
        self._jobs_collection.insert_one(new_job)
        return True

    def try_compress_from_fs(self) -> bool:
        """
        Attempts to send a compression job with fs mapping.
        :return: True on success.
        """
        if not self.ready_for_compression():
            return False

        compression_path: List[Tuple(str, str)]
        with self.__lock:
            compression_path = [
                (s3_bucket, str(s3_path)) for s3_bucket, s3_path in self.__path_list
            ]
            self.clear_buffer()

        new_job = {
            "input_type": "fs",
            "input_config": {
                "path_prefix_to_remove": str(self._mnt_prefix),
                "paths": compression_path,
            },
            "output_config": {},
            "status": "pending",
            "submission_timestamp": floor(time.time() * 1000),
        }
        self._jobs_collection.insert_one(new_job)
        self._logger.info("Submitted the job to db")
        return True

    def generate_compression_entry_from_s3_path_prefix(self, s3_path: PurePath) -> Dict[str, str]:
        return {
            "endpoint_url": self._endpoint_url,
            "s3_path_prefix": str(s3_path),
            # Remove the bucket name from the path prefixes while displaying the
            # mounted compressed results on the WebUI. To construct the prefix
            # to remove, join the leading slash with the bucket name.
            "s3_path_prefix_to_remove_from_mount": "".join(s3_path.parts[0:1]),
        }
