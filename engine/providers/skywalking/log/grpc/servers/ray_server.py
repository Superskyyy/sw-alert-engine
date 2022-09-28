#  Copyright 2022 SkyAPM org
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
The multiprocessing server uses SO_REUSEPORT to bind to a port and then forks
This will not work for Windows, we must fallback to asyncio based server on Windows platform
OSX is not tested, but it should probably work TODO: test on OSX
"""

import contextlib
import datetime
import logging
import multiprocessing
import socket
import sys
import time

import grpc
import ray

import secrets_temp
from engine.providers.skywalking.log.grpc.proto.generated import log_exporter_pb2_grpc
from engine.providers.skywalking.log.grpc.servicers.aio_servicer import LogIngestorServicer

ray.init()

_LOGGER = logging.getLogger(__name__)
_ONE_DAY = datetime.timedelta(days=1)
_PROCESS_COUNT = multiprocessing.cpu_count() - 14
print(f'Count of processes {_PROCESS_COUNT}')
_THREAD_CONCURRENCY = _PROCESS_COUNT
from redis import asyncio as aioredis  # noqa


@ray.remote(num_cpus=1)
class GRPCIngestorActor():
    def __init__(self, redis_info, bind_address):
        self.run = None
        self.bind_address = bind_address
        self.redis_info = redis_info
        options = (('grpc.so_reuseport', 1),)
        self.server = grpc.aio.server(options=options)

    async def _run_server(self):
        """
        Starts an Async gRPC server, port binding to more than
        one process will only work on Unix systems.
        On unsupported systems, this will fall back to a single
        actor server.
        """
        _LOGGER.info('Starting new server.')

        self.redis_conn = await aioredis.from_url(
            f'redis://{self.redis_info["REDIS_HOSTNAME"]}:{self.redis_info["REDIS_PORT"]}',
            retry_on_timeout=True, username=self.redis_info['username'],
            password=self.redis_info['password'])

        log_exporter_pb2_grpc.add_LogExportServiceServicer_to_server(
            LogIngestorServicer(redis_conn=self.redis_conn),
            self.server,
        )
        self.server.add_insecure_port(self.bind_address)
        await self.server.start()
        await self.server.wait_for_termination()

    async def start(self):
        await self._run_server()

    def stop(self):
        self.run = False

    def destroy(self):
        self.run = False

    def _wait_forever(self):
        try:
            while True:
                time.sleep(_ONE_DAY.total_seconds())
        except KeyboardInterrupt:
            self.server.stop(None)


@contextlib.contextmanager
def _reserve_port():
    """Find and reserve a port for all subprocesses to use."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    supported = True
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError as e:
        supported = False
        _LOGGER.info('SO_REUSEPORT not supported on this platform, falling back to single process')
        _LOGGER.info(e)

    sock.bind(('', 50051))  # 0 means to select an arbitrary unused port
    try:
        yield sock.getsockname()[1], supported
    finally:
        sock.close()


def main():
    with _reserve_port() as (port, success):
        _LOGGER.info(f'Port {port}, success {success}')
        bind_address = 'localhost:{}'.format(port)
        _LOGGER.info("Binding to '%s'", bind_address)
        sys.stdout.flush()
        # NOTE: It is imperative that the worker subprocesses be forked before
        # any gRPC servers start up. See
        # https://github.com/grpc/grpc/issues/16001 for more details.
        if not success:
            grpc_ingestors = [GRPCIngestorActor.remote(secrets_temp.redis_info, bind_address) for _ in range(1)]
        else:
            grpc_ingestors = [GRPCIngestorActor.remote(secrets_temp.redis_info, bind_address) for _ in range(2)]

        # make count of actor configurable
        try:
            refs = [grpc_ingestor.start.remote() for grpc_ingestor in grpc_ingestors]
            ray.get(refs)
        except KeyboardInterrupt:
            for grpc_ingestor in grpc_ingestors:
                grpc_ingestor.stop.remote()
        finally:
            for grpc_ingestor in grpc_ingestors:
                grpc_ingestor.destroy.remote()


if __name__ == '__main__':
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[PID %(process)d] %(message)s')
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    main()