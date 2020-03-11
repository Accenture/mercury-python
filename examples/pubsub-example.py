#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2020 Accenture Technology
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
from mercury.system.utility import Utility


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

    util = Utility()
    pubsub = PubSub()
    if pubsub.feature_enabled():
        #
        # the pub/sub topic name must be different from the subscriber function name
        # Note:
        # For kafka, the parameter list includes the following:
        # client_id, group_id and optional offset number (as a string)
        # e.g. ["client1", "group1"] or ["client1", "group1", "0"]
        #
        # To demonstrate that you can reset the READ offset, start/stop this application without the offset parameter
        # for a few times and then restart the application with offset "0"
        #
        pubsub.subscribe("hello.topic", "hello.world", ["client1", "group1"])
        # this is just a demo:
        # Publish an event
        # - this will send the event to the pub/sub system that the subscriber "hello.world" will receive it.
        # headers = optional parameters for the event
        # body = event payload
        pubsub.publish("hello.topic", headers={"some_parameter": "some_value"},
                       body="hello world "+util.get_iso_8601(time.time()))
    else:
        print("Pub/Sub feature not available from the underlying event stream")
        print("Did you start the language connector with Kafka?")
        print("e.g. java -Dcloud.connector=kafka -Dcloud.services=kafka.reporter -jar language-connector-1.12.31.jar")

    #
    # this will keep the main thread running in the background
    # so we can use Control-C or KILL signal to stop the application
    platform.run_forever()


if __name__ == '__main__':
    main()
