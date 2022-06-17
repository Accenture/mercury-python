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
from mercury.platform import Platform
from mercury.system.po import PostOffice

platform = Platform()
po = PostOffice()
log = platform.get_logger()


class Forwarder:

    def __init__(self, route: str):
        if isinstance(route, str):
            self.me = route
            self.subscription = dict()
        else:
            raise ValueError('route name for a forwarder service should be str')

    def relay(self, headers: dict, body: any):
        if 'type' in headers:
            if 'route' in headers and isinstance(headers['route'], str):
                route = headers['route']
                if 'subscribe' == headers['type']:
                    if route not in self.subscription:
                        self.subscription[route] = True
                        log.info(route + ' subscribed to ' + self.me)
                        return True
                    else:
                        return False
                if 'unsubscribe' == headers['type']:
                    if route in self.subscription:
                        self.subscription.pop(route)
                        log.info(route + ' unsubscribed from ' + self.me)
                        return True
                    else:
                        return False
                if 'subscription' == headers['type']:
                    return list(self.subscription.keys())
        # relay events
        for subscriber in self.subscription:
            try:
                po.send(subscriber, headers, body)
            except ValueError as e:
                log.warn('unable to relay event to '+subscriber + ' - ' + str(e))
