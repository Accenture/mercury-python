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
from mercury.system.models import EventEnvelope
from mercury.system.po import PostOffice

platform = Platform()
log = platform.get_logger()
po = PostOffice()


class Hi:
    MY_NAME = 'Hi'

    def hello(self, headers: dict, body: any):
        # singleton function signature (headers: dict, body: any)
        log.info(f'{self.MY_NAME} {headers}, {body}')
        return EventEnvelope().set_body({'body': body, 'origin': platform.get_origin()}) \
            .set_header('x-notes', 'I am a python app')


def hello(headers: dict, body: any, instance: int):
    # regular function signature (headers: dict, body: any, instance: int)
    log.info(f'#{instance} {headers} body={body}')
    # to set status, headers and body, return them in an event envelope
    result = EventEnvelope().set_header('hello', 'world').set_body(body)
    for h in headers:
        result.set_header(h, headers[h])
    return result


def main():
    # you can register a method of a class
    platform.register('hello.world.1', Hi().hello, 5)
    # or register a function
    platform.register('hello.world.2', hello, 10)
    # you can also create an alias for the same service
    platform.register('hello.world', Hi().hello, 5)

    # demonstrate sending asynchronously. Note that key-values in the headers will be encoded as strings
    po.send('hello.world.1', headers={'one': 1}, body='hello world one')
    po.send('hello.world.2', headers={'two': 2}, body='hello world two')

    # demonstrate a RPC request
    try:
        result = po.request('hello.world.2', 2.0, headers={'some_key': 'some_value'}, body='hello world')
        if isinstance(result, EventEnvelope):
            log.info('Received RPC response:')
            log.info(f'HEADERS = {result.get_headers()}, BODY = {result.get_body()}, STATUS = {result.get_status()}, '
                     f'EXEC = {result.get_exec_time()} ms, ROUND TRIP = {result.get_round_trip()} ms')
    except TimeoutError as e:
        log.error(f'Exception: {e}')

    # illustrate parallel RPC requests
    event_list = list()
    event_list.append(EventEnvelope().set_to('hello.world.1').set_body("first request"))
    event_list.append(EventEnvelope().set_to('hello.world.2').set_body("second request"))
    try:
        result = po.parallel_request(event_list, 2.0)
        if isinstance(result, list):
            log.info(f'Received {len(result)} parallel RPC responses:')
            for res in result:
                log.info(f'HEADERS = {res.get_headers()}, BODY = {res.get_body()}, STATUS = {res.get_status()}, '
                         f'EXEC = {res.get_exec_time()} ms, ROUND TRIP = {res.get_round_trip()} ms')
    except TimeoutError as e:
        log.error(f'Exception: {e}')

    # demonstrate deferred delivery
    po.send_later('hello.world.1', headers={'hello': 'world'}, body='this message arrives 5 seconds later', seconds=5.0)

    def life_cycle_listener(headers: dict, body: any):
        # Detect when cloud is ready
        log.info(f'Cloud life cycle event - {headers}')
        if 'type' in headers and 'ready' == headers['type']:
            # Demonstrate broadcast feature:
            # To test this feature, please run multiple instances of this demo
            po.broadcast("hello.world", body="this is a broadcast message from " + platform.get_origin())

    platform.register('my.cloud.status', life_cycle_listener, is_private=True)
    platform.subscribe_life_cycle('my.cloud.status')

    # Connect to the network
    platform.connect_to_cloud()
    #
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()
