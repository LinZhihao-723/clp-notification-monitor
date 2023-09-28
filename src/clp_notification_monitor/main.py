import argparse
import logging
import logging.handlers
import pathlib
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from typing import Generator, List, Optional

import pymongo

from clp_notification_monitor.compression_buffer.compression_buffer import CompressionBuffer
from clp_notification_monitor.seaweedfs_monitor.notification_message import S3NotificationMessage
from clp_notification_monitor.seaweedfs_monitor.seaweedfs_grpc_client import SeaweedFSClient

"""
Global logger
"""
logger: logging.Logger


def logger_init(log_file_path_str: Optional[str], log_level: int) -> None:
    """
    Initializes the global logger with the given log level.

    :param log_file_path_str: Path to store the log files. If None is given,
        logs will be written into stdout.
    :param log_level: Target log level.
    """
    global logger
    logger_handler: Optional[logging.Handler] = None
    logging_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    if log_file_path_str is not None:
        log_file_path: pathlib.Path = pathlib.Path(log_file_path_str).resolve()
        if log_file_path.is_dir():
            log_file_path /= "clp_notification_monitor.log"
        if log_file_path.exists():
            log_file_path.unlink()
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        logger_handler = logging.handlers.RotatingFileHandler(
            filename=log_file_path, maxBytes=1024 * 1024 * 8, backupCount=7
        )
        logger_handler.setFormatter(logging_formatter)
        logger_handler.setLevel(log_level)

    logger = logging.getLogger("clp_notification_monitor")
    logger.setLevel(log_level)
    if None is not logger_handler:
        logger.addHandler(logger_handler)
    stream_handler: logging.Handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging_formatter)
    stream_handler.setLevel(log_level)
    logger.addHandler(stream_handler)


def submit_compression_jobs_thread_entry(
    compression_buffer: CompressionBuffer, max_polling_period: int, exit: Event
) -> None:
    """
    The entry of the thread to submit compression jobs from the compression
    buffer.

    :param compression_buffer: Compression buffer.
    :param max_polling_period: The maximum polling period in seconds.
    :param exit: A fag to indicate the main thread to exit on failures.
    """
    try:
        while True:
            compression_buffer.wait_for_compression_jobs()
            sleep_time: int = 1
            while False is compression_buffer.try_compress_from_fs():
                time.sleep(sleep_time)
                sleep_time = min(sleep_time * 2, max_polling_period)
    except Exception as e:
        logger.error(f"Error on compression buffer: {e}")
        exit.set()


def filer_ingestion_listener_thread_entry(
    notification_generator: Generator[S3NotificationMessage, None, None],
    compression_buffer: CompressionBuffer,
    exit: Event,
) -> None:
    """
    The entry of the thread to listen from the filer notification generator.

    :param notification_generator: The notification generator that returns all
    the ingestion events.
    :param compression_buffer: Compression buffer to add the ingestion.
    :param exit: A fag to indicate the main thread to exit on failures.
    """
    try:
        for notification in notification_generator:
            logger.info(f"Ingestion: {notification.s3_full_path}")
            logger.info(f"Bucket: {notification.s3_bucket}")
            compression_buffer.append(
                notification.s3_bucket,
                notification.s3_full_path, 
                notification.file_size, 
                datetime.now()
            )
    except Exception as e:
        logger.error(f"Error on Filer notification listener: {e}")
        exit.set()


def main(argv: List[str]) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="CLP SeaweedFS Notification Monitor"
    )
    parser.add_argument(
        "--seaweed-filer-endpoint",
        required=True,
        help="The endpoint of the SeaweedFS Filer server.",
    )
    parser.add_argument(
        "--seaweed-s3-endpoint",
        required=True,
        help="The endpoint of the SeaweedFS S3 server.",
    )
    parser.add_argument(
        "--filer-notification-path-prefix",
        required=True,
        help="The path prefix that will trigger the filer notification.",
    )
    parser.add_argument(
        "--seaweed-mnt-prefix",
        required=True,
        help="The path prefix that the seaweed-fs is mount on",
    )
    parser.add_argument("--db-uri", required=True, help="Regional compression DB uri")
    args: argparse.Namespace = parser.parse_args(argv[1:])

    logger_init("./logs/notification.log", logging.INFO)
    endpoint: str = args.seaweed_filer_endpoint
    s3_endpoint: str = args.seaweed_s3_endpoint
    db_uri: str = args.db_uri
    mnt_prefix: str = args.seaweed_mnt_prefix
    filer_notification_path_prefix: Path = Path(args.filer_notification_path_prefix)

    seaweedfs_client: SeaweedFSClient
    try:
        seaweedfs_client = SeaweedFSClient("clp—testing", endpoint, logger)
    except Exception as e:
        logger.error(f"Failed to initiate seaweedfs client: {e}")
        return -1

    mongodb: pymongo.mongo_client.MongoClient  # type: ignore
    archive_db: pymongo.database.Database  # type: ignore
    jobs_collection: pymongo.collection.Collection  # type: ignore
    logger.info("Start initiating MongoDB client.")
    try:
        mongodb = pymongo.mongo_client.MongoClient(db_uri)
        archive_db = mongodb.get_default_database()
        jobs_collection = archive_db["cjobs"]
    except Exception as e:
        logger.error(f"Failed to initiate MongoDB: {e}")
        seaweedfs_client.close()
        return -1
    logger.info("MongoDB client successfully initiated.")

    compression_buffer: CompressionBuffer
    try:
        compression_buffer = CompressionBuffer(
            logger=logger,
            jobs_collection=jobs_collection,
            s3_endpoint=s3_endpoint,
            max_buffer_size=16 * 1024 * 1024,  # 16MB
            min_refresh_period=5 * 1000,  # 5 seconds
            mnt_prefix=mnt_prefix,
        )
    except Exception as e:
        logger.error(f"Failed to initiate Compression Buffer: {e}")
        seaweedfs_client.close()
        return -1

    exit_event: Event = Event()
    job_submission_thread: Thread
    max_polling_period: int = 180  # 3 min
    try:
        job_submission_thread = Thread(
            target=submit_compression_jobs_thread_entry,
            args=(compression_buffer, max_polling_period, exit_event),
        )
        job_submission_thread.daemon = True
        job_submission_thread.start()
    except Exception as e:
        logger.error(f"Failed to initiate Job Submission Thread: {e}")
        seaweedfs_client.close()
        return -1

    filer_listener_thread: Thread
    try:
        generator: Generator[S3NotificationMessage, None, None] = (
            seaweedfs_client.s3_file_ingestion_listener(
                filer_notification_path_prefix, since_ns=time.time_ns(), store_fid=False
            )
        )
        filer_listener_thread = Thread(
            target=filer_ingestion_listener_thread_entry,
            args=(generator, compression_buffer, exit_event),
        )
        filer_listener_thread.daemon = True
        filer_listener_thread.start()
    except Exception as e:
        logger.error(f"Failed to initiate Filer listener thread: {e}")
        seaweedfs_client.close()
        return -1

    while False is exit_event.is_set():
        time.sleep(60)
    seaweedfs_client.close()
    return 0
