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
from mercury.system.utility import Utility


def main():
    platform = Platform()
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
        # Publish an event
        # headers = optional parameters for the event
        # body = event payload
        for x in range(10):
            print("publishing event#", x)
            pubsub.publish("hello.topic", headers={"some_parameter": "some_value", "n": x},
                           body="hello world " + util.get_iso_8601(time.time()))
    else:
        print("Pub/Sub feature is not available from the underlying event stream")
        print("Did you start the language connector with cloud.connector=Kafka or cloud.services=kafka.pubsub?")
        print("e.g. java -Dcloud.connector=kafka -Dcloud.services=kafka.reporter -jar language-connector.jar")

    # quit application
    platform.stop()


if __name__ == '__main__':
    main()
