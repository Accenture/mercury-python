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

import logging
from mercury.system.singleton import Singleton


@Singleton
class LoggingService:

    def __init__(self, log_level='INFO'):
        # DEBUG | INFO | WARN | ERROR | FATAL
        level = logging.INFO
        if log_level.upper() == 'DEBUG':
            level = logging.DEBUG
        elif log_level.upper() == 'ERROR':
            level = logging.ERROR
        elif log_level.upper() == 'WARN':
            level = logging.WARNING
        elif log_level.upper() == 'FATAL':
            level = logging.CRITICAL
        self.logger = logging.getLogger()
        self.logger.setLevel(level)
        ch = logging.StreamHandler()
        ch.setLevel(level)
        formatter = logging.Formatter(fmt='%(asctime)s %(levelname)s %(message)s [%(filename)s:%(lineno)s]')
        formatter.default_msec_format = '%s.%03d'
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

    def get_logger(self):
        return self.logger
