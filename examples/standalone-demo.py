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


def hello(headers: dict, body: any, instance: int):
    # regular function signature (headers: dict, body: any, instance: int)
    log.info("#" + str(instance) + " got ---> " + str(headers) + " body=" + str(body))
    # as a demo, just echo the original payload
    return body


def main():
    # register a function
    platform.register('hello.world', hello, 10)

    # demonstrate sending asynchronously. Note that key-values in the headers will be encoded as strings
    po.send('hello.world', headers={'one': 1}, body='hello world one')
    po.send('hello.world', headers={'two': 2}, body='hello world two')

    # demonstrate a RPC request
    try:
        result = po.request('hello.world', 2.0, headers={'some_key': 'some_value'}, body='hello world')
        if isinstance(result, EventEnvelope):
            log.info('Received RPC response:')
            log.info("HEADERS = " + str(result.get_headers()) + ", BODY = " + str(result.get_body()) +
                     ", STATUS = " + str(result.get_status()) +
                     ", EXEC = " + str(result.get_exec_time()) +
                     ", ROUND TRIP = " + str(result.get_round_trip()) + "ms")
    except TimeoutError as e:
        log.info("Exception: " + str(e))

    # demonstrate drop-n-forget
    for n in range(20):
        po.send('hello.world', body='just a drop-n-forget message ' + str(n))

    # demonstrate deferred delivery
    po.send_later('hello.world', headers={'hello': 'world'}, body='this message arrives 5 seconds later', seconds=5.0)
    #
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()
