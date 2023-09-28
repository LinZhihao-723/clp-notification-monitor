import argparse
import logging
import logging.handlers
import math
import pathlib
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, Generator, List, Optional

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
    compression_buffer: CompressionBuffer,
    max_polling_period: int,
    jobs_collection: pymongo.collection.Collection,  # type: ignore
    input_type: str,
    seaweed_s3_endpoint_url: str,
    filer_notification_path_prefix: Path,
    seaweed_mnt_prefix: Path,
    exit: Event,
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
            while True:
                paths_to_compress = compression_buffer.get_paths_to_compress()
                if 0 == len(paths_to_compress):
                    time.sleep(sleep_time)
                    sleep_time = min(sleep_time * 2, max_polling_period)
                    continue

                new_job_entry: Dict[str, Any] = {
                    "input_type": input_type,
                    "output_config": {},
                    "status": "pending",
                    "submission_timestamp": math.floor(time.time() * 1000),
                }
                if "s3" == input_type:
                    input_buckets: List[Dict[str, str]] = []
                    for full_s3_path in paths_to_compress:
                        input_buckets.append(
                            {
                                "endpoint_url": seaweed_s3_endpoint_url,
                                "s3_path_prefix": str(full_s3_path),
                                # Remove the bucket name from the path prefixes
                                "s3_path_prefix_to_remove_from_mount": "".join(
                                    full_s3_path.parts[:2]
                                ),
                            }
                        )
                    new_job_entry["input_config"] = {
                        "access_key_id": "",  # not used
                        "secret_access_key": "",  # not used
                        "buckets": input_buckets,
                    }
                else:
                    fs_compression_paths = []
                    # Remove the leading slash from mounted path for path
                    # concatenation
                    for full_s3_path in paths_to_compress:
                        mounted_path_str = seaweed_mnt_prefix + str(full_s3_path)[1:]
                        fs_compression_paths.append(mounted_path_str)
                    new_job_entry["input_config"] = {
                        "paths": fs_compression_paths,
                        "path_prefix_to_remove": (
                            seaweed_mnt_prefix + str(filer_notification_path_prefix)[1:]
                        ),
                    }
                jobs_collection.insert_one(new_job_entry)
                logger.info("Submitted job to compression database.")
                break
    except Exception as e:
        logger.error(f"Error on compression buffer: {e}")
    finally:
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
            compression_buffer.append(
                notification.s3_full_path, notification.file_size, datetime.now()
            )
    except Exception as e:
        logger.error(f"Error on Filer notification listener: {e}")
    finally:
        exit.set()


def main(argv: List[str]) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="CLP SeaweedFS Notification Monitor"
    )
    parser.add_argument(
        "--seaweed-filer-endpoint",
        required=True,
        help="The endpoint of the SeaweedFS Filer server. Used for filer notifications.",
    )
    parser.add_argument(
        "--seaweed-s3-endpoint-url",
        required=True,
        help="The endpoint of the SeaweedFS S3 server. Used for filer access.",
    )
    parser.add_argument(
        "--filer-notification-path-prefix",
        required=True,
        help="The path prefix to monitor the filer notifications.",
    )
    parser.add_argument("--db-uri", required=True, help="Regional compression DB uri")

    # FS ingestion option
    input_type_parser = parser.add_subparsers(dest="input_type")
    input_type_parser.required = True

    input_type_parser.add_parser("s3")

    fs_input_parser = input_type_parser.add_parser("fs")
    fs_input_parser.add_argument(
        "--seaweed-mnt-prefix",
        help="The path prefix that the seaweed-fs is mount on",
    )

    args: argparse.Namespace = parser.parse_args(argv[1:])

    logger_init("./logs/notification.log", logging.INFO)
    seaweed_filer_endpoint: str = args.seaweed_filer_endpoint
    seaweed_s3_endpoint_url: str = args.seaweed_s3_endpoint_url
    filer_notification_path_prefix: Path = Path(args.filer_notification_path_prefix)
    db_uri: str = args.db_uri
    input_type: str = args.input_type

    if not filer_notification_path_prefix.is_absolute():
        parser.error("--filer-notification-path-prefix must be absolute.")

    seaweed_mnt_prefix: Path = Path("/")
    if "fs" == input_type:
        seaweed_mnt_prefix: Path = Path(args.seaweed_mnt_prefix)
        if not seaweed_mnt_prefix.is_absolute():
            parser.error("--seaweed-mnt-prefix must be absolute.")

    seaweedfs_client: SeaweedFSClient
    try:
        seaweedfs_client = SeaweedFSClient("clp—user", seaweed_filer_endpoint, logger)
    except Exception as e:
        logger.error(f"Failed to initiate seaweedfs client: {e}")
        return -1

    db_client: pymongo.mongo_client.MongoClient  # type: ignore
    archive_db: pymongo.database.Database  # type: ignore
    jobs_collection: pymongo.collection.Collection  # type: ignore
    logger.info("Start initiating MongoDB client.")
    try:
        db_client = pymongo.mongo_client.MongoClient(db_uri)
        archive_db = db_client.get_default_database()
        jobs_collection = archive_db["cjobs"]
    except Exception as e:
        logger.error(f"Failed to initiate MongoDB: {e}")
        seaweedfs_client.close()
        db_client.close()
        return -1
    logger.info("MongoDB client successfully initiated.")

    compression_buffer: CompressionBuffer
    try:
        compression_buffer = CompressionBuffer(
            logger=logger,
            max_buffer_size=16 * 1024 * 1024,  # 16MB
            min_refresh_period=5 * 1000,  # 5 seconds
        )
    except Exception as e:
        logger.error(f"Failed to initiate Compression Buffer: {e}")
        seaweedfs_client.close()
        db_client.close()
        return -1

    exit_event: Event = Event()
    job_submission_thread: Thread
    max_polling_period: int = 180  # 3 min
    try:
        job_submission_thread = Thread(
            target=submit_compression_jobs_thread_entry,
            args=(
                compression_buffer,
                max_polling_period,
                jobs_collection,
                input_type,
                seaweed_s3_endpoint_url,
                filer_notification_path_prefix,
                seaweed_mnt_prefix,
                exit_event,
            ),
        )
        job_submission_thread.daemon = True
        job_submission_thread.start()
    except Exception as e:
        logger.error(f"Failed to initiate Job Submission Thread: {e}")
        seaweedfs_client.close()
        db_client.close()
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
        db_client.close()
        return -1

    while False is exit_event.is_set():
        time.sleep(60)
    seaweedfs_client.close()
    db_client.close()
    return 0
