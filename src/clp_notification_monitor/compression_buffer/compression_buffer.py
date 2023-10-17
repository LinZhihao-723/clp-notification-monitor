from datetime import datetime, timedelta
from logging import Logger
from pathlib import Path, PurePath
from threading import Condition, Lock
from typing import Dict, List, Optional, Tuple


class SingleBuffer:
    def __init__(self) -> None:
        self.path_list: List[PurePath] = []
        self.total_buffer_size: int = 0
        self.first_path_timestamp: Optional[datetime] = None

    def clear(self) -> None:
        self.path_list.clear()
        self.total_buffer_size = 0
        self.first_path_timestamp = None


class CompressionBuffer:
    def __init__(
        self,
        logger: Logger,
        max_buffer_size: int,
        min_refresh_period: int,
        group_by_paths_under_prefix: Path,
    ) -> None:
        self._logger: Logger = logger

        self.__max_buffer_size: int = max_buffer_size
        self.__min_refresh_period: timedelta = timedelta(milliseconds=min_refresh_period)

        self.__named_buffers: Dict[str, SingleBuffer] = {}
        self.__ready_buffers: List[str] = []
        self.__group_by_paths_under_prefix: Path = group_by_paths_under_prefix

        # TODO: change the granularity of lock to each buffer
        self.__lock: Lock = Lock()
        self.__populate_buffer_cv: Condition = Condition(self.__lock)

    def append(
        self,
        s3_path: PurePath,
        object_size: int,
        process_timestamp: datetime,
    ) -> None:
        with self.__populate_buffer_cv:
            relative_path: PurePath
            index_name: str
            try:
                relative_path = s3_path.relative_to(self.__group_by_paths_under_prefix)
                index_name = relative_path.parts[0]
            except (KeyError, ValueError):
                # This file does not belong to any named buffer
                index_name = ""

            # Get the correct buffer
            target_buffer: SingleBuffer
            try:
                target_buffer = self.__named_buffers[index_name]
            except KeyError:
                target_buffer = SingleBuffer()
                self.__named_buffers[index_name] = target_buffer

            # Add path to buffer
            target_buffer.total_buffer_size += object_size
            if None is target_buffer.first_path_timestamp:
                target_buffer.first_path_timestamp = process_timestamp
            target_buffer.path_list.append(s3_path)

            # Notify the thread waiting for a compression job
            self.__populate_buffer_cv.notify_all()

    def wait_for_compression_jobs(self) -> None:
        """
        Block the current process until the buffer is populated.
        """
        # If the lock is being acquired due to unprocessed ready-to-comopress
        # buffers, skip waiting to avoid deadlock.
        if len(self.__ready_buffers) > 0:
            return

        with self.__populate_buffer_cv:
            while True:
                # TODO: come up with a more efficient scanning method when
                # a large number of buffers is to be expected
                all_buffers_empty: bool = True
                for buf in self.__named_buffers.values():
                    if None is not buf.first_path_timestamp:
                        all_buffers_empty = False
                        break
                if not all_buffers_empty:
                    break
                self.__populate_buffer_cv.wait()

    def _get_buffers_ready_for_compression(self) -> List[str]:
        ready_buffers: List[str] = []
        for buf_name, buf in self.__named_buffers.items():
            if None is buf.first_path_timestamp:
                continue

            # Job triggered by reaching maximum archive size
            if buf.total_buffer_size > self.__max_buffer_size:
                self._logger.info(
                    f"Buffer {buf_name} ready for compression (max buffer size exceeded). "
                    f"Total buffer size: {buf.total_buffer_size}. "
                    f"Max buffer size: {self.__max_buffer_size}."
                )
                ready_buffers.append(buf_name)
                continue

            # Job triggered by the minimum buffer refresh frequency
            total_elapsed_time: timedelta = datetime.now() - buf.first_path_timestamp
            if total_elapsed_time > self.__min_refresh_period:
                self._logger.info(
                    f"Buffer {buf_name} ready for compression (min refresh period exceeded). "
                    f"Total elapsed time: {total_elapsed_time.total_seconds()} seconds. "
                    f"First chunk timestamp: {buf.first_path_timestamp}."
                )
                ready_buffers.append(buf_name)

        return ready_buffers

    def get_paths_to_compress(self) -> Tuple[List[PurePath], str]:
        """
        :return: The path list of the current buffer if the compression job can
        be fired. Otherwise, returns an empty list.
        Also returns the index name that applies to all the paths in this job.
        """
        buffer_name: str = ""
        if 0 == len(self.__ready_buffers):
            self.__lock.acquire()
            self.__ready_buffers = self._get_buffers_ready_for_compression()
            if 0 == len(self.__ready_buffers):
                self.__lock.release()
                return [], buffer_name

        buffer_name = self.__ready_buffers.pop(0)
        target_buffer: SingleBuffer = self.__named_buffers[buffer_name]

        # Get a deepcopy of the current path list
        path_list_to_return = list(target_buffer.path_list)
        target_buffer.clear()

        # Do not release the lock until all target buffers have been processed.
        # The caller is expected to repeatedly call this function for new jobs.
        if 0 == len(self.__ready_buffers):
            self.__lock.release()

        return path_list_to_return, buffer_name
