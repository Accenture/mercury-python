#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2022 Accenture Technology
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import io
import uuid
import os
import time
import asyncio
import aiohttp
import msgpack

from mercury.system.models import EventEnvelope
from mercury.system.utility import Utility
from mercury.system.cache import SimpleCache
from mercury.system.singleton import Singleton


@Singleton
class NetworkConnector:

    INCOMING_WS_PATH = "ws.incoming"
    OUTGOING_WS_PATH = "ws.outgoing"
    SYSTEM_ALERT = "system.alerts"
    SERVER_CONFIG = "system.config"
    MAX_PAYLOAD = "max.payload"
    TRACE_AGGREGATION = "trace.aggregation"
    DISTRIBUTED_TRACING = "distributed.tracing"
    CONNECTOR_LIFECYCLE = 'cloud.connector.lifecycle'
    # payload segmentation reserved tags (from v1.13.0 onwards)
    MSG_ID = '_id_'
    COUNT = '_blk_'
    TOTAL = '_max_'

    def __init__(self, platform, distributed_trace, loop, url_list, origin):
        self.platform = platform
        self._subscription = list()
        self._distributed_trace = distributed_trace
        self._loop = loop
        self.log = platform.log
        self.config = platform.config
        self.normal = True
        self.started = False
        self.ready = False
        self.ws = None
        self.close_code = 1000
        self.close_message = 'OK'
        self.last_active = time.time()
        self.max_ws_payload = 32768
        self.util = Utility()
        self.urls = self.util.multi_split(url_list, ', ')
        self.next_url = 1
        self.origin = origin
        self.cache = SimpleCache(loop, self.log, timeout_seconds=30)
        self.api_key = self._get_api_key()

    def _get_api_key(self):
        api_key_env_var = self.config.get_property('language.pack.key', default_value='LANGUAGE_PACK_KEY')
        if api_key_env_var in os.environ:
            self.log.info(f'Found API key in environment variable {api_key_env_var}')
            return os.environ[api_key_env_var]
        # check temp file system because API key not in environment
        temp_dir = '/tmp/config'
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
            self.log.info(f'Folder {temp_dir} created')
        api_key_file = temp_dir+"/lang-api-key.txt"
        if os.path.exists(api_key_file):
            with open(api_key_file) as f:
                self.log.info(f'Reading language API key from {api_key_file}')
                return f.read().strip()
        else:
            with open(api_key_file, 'w') as f:
                self.log.info(f'Generating new language API key in {api_key_file}')
                value = ''.join(str(uuid.uuid4()).split('-'))
                f.write(value + '\n')
                return value

    def _get_next_url(self):
        # index starts from 1
        return self.urls[self.next_url - 1]

    def _skip_url(self):
        self.next_url += 1
        if self.next_url > len(self.urls):
            self.next_url = 1

    def send_keep_alive(self):
        message = "Keep-Alive "+self.util.get_iso_8601(time.time(), show_ms=True)
        envelope = EventEnvelope()
        envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'text').set_body(message)
        self.platform.send_event(envelope)

    def send_payload(self, data: dict):
        if 'type' in data and data['type'] == 'event' and 'event' in data:
            evt = data['event']
            payload = msgpack.packb(evt, use_bin_type=True)
            payload_len = len(payload)
            if payload_len > self.max_ws_payload:
                msg_id = evt['id']
                total = int(payload_len / self.max_ws_payload)
                if payload_len > total:
                    total += 1
                buffer = io.BytesIO(payload)
                count = 0
                for i in range(total):
                    count += 1
                    block = EventEnvelope()
                    block.set_header(self.MSG_ID, msg_id)
                    block.set_header(self.COUNT, count)
                    block.set_header(self.TOTAL, total)
                    block.set_body(buffer.read(self.max_ws_payload))
                    block_map = dict()
                    block_map['type'] = 'block'
                    block_map['block'] = block.to_map()
                    block_payload = msgpack.packb(block_map, use_bin_type=True)
                    envelope = EventEnvelope()
                    envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'bytes').set_body(block_payload)
                    self.platform.send_event(envelope)
            else:
                relay_map = dict()
                relay_map['type'] = 'event'
                relay_map['event'] = payload
                envelope = EventEnvelope()
                envelope_payload = msgpack.packb(relay_map, use_bin_type=True)
                envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'bytes').set_body(envelope_payload)
                self.platform.send_event(envelope)
        else:
            envelope = EventEnvelope()
            envelope_payload = msgpack.packb(data, use_bin_type=True)
            envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'bytes').set_body(envelope_payload)
            self.platform.send_event(envelope)

    def _get_server_config(self, headers: dict, body: any):
        if 'type' in headers:
            # at this point, login is successful
            if headers['type'] == 'system.config' and isinstance(body, dict):
                if self.MAX_PAYLOAD in body:
                    self.max_ws_payload = body[self.MAX_PAYLOAD]
                    self.log.info('Authenticated')
                    self._send_life_cycle_event({'type': 'authenticated'})
                    self.log.info(f'Automatic payload segmentation at {format(self.max_ws_payload, ",d")} bytes')
                if self.TRACE_AGGREGATION in body:
                    self.platform.set_trace_support(body[self.TRACE_AGGREGATION])
                # advertise public routes to language connector
                for r in self.platform.get_routes('public'):
                    self.send_payload({'type': 'add', 'route': r})
                # tell server that I am ready
                self.send_payload({'type': 'ready'})
            # server acknowledges my ready signal
            if headers['type'] == 'ready':
                self.ready = True
                self.log.info('Ready')
                self._send_life_cycle_event({'type': 'ready'})

    def subscribe_life_cycle(self, callback: str):
        if not isinstance(callback, str):
            raise ValueError('callback route name must be str')
        if callback not in self._subscription:
            self._subscription.append(callback)

    def unsubscribe_life_cycle(self, callback: str):
        if not isinstance(callback, str):
            raise ValueError('callback route name must be str')
        if callback in self._subscription:
            self._subscription.remove(callback)

    def _send_life_cycle_event(self, headers: dict):
        event = EventEnvelope()
        event.set_to(self.CONNECTOR_LIFECYCLE).set_headers(headers)
        self.platform.send_event(event)

    def _life_cycle(self, headers: dict, body: any):
        for subscriber in self._subscription:
            try:
                event = EventEnvelope()
                event.set_to(subscriber).set_headers(headers).set_body(body)
                self.platform.send_event(event)
            except ValueError as e:
                self.log.warn(f'Unable to relay life cycle event {headers} to {subscriber} - {e}')

    def _alert(self, headers: dict, body: any):
        if 'status' in headers:
            if headers['status'] == '200':
                self.log.info(str(body))
            else:
                self.log.warn(str(body)+", status="+headers['status'])

    def _incoming(self, headers: dict, body: any):
        """
        This function handles incoming messages from the websocket connection with the Mercury language connector.
        It must be invoked using events. It should not be called directly to guarantee proper event sequencing.

        Args:
            headers: type is open, close, text or bytes
            body: string or bytes

        Returns: None

        """
        if self.ws and 'type' in headers:
            if headers['type'] == 'open':
                self.ready = False
                self.log.info("Login to language connector")
                self.send_payload({'type': 'login', 'api_key': self.api_key})
                self._send_life_cycle_event(headers)
            if headers['type'] == 'close':
                self.ready = False
                self.log.info("Closed")
                self._send_life_cycle_event(headers)
            if headers['type'] == 'text':
                self.log.debug(body)
            if headers['type'] == 'bytes':
                event = msgpack.unpackb(body, raw=False)
                if 'type' in event:
                    event_type = event['type']
                    if event_type == 'block' and 'block' in event:
                        envelope = EventEnvelope()
                        inner_event = envelope.from_map(event['block'])
                        inner_headers = inner_event.get_headers()
                        if self.MSG_ID in inner_headers and self.COUNT in inner_headers and self.TOTAL in inner_headers:
                            msg_id = inner_headers[self.MSG_ID]
                            msg_count = inner_headers[self.COUNT]
                            msg_total = inner_headers[self.TOTAL]
                            data = inner_event.get_body()
                            if isinstance(data, bytes):
                                buffer = self.cache.get(msg_id)
                                if buffer is None:
                                    buffer = io.BytesIO()
                                buffer.write(data)
                                if msg_count == msg_total:
                                    self.cache.remove(msg_id)
                                    # reconstruct event for processing
                                    buffer.seek(0)
                                    envelope = EventEnvelope()
                                    unpacked = msgpack.unpackb(buffer.read(), raw=False)
                                    restored = envelope.from_map(unpacked)
                                    target = restored.get_to()
                                    if self.platform.has_route(target):
                                        self.platform.send_event(restored)
                                    else:
                                        self.log.warn(f'Incoming event dropped because {target} not found')
                                else:
                                    self.cache.put(msg_id, buffer)
                    if event_type == 'event' and 'event' in event:
                        unpacked = msgpack.unpackb(event['event'], raw=False)
                        envelope = EventEnvelope()
                        inner_event = envelope.from_map(unpacked)
                        if self.platform.has_route(inner_event.get_to()):
                            self.platform.send_event(inner_event)
                        else:
                            self.log.warn(f'Incoming event dropped because {inner_event.get_to()} not found')

    def _outgoing(self, headers: dict, body: any):
        """
        This function handles sending outgoing messages to the websocket connection with the Mercury language connector.
        It must be invoked using events. It should not be called directly to guarantee proper event sequencing.

        Args:
            headers: type is close, text or bytes
            body: string or bytes

        Returns: None

        """
        if 'type' in headers:
            if headers['type'] == 'close':
                code = 1000 if 'code' not in headers else headers['code']
                reason = 'OK' if 'reason' not in headers else headers['reason']
                self.close_connection(code, reason)
            if headers['type'] == 'text':
                self._send_text(body)
            if headers['type'] == 'bytes':
                self._send_bytes(body)

    def _send_text(self, body: str):
        def send(data: str):
            async def async_send(d: str):
                await self.ws.send_str(d)
            self._loop.create_task(async_send(data))
        if self.is_connected():
            self._loop.call_soon_threadsafe(send, body)

    def _send_bytes(self, body: bytes):
        def send(data: bytes):
            async def async_send(d: bytes):
                await self.ws.send_bytes(d)
            self._loop.create_task(async_send(data))
        if self.is_connected():
            self._loop.call_soon_threadsafe(send, body)

    def is_connected(self):
        return self.started and self.ws

    def is_ready(self):
        return self.is_connected() and self.ready

    def start_connection(self):
        async def worker():
            while self.normal:
                await self._loop.create_task(self.connection_handler(self._get_next_url()))
                # check again because the handler may have run for a while
                if self.normal:
                    # retry connection in 5 seconds
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        if not self.normal:
                            break
                else:
                    break
        if not self.started:
            self.started = True
            self.platform.register(self.DISTRIBUTED_TRACING, self._distributed_trace.logger, 1, is_private=True)
            self.platform.register(self.INCOMING_WS_PATH, self._incoming, 1, is_private=True)
            self.platform.register(self.OUTGOING_WS_PATH, self._outgoing, 1, is_private=True)
            self.platform.register(self.SYSTEM_ALERT, self._alert, 1, is_private=True)
            self.platform.register(self.SERVER_CONFIG, self._get_server_config, 1, is_private=True)
            self.platform.register(self.CONNECTOR_LIFECYCLE, self._life_cycle, 1, is_private=True)
            self._loop.create_task(worker())

    def close_connection(self, code, reason, stop_engine=False):
        async def async_close(rc, msg):
            if self.is_connected():
                # this only send a "closing signal" to the handler - it does not actually close the connection.
                self.close_code = rc
                self.close_message = msg
                await self.ws.close()

        def closing(rc, msg):
            self._loop.create_task(async_close(rc, msg))

        if stop_engine:
            self.normal = False
            self.cache.stop()
        self._loop.call_soon_threadsafe(closing, code, reason)

    async def connection_handler(self, url):
        try:
            async with aiohttp.ClientSession(loop=self._loop, timeout=aiohttp.ClientTimeout(total=10)) as session:
                full_path = f'{url}/{self.origin}'
                self.ws = await session.ws_connect(full_path)
                envelope = EventEnvelope()
                envelope.set_to(self.INCOMING_WS_PATH).set_header('type', 'open').set_header('url', full_path)
                self.platform.send_event(envelope)
                self.log.info(f'Connected to {full_path}')
                closed = False
                self.last_active = time.time()

                while self.normal:
                    try:
                        msg = await self.ws.receive(timeout=1)
                    except asyncio.TimeoutError:
                        if not self.normal:
                            break
                        else:
                            # idle - send keep-alive
                            now = time.time()
                            if self.is_connected() and now - self.last_active > 30:
                                self.last_active = now
                                self.send_keep_alive()
                            continue

                    # receive incoming event
                    self.last_active = time.time()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if self.platform.has_route(self.INCOMING_WS_PATH):
                            envelope = EventEnvelope()
                            envelope.set_to(self.INCOMING_WS_PATH).set_header('type', 'text').set_body(msg.data)
                            self.platform.send_event(envelope)
                        else:
                            break
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        if self.platform.has_route(self.INCOMING_WS_PATH):
                            envelope = EventEnvelope()
                            envelope.set_to(self.INCOMING_WS_PATH).set_header('type', 'bytes').set_body(msg.data)
                            self.platform.send_event(envelope)
                        else:
                            break
                    else:
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            self.log.error('Unexpected connection error')
                        if msg.type == aiohttp.WSMsgType.CLOSING:
                            # closing signal received - close the connection now
                            self.log.info(f'Disconnected, status={self.close_code}, message={self.close_message}')
                            await self.ws.close(code=self.close_code, message=bytes(self.close_message, 'utf-8'))
                            if self.platform.has_route(self.INCOMING_WS_PATH):
                                envelope = EventEnvelope()
                                envelope.set_to(self.INCOMING_WS_PATH).set_body(self.close_message)\
                                        .set_header('type', 'close').set_header('status', self.close_code)
                                self.platform.send_event(envelope)
                            closed = True
                        if msg.type == aiohttp.WSMsgType.CLOSE or msg.type == aiohttp.WSMsgType.CLOSED:
                            self.close_code = 1001 if msg.data is None else msg.data
                            self.close_message = 'OK' if msg.extra is None else str(msg.extra)
                            self.log.info(f'Disconnected, status={self.close_code}, message={self.close_message}')
                            if self.platform.has_route(self.INCOMING_WS_PATH):
                                envelope = EventEnvelope()
                                envelope.set_to(self.INCOMING_WS_PATH).set_header('message', self.close_message) \
                                        .set_header('type', 'close').set_header('status', self.close_code)
                                self.platform.send_event(envelope)
                            closed = True
                        break
                if not closed:
                    await self.ws.close(code=1000, message=b'OK')
                    self.ws = None
                    if self.platform.has_route(self.INCOMING_WS_PATH):
                        envelope = EventEnvelope()
                        envelope.set_to(self.INCOMING_WS_PATH).set_body('OK')\
                            .set_header('type', 'close').set_header('status', 1000)
                        self.platform.send_event(envelope)

        except aiohttp.ClientConnectorError:
            self._skip_url()
            self.log.warn(f'Unreachable {url}')
