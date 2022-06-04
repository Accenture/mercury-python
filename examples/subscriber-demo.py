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

import time
from mercury.platform import Platform
from mercury.system.pubsub import PubSub


def hello(headers: dict, body: any, instance: int):
    # this is a very simple pub/sub subscriber function
    # no need to return anything because pub/sub subscriber is a listener only
    print("#" + str(instance), "GOT", "headers =", str(headers), "body =", str(body))


def main():
    platform = Platform()
    # register a route name for a pub/sub subscriber function
    # setting number of instance to 1 because pub/sub subscriber is always a singleton
    platform.register('hello.world', hello, 1)

    # Once it connects to the network, it is ready to serve requests
    platform.connect_to_cloud()
    # wait until connected
    while not platform.cloud_ready():
        try:
            time.sleep(0.1)
        except KeyboardInterrupt:
            # this allows us to stop the application while waiting for cloud connection
            platform.stop()
            return

    pubsub = PubSub()
    if pubsub.feature_enabled():

        pubsub.create_topic('hello.topic', 5)

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
            count = pubsub.partition_count('hello.topic')
            print('hello.topic has '+str(count)+' partitions')

            pubsub.subscribe("hello.topic", "hello.world", ["client1", "group1"])
            #
            # This will keep the main thread running in the background.
            # We can use Control-C or KILL signal to stop the application.
            platform.run_forever()

        except Exception as e:
            print(type(e).__name__ + ': ' + str(e))
            platform.stop()

    else:
        print("Pub/Sub feature is not available from the underlying event stream")
        print("Did you start the language connector with cloud.connector=Kafka or cloud.services=kafka.pubsub?")
        print("e.g. java -Dcloud.connector=kafka -Dcloud.services=kafka.reporter -jar language-connector.jar")
        platform.stop()


if __name__ == '__main__':
    main()
