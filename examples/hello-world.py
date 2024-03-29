#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2022 Accenture Technology
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from mercury.platform import Platform
from mercury.system.models import AsyncHttpRequest

platform = Platform()
log = platform.get_logger()


def hello(headers: dict, body: any, instance: int):
    if isinstance(body, dict):
        request = AsyncHttpRequest(body)
        # is this an HTTP request from REST automation?
        if request.get_method() is not None:
            log.info(f'#{instance} HTTP request: {request.get_method()} {request.get_url()}')
    # just print out the input onto the console
    log.info(f'#{instance} GOT headers = {headers}, body = {body}')
    # return the result as a dict so that it can be rendered as JSON, XML or HTML automatically by the REST endpoint
    return {'instance': instance, 'headers': headers, 'body': body}


def main():
    # this shows that we can register a route name for a function
    platform.register('hello.world', hello, 10)

    # Once it connects to the network, it is ready to serve requests
    platform.connect_to_cloud()
    #
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()

