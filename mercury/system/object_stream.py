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

from mercury.system.po import PostOffice
from mercury.system.models import EventEnvelope, AppException


class ObjectStreamIO:
    STREAM_IO_MANAGER = 'object.streams.io'

    def __init__(self, expiry_seconds: int = 1800):
        self.po = PostOffice()
        self.in_stream = None
        self.out_stream = None

        # create a new stream
        if not isinstance(expiry_seconds, int):
            raise ValueError('expiry_seconds must be int')
        result = self.po.request(self.STREAM_IO_MANAGER, 6.0,
                                 headers={'type': 'create_stream', 'expiry': expiry_seconds})
        if isinstance(result, EventEnvelope) and isinstance(result.get_body(), dict) \
                and result.get_status() == 200:
            response: dict = result.get_body()
            if 'in' in response and 'out' in response:
                self.in_stream = response['in']
                self.out_stream = response['out']
        if self.in_stream is None:
            raise IOError('Invalid response from stream manager')

    def get_input_stream(self):
        return self.in_stream

    def get_output_stream(self):
        return self.out_stream


class ObjectStreamReader:

    def __init__(self, route: str):
        if not isinstance(route, str):
            raise ValueError('output stream-ID must be str')
        self.closed = False
        self.eof = False
        self.po = PostOffice()
        self.input_stream = route
        self.stream = None

    def read(self, timeout_seconds: float):
        if self.stream:
            return self.stream()

        if isinstance(timeout_seconds, int):
            timeout_seconds = float(timeout_seconds)

        if isinstance(timeout_seconds, float):
            # minimum read timeout is one second
            if timeout_seconds < 1.0:
                timeout_seconds = 1.0
        else:
            raise ValueError('Read timeout must be float or int')

        def reader():
            while not self.eof:
                # if input stream has nothing, it will throw TimeoutError
                result = self.po.request(self.input_stream, timeout_seconds, headers={'type': 'read'})
                if isinstance(result, EventEnvelope):
                    if result.get_status() == 200:
                        payload_type = result.get_headers().get('type')
                        if 'eof' == payload_type:
                            self.eof = True
                            yield None
                        if 'data' == payload_type:
                            yield result.get_body()
                    else:
                        raise AppException(result.get_status(), str(result.get_body()))

        self.stream = reader
        return reader()

    def close(self):
        if not self.closed:
            self.closed = True
            self.po.request(self.input_stream, 10.0, headers={'type': 'close'})


class ObjectStreamWriter:

    def __init__(self, route: str):
        if not isinstance(route, str):
            raise ValueError('output stream-ID must be str')
        self.closed = False
        self.po = PostOffice()
        self.output_stream = route

    def write(self, payload: any):
        if not self.closed:
            if isinstance(payload, dict) or isinstance(payload, str) \
                    or isinstance(payload, bytes) \
                    or isinstance(payload, int) or isinstance(payload, float) or isinstance(payload, bool):
                # for orderly write, use RPC request to guarantee that payload is written into the object stream
                self.po.send(self.output_stream, headers={'type': 'data'}, body=payload)
            else:
                raise ValueError('payload must be dict, str, bool, int or float')

    def close(self):
        if not self.closed:
            self.closed = True
            self.po.send(self.output_stream, headers={'type': 'eof'})
