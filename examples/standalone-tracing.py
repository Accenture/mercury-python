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
import uuid

platform = Platform()
log = platform.get_logger()
po = PostOffice()


def tracing(headers: dict, body: any):
    # no instance parameter because this is a singleton
    log.info(f'TRACE {headers}')


def hello(headers: dict, body: any, instance: int):
    # regular function signature (headers: dict, body: any, instance: int)
    log.info(f'#{instance} got header={headers} body={body}')
    # as a demo, just echo the original payload
    return body


def main():
    # register a function
    platform.register('hello.world', hello, 10)
    platform.register('distributed.tracing', tracing, 10)

    # demonstrate sending asynchronously. Note that key-values in the headers will be encoded as strings
    po.send('hello.world', headers={'one': 1}, body='hello world one')
    po.send('hello.world', headers={'two': 2}, body='hello world two')

    # demonstrate a RPC request
    try:
        trace_id = str(uuid.uuid4()).replace('-', '')
        trace_path = 'GET /api/hello/world'
        event = EventEnvelope().set_to("hello.world").set_header('some_key', 'some_value').set_body('hello world')
        event.set_trace(trace_id, trace_path).set_from('this.demo')
        result = po.send_request(event, 2.0)
        if isinstance(result, EventEnvelope):
            log.info('Received RPC response:')
            log.info(f'HEADERS = {result.get_headers()}, BODY = {result.get_body()}, STATUS = {result.get_status()}, '
                     f'EXEC = {result.get_exec_time()} ms, ROUND TRIP = {result.get_round_trip()} ms')
    except TimeoutError as e:
        log.error(f'Exception: {e}')

    # demonstrate drop-n-forget
    for n in range(20):
        po.send('hello.world', body=f'drop-n-forget message {n}')

    # demonstrate deferred delivery
    po.send_later('hello.world', headers={'hello': 'world'}, body='this message arrives 5 seconds later', seconds=5.0)
    #
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()
