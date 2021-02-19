#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2021 Accenture Technology
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


def hello(headers: dict, body: any, instance: int):
    # regular function signature (headers: dict, body: any, instance: int)
    Platform().log.info("#"+str(instance)+" got ---> "+str(headers)+" body="+str(body))
    # as a demo, just echo the original payload
    return body


def main():
    platform = Platform()
    # register a function
    platform.register('hello.world', hello, 10)

    po = PostOffice()
    # demonstrate sending asynchronously. Note that key-values in the headers will be encoded as strings
    po.send('hello.world', headers={'one': 1}, body='hello world one')
    po.send('hello.world', headers={'two': 2}, body='hello world two')

    # demonstrate a RPC request
    try:
        result = po.request('hello.world', 2.0, headers={'some_key': 'some_value'}, body='hello world')
        if isinstance(result, EventEnvelope):
            print('Received RPC response:')
            print("HEADERS =", result.get_headers(), ", BODY =", result.get_body(),
                  ", STATUS =",  result.get_status(),
                  ", EXEC =", result.get_exec_time(), ", ROUND TRIP =", result.get_round_trip(), "ms")
    except TimeoutError as e:
        print("Exception: ", str(e))

    for n in range(10):
        po.send('hello.world', body='test message '+str(n))

    #
    # this will keep the main thread running in the background
    # so we can use Control-C or KILL signal to stop the application
    platform.run_forever()


if __name__ == '__main__':
    main()
