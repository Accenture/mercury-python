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

import os
import logging
from mercury.system.singleton import Singleton


def get_level(level: str):
    # DEBUG | INFO | WARN | ERROR | FATAL
    result = logging.INFO
    if level is not None:
        if level.upper() == 'DEBUG':
            result = logging.DEBUG
        elif level.upper() == 'ERROR':
            result = logging.ERROR
        elif level.upper() == 'WARN' or level.upper() == 'WARNING':
            result = logging.WARNING
        elif level.upper() == 'FATAL':
            result = logging.CRITICAL
    return result


@Singleton
class LoggingService:

    def __init__(self, log_level='INFO'):
        self.logger = logging.getLogger()
        env_log_level = os.getenv('LOG_LEVEL')
        level = get_level(env_log_level) if env_log_level is not None else get_level(log_level)
        self.logger.setLevel(level)
        formatter = logging.Formatter(fmt='%(asctime)s %(levelname)s %(message)s [%(filename)s:%(lineno)s]')
        formatter.default_msec_format = '%s.%03d'
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        ch.setLevel(level)
        self.logger.addHandler(ch)

    def get_logger(self):
        return self.logger
