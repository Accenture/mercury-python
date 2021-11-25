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

import time

from mercury.platform import Platform
from mercury.system.object_stream import ObjectStreamIO, ObjectStreamWriter, ObjectStreamReader


def main():
    platform = Platform()
    log = platform.get_logger()
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

    #
    # You can create a new I/O stream using ObjectStreamIO.
    # This requires a live connection to the language connector.
    #
    stream = ObjectStreamIO(10)

    in_stream_id = stream.get_input_stream()
    out_stream_id = stream.get_output_stream()

    output_stream = ObjectStreamWriter(out_stream_id)
    input_stream = ObjectStreamReader(in_stream_id)

    for i in range(100):
        output_stream.write('hello world '+str(i))

    #
    # if output stream is not closed, input will timeout
    # Therefore, please use try-except for TimeoutError in the iterator for-loop below.
    #
    output_stream.close()

    for block in input_stream.read(5.0):
        if block is None:
            log.info("EOF")
        else:
            log.info(block)

    input_stream.close()
    #
    # this will keep the main thread running in the background
    # so we can use Control-C or KILL signal to stop the application
    platform.run_forever()


if __name__ == '__main__':
    main()
