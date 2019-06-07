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

from mercury.system.singleton import Singleton
from mercury.platform import Platform
from mercury.system.po import PostOffice
from mercury.system.models import EventEnvelope, AppException
from mercury.system.utility import Utility


@Singleton
class PubSub:

    def __init__(self):
        self.platform = Platform()
        self.po = PostOffice()
        self.util = Utility()
        self.subscription = dict()

        def subscription_sync(headers: dict, body: any):
            if 'type' in headers and headers['type'] == 'subscription_sync':
                if len(self.subscription) > 0:
                    for topic in self.subscription:
                        route_map = self.subscription[topic]
                        for route in route_map:
                            parameters = route_map[route]
                            self.platform.log.info('Update subscription '+topic+' -> '+route)
                            self.subscribe(topic, route, parameters)
                else:
                    self.platform.log.info('No subscription to update')

        self.platform.register('pub.sub.sync', subscription_sync, 1, is_private=True)

    def feature_enabled(self):
        result = self.po.request('pub.sub.controller', 10.0, headers={'type': 'feature'})
        return self._normalize_result(result, True)

    def list_topics(self):
        result = self.po.request('pub.sub.controller', 10.0, headers={'type': 'list'})
        return self._normalize_result(result, list())

    def exists(self, topic: str):
        if isinstance(topic, str):
            result = self.po.request('pub.sub.controller', 10.0, headers={'type': 'exists', 'topic': topic})
            return self._normalize_result(result, True)
        else:
            return False

    def create_topic(self, topic: str):
        if isinstance(topic, str):
            result = self.po.request('pub.sub.controller', 10.0, headers={'type': 'create', 'topic': topic})
            return self._normalize_result(result, True)
        else:
            raise ValueError("topic must be str")

    def delete_topic(self, topic: str):
        if isinstance(topic, str):
            result = self.po.request('pub.sub.controller', 10.0, headers={'type': 'delete', 'topic': topic})
            return self._normalize_result(result, True)
        else:
            raise ValueError("topic must be str")

    def publish(self, topic: str, headers: dict = None, body: any = None):
        if isinstance(topic, str):
            # encode payload
            payload = dict()
            payload['body'] = body
            payload['headers'] = self._normalize_headers(headers)
            result = self.po.request('pub.sub.controller', 10.0,
                                     headers={'type': 'publish', 'topic': topic}, body=payload)
            return self._normalize_result(result, True)
        else:
            raise ValueError("topic must be str")

    def subscribe(self, topic: str, route: str, parameters: list = None):
        if isinstance(topic, str) and isinstance(route, str):
            if self.platform.has_route(route):
                normalized_config = self._normalize_parameters(parameters)
                result = self.po.request('pub.sub.controller', 10.0, body=normalized_config,
                                         headers={'type': 'subscribe', 'topic': topic, 'route': route})
                done = self._normalize_result(result, True)
                if done:
                    if topic not in self.subscription:
                        self.subscription[topic] = dict()
                        self.platform.log.info('Subscribed topic ' + topic)
                    route_map: dict = self.subscription[topic]
                    if route not in route_map:
                        route_map[route] = normalized_config
                        self.platform.log.info('Adding '+route+' to topic '+topic)
                return done
            else:
                raise ValueError("Unable to subscribe topic " + topic + " because route " + route + " not registered")
        else:
            raise ValueError("topic and route must be str")

    def unsubscribe(self, topic: str, route: str):
        if isinstance(topic, str) and isinstance(route, str):
            if self.platform.has_route(route):
                result = self.po.request('pub.sub.controller', 10.0,
                                         headers={'type': 'unsubscribe', 'topic': topic, 'route': route})
                done = self._normalize_result(result, True)
                if done:
                    if topic in self.subscription:
                        route_map: dict = self.subscription[topic]
                        if route in route_map:
                            route_map.pop(route)
                            self.platform.log.info('Removing ' + route + ' from topic ' + topic)
                            if len(route_map) == 0:
                                self.subscription.pop(topic)
                                self.platform.log.info('Unsubscribed topic ' + topic)
                return done
            else:
                raise ValueError("Unable to unsubscribe topic " + topic + " because route " + route + " not registered")
        else:
            raise ValueError("topic and route must be str")

    @staticmethod
    def _normalize_result(result: EventEnvelope, result_obj: any):
        if isinstance(result, EventEnvelope):
            if result.get_status() == 200:
                if isinstance(result.get_body(), type(result_obj)):
                    return result.get_body()
                else:
                    raise AppException(500, str(result.get_body()))
            else:
                raise AppException(result.get_status(), str(result.get_body()))

    @staticmethod
    def _normalize_headers(headers: dict):
        if headers is None:
            return dict()
        if isinstance(headers, dict):
            result = dict()
            for h in headers:
                result[str(h)] = str(headers[h])
            return result
        else:
            raise ValueError("headers must be dict of str key-values")

    @staticmethod
    def _normalize_parameters(parameters: list):
        if parameters is None:
            return list()
        if isinstance(parameters, list):
            result = list()
            for h in parameters:
                result.append(str(h))
            return result
        else:
            raise ValueError("headers must be a list of str")
