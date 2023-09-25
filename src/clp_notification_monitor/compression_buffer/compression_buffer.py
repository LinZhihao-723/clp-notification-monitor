from datetime import datetime, timedelta
from logging import Logger
from pathlib import PurePath
from threading import Condition, Lock
from typing import List, Optional


class CompressionBuffer:
    def __init__(
        self,
        logger: Logger,
        max_buffer_size: int,
        min_refresh_period: int,
    ):
        self._logger: Logger = logger

        self.__path_list: List[PurePath] = []
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
        s3_path: PurePath,
        object_size: int,
        process_timestamp: datetime,
    ) -> None:
        with self.__populate_buffer_cv:
            self.__total_buffer_size += object_size
            if self.__first_path_timestamp is None:
                self.__first_path_timestamp = process_timestamp
            self.__path_list.append(s3_path)
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

    def get_paths_to_compress(self) -> List[PurePath]:
        """
        :return: The path list of the current buffer if the compression job can
        be fired. Otherwise, returns an empty list.
        """
        if not self.ready_for_compression():
            return []

        with self.__lock:
            # Get a deepcopy of the current path list
            path_list_to_return = list(self.__path_list)
            self.clear_buffer()
        return path_list_to_return
