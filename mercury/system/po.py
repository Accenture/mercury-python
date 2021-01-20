#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2021 Accenture Technology
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
from mercury.system.models import EventEnvelope
from mercury.system.singleton import Singleton
from mercury.system.utility import Utility


@Singleton
class PostOffice:
    """
    Convenient class for making RPC, async and callback.
    """

    DEFERRED_DELIVERY = 'system.deferred.delivery'

    def __init__(self):
        self.platform = Platform()
        self.util = Utility()

    def get_route(self):
        """
        Obtain my route name for the currently running service.
        This is useful for Role Based Access Control (RBAC) to restrict certain user roles for a service.
        Note that RBAC is the responsibility of the user application.

        :return: route name
        """
        trace_info = self.get_trace()
        return "?" if trace_info is None else trace_info.get_route()

    def get_trace_id(self):
        return self.platform.get_trace_id()

    def get_trace(self):
        return self.platform.get_trace()

    def annotate_trace(self, key: str, value: str):
        self.platform.annotate_trace(key, value)

    def broadcast(self, route: str, headers: dict = None, body: any = None) -> None:
        self.util.validate_service_name(route)
        if headers is None and body is None:
            raise ValueError('Unable to broadcast because both headers and body are missing')
        event = EventEnvelope().set_to(route)
        if headers is not None:
            if not isinstance(headers, dict):
                raise ValueError('headers must be dict')
            for h in headers:
                event.set_header(h, str(headers[h]))
        if body is not None:
            event.set_body(body)
        self.platform.send_event(event, True)

    def send_later(self, route: str, headers: dict = None, body: any = None, seconds: float = 1.0) -> None:
        self.util.validate_service_name(route, True)
        if isinstance(seconds, float) or isinstance(seconds, int):
            relay = dict()
            relay['route'] = route
            if headers is not None:
                relay_headers = dict()
                for h in headers:
                    relay_headers[str(h)] = str(headers[h])
                relay['headers'] = relay_headers
            if body is not None:
                relay['body'] = body
            relay['seconds'] = seconds
            self.send(self.DEFERRED_DELIVERY, body=relay)
        else:
            raise ValueError('delay in seconds must be int or float')

    def send(self, route: str, headers: dict = None, body: any = None, reply_to: str = None, me=True) -> None:
        self.util.validate_service_name(route, True)
        if headers is None and body is None:
            raise ValueError('Unable to send because both headers and body are missing')
        event = EventEnvelope().set_to(route)
        if headers is not None:
            if not isinstance(headers, dict):
                raise ValueError('headers must be dict')
            for h in headers:
                event.set_header(str(h), str(headers[h]))
        if body is not None:
            event.set_body(body)
        if reply_to is not None:
            if not isinstance(reply_to, str):
                raise ValueError('reply_to must be str')
            # encode 'me' in the "call back" if replying to this instance
            event.set_reply_to(reply_to, me)
        self.platform.send_event(event)

    def request(self, route: str, timeout_seconds: float,
                headers: dict = None, body: any = None,
                correlation_id: str = None) -> EventEnvelope:
        self.util.validate_service_name(route, True)
        if headers is None and body is None:
            raise ValueError('Unable to make RPC call because both headers and body are missing')
        timeout_value = self.util.get_float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError("timeout value in seconds must be positive number")
        event = EventEnvelope().set_to(route)
        if headers is not None:
            if not isinstance(headers, dict):
                raise ValueError('headers must be dict')
            for h in headers:
                event.set_header(h, str(headers[h]))
        if body is not None:
            event.set_body(body)
        if correlation_id is not None:
            event.set_correlation_id(str(correlation_id))
        return self.platform.request(event, timeout_seconds)

    def parallel_request(self, events: list, timeout_seconds: float) -> list:
        return self.platform.parallel_request(events, timeout_seconds)

    def exists(self, routes: any):
        return self.platform.exists(routes)
