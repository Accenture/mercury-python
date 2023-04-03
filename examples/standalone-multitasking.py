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
from mercury.system.po import PostOffice

platform = Platform()
log = platform.get_logger()
po = PostOffice()

HELLO_MULTI_TASKING = "hello.multi.tasking"

#
# You can use this standalone multitasking demo as a template to create
# a simple load test runner
#
# Just put your integration test business logic in the "my_simple_function" place-holder.
#
# For example, you can read a CSV file in the "main" section
# and send each record to the concurrent function that will spin up
# multiple workers to process each record.
#
# Note that the "body" should be a dictionary of primitives like list and strings
#


def my_simple_function(headers: dict, body: any, instance: int):
    # insert your business logic here
    log.info(f'#{instance} got header={headers} body={body}')
    # since this function runs asynchronously, we can just return a dummy value
    return True


def main():
    # register a function - adjust the number workers to fit your concurrency need
    platform.register(HELLO_MULTI_TASKING, my_simple_function, 10)

    # demonstrate drop-n-forget
    for n in range(100):
        po.send(HELLO_MULTI_TASKING, body=f'drop-n-forget message {n}')
    #
    # This will keep the main thread running in the background.
    # We can use Control-C or KILL signal to stop the application.
    platform.run_forever()


if __name__ == '__main__':
    main()
