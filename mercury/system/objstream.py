#!/usr/bin/env python
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

from mercury.platform import Platform
from mercury.system.po import PostOffice
from mercury.system.models import EventEnvelope, AppException
from mercury.system.utility import Utility


class ObjectStreamIO:

    STREAM_IO_MANAGER = 'object.streams.io'

    def __init__(self, route: str = None, expiry_seconds: int = 1800):
        self.platform = Platform()
        self.po = PostOffice()
        self.util = Utility()
        self.route = None
        self.input_stream = None
        self.output_stream = None
        self.eof = False
        self.input_closed = False
        self.output_closed = False

        if route is not None:
            # open an existing stream
            if isinstance(route, str):
                name: str = route
                if name.startswith('stream.') and '@' in name:
                    self.route = name
            if self.route is None:
                raise ValueError('Invalid stream route')
        else:
            # create a new stream
            if not isinstance(expiry_seconds, int):
                raise ValueError('expiry_seconds must be int')
            result = self.po.request(self.STREAM_IO_MANAGER, 6.0,
                                     headers={'type': 'create', 'expiry_seconds': expiry_seconds})
            if isinstance(result, EventEnvelope) and isinstance(result.get_body(), str) \
                    and result.get_status() == 200:
                name: str = result.get_body()
                if name.startswith('stream.') and '@' in name:
                    self.route = name
            if self.route is None:
                raise IOError('Stream manager is not responding correctly')

    def get_route(self):
        return self.route

    def is_eof(self):
        return self.eof

    def read(self, timeout_seconds: float):
        if self.input_stream:
            return self.input_stream()

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
                result = self.po.request(self.route, timeout_seconds, headers={'type': 'read'})
                if isinstance(result, EventEnvelope):
                    if result.get_status() == 200:
                        payload_type = result.get_headers().get('type')
                        if 'eof' == payload_type:
                            self.eof = True
                            break
                        if 'body' == payload_type:
                            yield result.get_body()
                    else:
                        raise AppException(result.get_status(), str(result.get_body()))

        self.input_stream = reader
        return reader()

    def write(self, payload: any, timeout_seconds: float = 10.0):
        if isinstance(timeout_seconds, int):
            timeout_seconds = float(timeout_seconds)

        if isinstance(timeout_seconds, float):
            # minimum write timeout is five second
            if timeout_seconds < 5.0:
                timeout_seconds = 5.0
        else:
            raise ValueError('Write timeout must be float or int')

        if not self.output_closed:
            if isinstance(payload, dict) or isinstance(payload, str) or isinstance(payload, bool) \
                    or isinstance(payload, int) or isinstance(payload, float):
                # for orderly write, use RPC request to guarantee that payload is written into the object stream
                self.po.request(self.route, timeout_seconds, headers={'type': 'write'}, body=payload)
            else:
                raise ValueError('payload must be dict, str, bool, int or float')

    def send_eof(self):
        if not self.output_closed:
            self.output_closed = True
            self.po.send(self.route, headers={'type': 'eof'})

    def is_output_closed(self):
        return self.output_closed

    def get_local_streams(self):
        result = self.po.request(self.STREAM_IO_MANAGER, 6.0, headers={'type': 'query'})
        if isinstance(result, EventEnvelope) and isinstance(result.get_body(), dict) \
                and result.get_status() == 200:
            return result.get_body()
        else:
            return dict()

    def close(self):
        if not self.input_closed:
            self.input_closed = True
            self.po.send(self.route, headers={'type': 'close'})

    def is_input_closed(self):
        return self.input_closed
