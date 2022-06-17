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
from mercury.system.pubsub import PubSub

platform = Platform()
log = platform.get_logger()
ps = PubSub()


def hello(headers: dict, body: any, instance: int):
    # this is a very simple pub/sub subscriber function
    # no need to return anything because pub/sub subscriber is a listener only
    log.info("#" + str(instance) + " GOT headers = " + str(headers) + " body = " + str(body))


def unsubscribe_from_topic():
    ps.unsubscribe('hello.topic', 'hello.world')


def subscribe_to_topic():

    if ps.feature_enabled():

        # ensure the topic exists
        ps.create_topic('hello.topic', 5)
        #
        # The pub/sub topic name must be different from the subscriber function route name
        #
        # Note:
        # For kafka, the parameter list includes the following:
        # client_id, group_id and optional offset number (as a string)
        # e.g. ["client1", "group1"] or ["client1", "group1", "0"]
        #
        # In this example, it is reading from the latest without the offset number.
        #
        # Since READ offset is maintained per partition in each topic,
        # it can only be reset when your topic has only one partition
        # or when your app subscribes to a specific partition using the
        # pubsub.subscribe_to_partition() method.
        #
        try:
            count = ps.partition_count('hello.topic')
            log.info('hello.topic has '+str(count)+' partitions')

            ps.subscribe("hello.topic", "hello.world", ["client1", "group1"])

        except Exception as e:
            log.error(type(e).__name__ + ': ' + str(e))
            platform.stop()

    else:
        print("Pub/Sub feature is not available from the underlying event stream")
        print("Did you start the language connector with cloud.connector=Kafka or cloud.services=kafka.pubsub?")
        print("e.g. java -Dcloud.connector=kafka -Dcloud.services=kafka.reporter -jar language-connector.jar")
        platform.stop()


def main():
    # Register a route name for a pub/sub subscriber function
    # Service is a singleton if number of instances is not given
    platform.register('hello.world', hello)

    def life_cycle_listener(headers: dict, body: any):
        # Detect when cloud is up or down
        log.info("Cloud life cycle event - " + str(headers))
        if 'type' in headers:
            if 'ready' == headers['type']:
                subscribe_to_topic()
            if 'close' == headers['type']:
                unsubscribe_from_topic()

    platform.register('my.cloud.status', life_cycle_listener, is_private=True)
    platform.subscribe_life_cycle('my.cloud.status')

    # connect to cloud after setting up life cycle event listener
    platform.connect_to_cloud()
    platform.run_forever()


if __name__ == '__main__':
    main()
