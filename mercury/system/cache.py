#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2020 Accenture Technology
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
import asyncio


class SimpleCache:

    def __init__(self, loop, log, timeout_seconds=10):
        if not isinstance(timeout_seconds, int):
            raise ValueError('timeout_seconds must be int')
        self._loop = loop
        self.log = log
        self.normal = True
        self.timeout = int(timeout_seconds)
        self.map = dict()
        self.log.info("Started")
        self._loop.create_task(self.auto_expire())

    def put(self, key: str, value: any):
        self.map[key] = (time.time(), value)
        return self

    def remove(self, key: str):
        if key in self.map:
            self.map.pop(key, None)
        return self

    def get(self, key: str):
        return self.map[key][1] if key in self.map else None

    async def auto_expire(self):
        now = time.time()
        deletion = list()
        for k in self.map:
            if now - self.map[k][0] > self.timeout:
                deletion.append(k)
        for k in deletion:
            self.remove(k)

        if self.normal:
            await asyncio.sleep(0.5)
            self._loop.create_task(self.auto_expire())
        else:
            self.log.info("Stopped")

    def stop(self):
        self.normal = False
