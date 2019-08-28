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


def hello(headers: dict, body: any, instance: int):
    # just print out the input onto the console
    print("#"+str(instance), "GOT", "headers =", str(headers), "body =", str(body))
    # return the result as a dict so it can be rendered as JSON, XML or HTML automatically by the REST endpoint
    return {'instance': instance, 'headers': headers, 'body': body}


def main():
    platform = Platform()
    # this shows that we can register a route name for a function
    platform.register('hello.world', hello, 10)

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

    #
    # this will keep the main thread running in the background
    # so we can use Control-C or KILL signal to stop the application
    platform.run_forever()


if __name__ == '__main__':
    main()
