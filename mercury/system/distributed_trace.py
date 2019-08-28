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

from mercury.system.models import EventEnvelope


class DistributedTrace:

    def __init__(self, platform, dt_processor):
        self.platform = platform
        self.log = platform.log
        self._dt_processor = dt_processor
        self._dt_last_check = None
        self._dt_found = False

    def logger(self, event: EventEnvelope):
        if isinstance(event, EventEnvelope):
            self.log.info('trace=' + str(event.get_headers()) + ', annotations=' + str(event.get_body()))
            # forward to user provided distributed trace logger if any
            current_time = time.time()
            if self._dt_last_check is None or current_time - self._dt_last_check > 5.0:
                self._dt_last_check = current_time
                self._dt_found = self.platform.exists(self._dt_processor)
            if self._dt_found:
                te = EventEnvelope()
                te.set_to(self._dt_processor).set_body(event.get_body())
                for h in event.get_headers():
                    te.set_header(h, event.get_header(h))
                self.platform.send_event(te)