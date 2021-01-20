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
from threading import Lock


class Throttle:
    #
    # DO NOT CHANGE this multiplier value. It has been tested with computers of different performance.
    #
    MULTIPLIER = 20

    def __init__(self, test_file: str, log=None):
        self.transactions = list()
        self.interval = 1.0 / self.MULTIPLIER
        self.test_file = test_file
        self.tps = self.test_file_io()
        self.batch_size = int(self.tps / self.MULTIPLIER)
        self.log = log
        self.lock = Lock()

    def get_tps(self):
        # estimated TPS is max file write speed / 2
        return int(self.tps / 2)

    def test_file_io(self):
        open(self.test_file, 'w').close()
        sample = "123456789."
        s = ''
        for i in range(10):
            s += sample

        begin = time.time()
        n = 0
        while time.time() - begin < 1.0:
            with open(self.test_file, 'ab') as f:
                f.write(bytes(s, 'utf-8'))
            n += 1
        return n

    def regulate_rate(self, seq):
        self.lock.acquire()
        now = time.time()
        self.transactions.append(now)
        while len(self.transactions) > self.batch_size:
            self.transactions.pop(0)
        # evaluate the last batch of transactions
        if len(self.transactions) == self.batch_size:
            t1 = self.transactions[0]
            diff = now - t1
            if diff < self.interval:
                timer = self.interval - diff
                if timer > 0:
                    if self.log:
                        self.log.debug("Reduce rate for "+str(round(timer, 3))+" seconds at seq-"+str(seq))
                    time.sleep(timer)
                else:
                    if self.log:
                        self.log.debug("Slowing down at seq "+str(seq))
                    time.sleep(0.001)
                self.transactions.clear()
        self.lock.release()
