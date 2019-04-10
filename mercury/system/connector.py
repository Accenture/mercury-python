# -*- coding: utf-8 -*-
#
# Copyright 2018-2019 Accenture Technology
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

import time
import asyncio
import aiohttp
import msgpack

from mercury.system.models import EventEnvelope
from mercury.system.utility import Utility
from mercury.system.singleton import Singleton


@Singleton
class NetworkConnector:

    INCOMING_WS_PATH = "ws.incoming"
    OUTGOING_WS_PATH = "ws.outgoing"
    SYSTEM_ALERT = "system.alerts"

    def __init__(self, platform, loop, api_key, url):
        self.url = url
        self.api_key = api_key
        self.platform = platform
        self._loop = loop
        self.log = platform.log
        self.normal = True
        self.started = False
        self.ws = None
        self.close_code = 1000
        self.close_message = 'OK'
        self.last_active = time.time()
        self.util = Utility()

    def send_keep_alive(self):
        message = "Keep-Alive "+self.util.get_iso_8601(time.time(), show_ms=True)
        envelope = EventEnvelope()
        envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'text').set_body(message)
        self.platform.send_event(envelope)

    def send_payload(self, data: dict):
        payload = msgpack.packb(data, use_bin_type=True)
        envelope = EventEnvelope()
        envelope.set_to(self.OUTGOING_WS_PATH).set_header('type', 'bytes').set_body(payload)
        self.platform.send_event(envelope)

    def alert(self, headers: dict, body: any):
        if 'status' in headers:
            if headers['status'] == '200':
                self.log.info(str(body))
            else:
                self.log.warn(str(body)+", status="+headers['status'])

    def incoming(self, headers: dict, body: any):
        """
        This function handles incoming messages from the websocket connection with the Mercury language connector.
        It must be invoked using events. It should not be called directly to guarantee proper event sequencing.

        :param headers: type is open, close, text or bytes
        :param body: string or bytes
        :return: None
        """
        if self.ws and 'type' in headers:
            if headers['type'] == 'open':
                self.log.info("Ready")
                self.send_payload({'type': 'login', 'api_key': self.api_key})
                for r in self.platform.get_routes('public'):
                    self.send_payload({'type': 'add', 'route': r})

            if headers['type'] == 'close':
                self.log.info("Closed")
            if headers['type'] == 'text':
                self.log.debug(body)
            if headers['type'] == 'bytes':
                event = msgpack.unpackb(body, raw=False)
                if 'type' in event:
                    event_type = event['type']
                    if event_type == 'event' and 'event' in event:
                        envelope = EventEnvelope()
                        inner_event = envelope.from_map(event['event'])
                        if self.platform.has_route(inner_event.get_to()):
                            self.platform.send_event(inner_event)

    def outgoing(self, headers: dict, body: any):
        """
        This function handles sending outgoing messages to the websocket connection with the Mercury language connector.
        It must be invoked using events. It should not be called directly to guarantee proper event sequencing.

        :param headers: type is close, text or bytes
        :param body: string or bytes
        :return: None
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

    def start_connection(self):
        async def worker():
            while self.normal:
                await self._loop.create_task(self.connection_handler())
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
            self.platform.register(self.INCOMING_WS_PATH, self.incoming, 1, is_private=True)
            self.platform.register(self.OUTGOING_WS_PATH, self.outgoing, 1, is_private=True)
            self.platform.register(self.SYSTEM_ALERT, self.alert, 1, is_private=True)
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
        self._loop.call_soon_threadsafe(closing, code, reason)

    async def connection_handler(self):
        try:
            async with aiohttp.ClientSession(loop=self._loop, timeout=aiohttp.ClientTimeout(total=10)) as session:
                self.ws = await session.ws_connect(self.url)
                envelope = EventEnvelope()
                envelope.set_to(self.INCOMING_WS_PATH).set_header('type', 'open')
                self.platform.send_event(envelope)
                self.log.info("Connected to "+self.url)
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
                            self.log.error("Unexpected connection error")
                        if msg.type == aiohttp.WSMsgType.CLOSING:
                            # closing signal received - close the connection now
                            self.log.info("Disconnected, status="+str(self.close_code)+", message="+self.close_message)
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
                            self.log.info("Disconnected, status="+str(self.close_code)+", message="+self.close_message)
                            if self.platform.has_route(self.INCOMING_WS_PATH):
                                envelope = EventEnvelope()
                                envelope.set_to(self.INCOMING_WS_PATH).set_body(self.close_message)\
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
            self.log.warn("Unreachable "+self.url)
