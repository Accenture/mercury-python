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

from mercury.platform import Platform
from mercury.system.models import EventEnvelope
from mercury.system.po import PostOffice


class Hi:
    MY_NAME = 'Hi'

    def hello(self, headers: dict, body: any):
        # singleton function signature (headers: dict, body: any)
        Platform().log.info(self.MY_NAME+" "+str(headers) + ", " + str(body))
        return body


def hello(headers: dict, body: any, instance: int):
    # regular function signature (headers: dict, body: any, instance: int)
    Platform().log.info("#"+str(instance)+" "+str(headers)+" body="+str(body))
    # to set status, headers and body, return them in an event envelope
    result = EventEnvelope().set_header('hello', 'world').set_body(body)
    for h in headers:
        result.set_header(h, headers[h])
    return result


def main():
    platform = Platform()
    # you can register a method of a class
    platform.register('hello.world.1', Hi().hello, 5)
    # or register a function
    platform.register('hello.world.2', hello, 10)

    po = PostOffice()
    # demonstrate sending asynchronously. Note that key-values in the headers will be encoded as strings
    po.send('hello.world.1', headers={'one': 1}, body='hello world one')
    po.send('hello.world.2', headers={'two': 2}, body='hello world two')

    # demonstrate a RPC request
    try:
        result = po.request('hello.world.2', 2.0, headers={'some_key': 'some_value'}, body='hello world')
        if isinstance(result, EventEnvelope):
            print('Received RPC response:')
            print("HEADERS =", result.get_headers(), ", BODY =", result.get_body(),
                  ", STATUS =",  result.get_status(),
                  ", EXEC =", result.get_exec_time(), ", ROUND TRIP =", result.get_round_trip(), "ms")
    except TimeoutError as e:
        print("Exception: ", str(e))

    # illustrate parallel RPC requests
    event_list = list()
    event_list.append(EventEnvelope().set_to('hello.world.1').set_body("first request"))
    event_list.append(EventEnvelope().set_to('hello.world.2').set_body("second request"))
    try:
        result = po.parallel_request(event_list, 2.0)
        if isinstance(result, list):
            print('Received', len(result), 'RPC responses:')
            for res in result:
                print("HEADERS =", res.get_headers(), ", BODY =", res.get_body(),
                      ", STATUS =",  res.get_status(),
                      ", EXEC =", res.get_exec_time(), ", ROUND TRIP =", res.get_round_trip(), "ms")
    except TimeoutError as e:
        print("Exception: ", str(e))

    # connect to the network
    platform.connect_to_cloud()
    # wait until connected
    while not platform.cloud_ready():
        try:
            time.sleep(0.1)
        except KeyboardInterrupt:
            # this allows us to stop the application while waiting for cloud connection
            platform.stop()
            return

    # Demonstrate broadcast feature
    # the event will be broadcast to multiple application instances that serve the same route
    po.broadcast("hello.world.1", body="this is a broadcast message from "+platform.get_origin())

    # demonstrate deferred delivery
    po.send_later('hello.world.1', headers={'hello': 'world'}, body='this message arrives 5 seconds later', seconds=5.0)

    #
    # this will keep the main thread running in the background
    # so we can use Control-C or KILL signal to stop the application
    platform.run_forever()


if __name__ == '__main__':
    main()
