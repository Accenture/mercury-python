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

import time
import os
import uuid
import struct
import inspect
from enum import Enum
from mercury.system.singleton import Singleton
from mercury.system.models import EventEnvelope


class FunctionType(Enum):
    INTERCEPTOR = 1
    REGULAR = 2
    SINGLETON = 3
    NOT_SUPPORTED = 4


@Singleton
class Utility:

    def __init__(self):

        def interceptor(event: EventEnvelope):
            """
            Sample interceptor function
            :param event: Event envelope that contains metadata such as correlation ID
            :return:
            """
            assert event is not None

        def regular_service(headers: dict, body: any, instance: int):
            """
            Sample function
            :param headers: dictionary of key-values as parameters
            :param body: message payload can be None, str, integer, float, map and other standard Python primitives.
            Note that The payload should be serializable with msgpack. i.e. it should not contain user defined classes.
            :param instance: the instance number of the selected worker
            :return: optional
            """
            assert type(headers) is dict
            assert type(instance) is int
            assert body is not None

        def singleton_service(headers: dict, body: any):
            """
            Singleton function that would be useful if you want to serialize operation. e.g. guarantee of sequencing.
            :param headers: dictionary of key-values as parameters
            :param body: message payload can be None, str, integer, float, map and other standard Python primitives.
            Note that The payload should be serializable with msgpack. i.e. it should not contain user defined classes.
            :return: optional
            """
            # singleton service function guarantees that there is only one instance.
            # important for services that want to guarantee event sequencing.
            assert type(headers) is dict
            assert body is not None

        # Generate parameter signatures from the sample service functions
        # (the assert statements in the sample functions are place-holders)
        self.interceptor_signature = str(inspect.signature(interceptor))
        self.regular_signature = str(inspect.signature(regular_service))
        self.singleton_signature = str(inspect.signature(singleton_service))
        self.inbox_sample = 'r.' + (''.join(str(uuid.uuid4()).split('-')))

    def get_function_type(self, user_function):
        if not inspect.ismethod(user_function) and not inspect.isfunction(user_function):
            raise ValueError("user_function must be a function or a method")
        # validate function signature
        signature = str(inspect.signature(user_function))
        if signature == self.interceptor_signature:
            return FunctionType.INTERCEPTOR
        elif signature == self.regular_signature:
            return FunctionType.REGULAR
        elif signature == self.singleton_signature:
            return FunctionType.SINGLETON
        else:
            return FunctionType.NOT_SUPPORTED

    def is_inbox(self, route: str):
        if route.startswith('r.') and len(route) == len(self.inbox_sample):
            name = route[2:]
            for c in name:
                if '0' <= c <= '9':
                    continue
                if 'a' <= c <= 'f':
                    continue
                return False
            return True
        else:
            return False

    @staticmethod
    def validate_service_name(name: str, po: bool = False):
        if not isinstance(name, str):
            raise ValueError("route must be str")
        route = name[0:name.index('@')] if po and '@' in name else name
        if route.startswith('.'):
            raise ValueError("route name must not start with period")
        if route.startswith('_'):
            raise ValueError("route name must not start with underline")
        if route.startswith('-'):
            raise ValueError("route name must not start with hyphen")
        if '..' in route:
            raise ValueError("route name must not contain consecutive dots")
        if route.endswith('.'):
            raise ValueError("route name must not end with period")
        if route.endswith('_'):
            raise ValueError("route name must not end with underline")
        if route.endswith('-'):
            raise ValueError("route name must not end with hyphen")
        if '.' not in route:
            raise ValueError("route name must be separated by a dot. e.g. hello.world")
        for c in route:
            if '0' <= c <= '9':
                continue
            if 'a' <= c <= 'z':
                continue
            if c == '.' or c == '_' or c == '-':
                continue
            raise ValueError("route name must use 0-9, a-z, period, hyphen or underline. e.g. hello.world")

    @staticmethod
    def multi_split(string, chars):
        """
        Split a string into an array of string using one or more separators
        :param string: input
        :param chars: string containing one or more separator characters
        :return: array of strings
        """
        separator = None
        n = 0
        for c in chars:
            if separator is None:
                separator = c
            else:
                string = string.replace(c, separator)
            n += 1
        if separator is None:
            raise ValueError("Empty separator characters")

        rv = []
        parts = string.split(separator)
        for p in parts:
            if len(p) > 0:
                rv.append(p)
        return rv

    @staticmethod
    def trim_array(text_array):
        segments = [i.strip() for i in text_array]
        result = []
        for s in segments:
            if len(s) > 0:
                result.append(s)
        return result

    def normalize_path(self, path: str):
        """
        Normalize a path to remove duplicated slashes
        :param path: input
        :return: normalized path
        """
        path = path.replace('\\', '/')
        elements = self.multi_split(path, '/')
        slash = '/' if path.startswith('/') else ''
        return slash + '/'.join(elements)

    def cleanup_dir(self, path, clear_dir=True):
        if os.path.exists(path):
            files = os.listdir(path)
            for f in files:
                os.remove(self.normalize_path(path + '/' + f))
            if clear_dir:
                os.rmdir(path)

    @staticmethod
    def int_to_bytes(n: int):
        """
        integer to bytes
        :param n: integer value
        :return: byte value
        """
        return struct.pack('>I', n)

    @staticmethod
    def bytes_to_int(b: bytes):
        """
        bytes to integer
        :param b: byte value
        :return: integer value
        """
        return struct.unpack(">I", b)[0]

    @staticmethod
    def get_iso_8601(seconds: float, show_ms=False):
        if show_ms:
            utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
            ms = format(seconds - int(seconds), '.3f')
            return utc+ms[1:]+'Z'
        else:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))

    @staticmethod
    def get_rfc_1123(seconds: float):
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(seconds))

    @staticmethod
    def get_float(number: any):
        if isinstance(number, int) or isinstance(number, float):
            return number
        try:
            return float(str(number))
        except ValueError:
            return -1.0
