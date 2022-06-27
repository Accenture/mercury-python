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

from mercury.system.singleton import Singleton
from mercury.platform import Platform
from mercury.system.po import PostOffice
from mercury.system.models import EventEnvelope, AppException
from mercury.system.utility import Utility

platform = Platform()
log = platform.get_logger()
po = PostOffice()
util = Utility()


@Singleton
class PubSub:

    def __init__(self, domain: str = 'system'):
        if not isinstance(domain, str):
            raise ValueError('Pub/sub domain must be str. e.g. system or user')
        value = domain.strip()
        self.domain = 'system' if value == '' else value
        self.subscription = dict()

    def feature_enabled(self):
        result = po.request('pub.sub.controller', 10.0, headers={'type': 'feature', 'domain': self.domain})
        return self._normalize_result(result, True)

    def list_topics(self):
        result = po.request('pub.sub.controller', 10.0, headers={'type': 'list', 'domain': self.domain})
        return self._normalize_result(result, list())

    def exists(self, topic: str):
        if isinstance(topic, str):
            result = po.request('pub.sub.controller', 10.0,
                                headers={'type': 'exists', 'topic': topic, 'domain': self.domain})
            return self._normalize_result(result, True)
        else:
            return False

    def create_topic(self, topic: str, partition: int = -1):
        if isinstance(topic, str):
            result = po.request('pub.sub.controller', 10.0,
                                headers={'type': 'create', 'topic': topic, 'partition': partition,
                                         'domain': self.domain})
            return self._normalize_result(result, True)
        else:
            raise ValueError('topic must be str')

    def partition_count(self, topic: str):
        if isinstance(topic, str):
            result = po.request('pub.sub.controller', 10.0, headers={'type': 'partition_count', 'topic': topic,
                                                                     'domain': self.domain})
            return self._normalize_result(result, 0)
        else:
            raise ValueError('topic must be str')

    def delete_topic(self, topic: str):
        if isinstance(topic, str):
            result = po.request('pub.sub.controller', 10.0, headers={'type': 'delete', 'topic': topic,
                                                                     'domain': self.domain})
            return self._normalize_result(result, True)
        else:
            raise ValueError('topic must be str')

    def publish(self, topic: str, headers: dict = None, body: any = None):
        return self.publish_to_partition(topic, -1, headers, body)

    def publish_to_partition(self, topic: str, partition: int, headers: dict = None, body: any = None):
        if not isinstance(topic, str):
            raise ValueError('topic must be str')
        if not isinstance(partition, int):
            raise ValueError('partition must be int')
        # encode payload
        payload = dict()
        payload['body'] = body
        payload['headers'] = headers
        if partition < 0:
            result = po.request('pub.sub.controller', 10.0,
                                headers={'type': 'publish', 'topic': topic, 'domain': self.domain}, body=payload)
        else:
            result = po.request('pub.sub.controller', 10.0, body=payload,
                                headers={'type': 'publish', 'topic': topic, 'partition': partition,
                                         'domain': self.domain})
        return self._normalize_result(result, True)

    def subscribe(self, topic: str, route: str, parameters: list = None):
        return self.subscribe_to_partition(topic, -1, route, parameters)

    def subscribe_to_partition(self, topic: str, partition: int, route: str, parameters: list = None):
        if not isinstance(partition, int):
            raise ValueError('partition must be int')
        if isinstance(topic, str) and isinstance(route, str):
            if platform.has_route(route):
                if route == topic:
                    raise ValueError('pub/sub topic name must be different from the subscriber function route name')
                prev_map: dict = self.subscription[topic] if topic in self.subscription else dict()
                if route in prev_map:
                    raise ValueError(f'Route {route} has already subscribed to topic {topic}')
                if partition < 0:
                    result = po.request('pub.sub.controller', 10.0, body=parameters,
                                        headers={'type': 'subscribe', 'topic': topic, 'route': route,
                                                 'domain': self.domain})
                else:
                    result = po.request('pub.sub.controller', 10.0, body=parameters,
                                        headers={'type': 'subscribe',
                                                 'topic': topic, 'partition': partition, 'route': route,
                                                 'domain': self.domain})
                done = self._normalize_result(result, True)
                if done:
                    if topic not in self.subscription:
                        self.subscription[topic] = dict()
                        log.info(f'Subscribed topic {topic}')
                    route_map: dict = self.subscription[topic]
                    if route not in route_map:
                        route_map[route] = {'parameters': parameters, 'partition': partition}
                        if partition < 0:
                            log.info(f'Attach {route} to topic {topic}')
                        else:
                            log.info(f'Attach {route} to topic {topic} partition {partition}')
                return done
            else:
                raise ValueError(f'Unable to subscribe topic {topic} because route {route} not registered')
        else:
            raise ValueError('topic and route must be str')

    def unsubscribe(self, topic: str, route: str):
        if isinstance(topic, str) and isinstance(route, str):
            if platform.has_route(route):
                route_map: dict = self.subscription[topic] if topic in self.subscription else dict()
                if route not in route_map:
                    raise ValueError(f'Route {route} was not subscribed to topic {topic}')
                route_map.pop(route)
                log.info(f'Detach {route} from topic {topic}')
                if len(route_map) == 0:
                    self.subscription.pop(topic)
                    log.info(f'Unsubscribed topic {topic}')

                if platform.cloud_ready():
                    result = po.request('pub.sub.controller', 10.0,
                                        headers={'type': 'unsubscribe', 'topic': topic, 'route': route,
                                                 'domain': self.domain})
                    return self._normalize_result(result, True)
                else:
                    log.warn('Subscription is ignored because cloud connection is not ready')
                    return False
            else:
                raise ValueError(f'Unable to unsubscribe topic {topic} because route {route} not registered')
        else:
            raise ValueError('topic and route must be str')

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
