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

platform = Platform()
log = platform.get_logger()


def my_trace_processor(headers: dict, body: any):
    #
    # Demonstrate user defined Distributed Trace processor
    # In this example, it prints the traces onto the console.
    # For production, you should save the trace and metrics into a database or search engine.
    #
    log.info(f'trace {headers} {body}')


def main():
    # we should register the custom trace processor as a singleton
    platform.register('distributed.trace.processor', my_trace_processor, 1)

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
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()
