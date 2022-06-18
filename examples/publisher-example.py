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

platform = Platform()
log = platform.get_logger()


def publish_some_events():
    util = Utility()
    ps = PubSub()
    if ps.feature_enabled():
        # Publish an event
        # headers = optional parameters for the event
        # body = event payload
        for x in range(10):
            log.info("publishing event#" + str(x))
            ps.publish("hello.topic", headers={"some_parameter": "some_value", "n": x},
                       body="hello python - " + util.get_iso_8601(time.time()))
    else:
        print("Pub/Sub feature is not available from the underlying event stream")
        print("Did you start the language connector with cloud.connector=Kafka or cloud.services=kafka.pubsub?")
        print("e.g. java -Dcloud.connector=kafka -Dcloud.services=kafka.reporter -jar language-connector.jar")

    # quit application
    platform.stop()


def main():
    def life_cycle_listener(headers: dict, body: any):
        # Detect when cloud is ready
        log.info("Cloud life cycle event - " + str(headers))
        if 'type' in headers and 'ready' == headers['type']:
            publish_some_events()

    platform.register('my.cloud.status', life_cycle_listener, is_private=True)
    platform.subscribe_life_cycle('my.cloud.status')

    # connect to cloud after setting up life cycle event listener
    platform.connect_to_cloud()
    platform.run_forever()


if __name__ == '__main__':
    main()
