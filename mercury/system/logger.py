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

import logging
import os
from logging.handlers import RotatingFileHandler
from mercury.system.utility import Utility
from mercury.system.singleton import Singleton


@Singleton
class LoggingService:

    def __init__(self, log_dir='/tmp/log', log_file: str = None, log_level='INFO'):
        # automatically create log directory
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

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
        self.logger = logging.getLogger(log_file)
        self.logger.setLevel(level)
        ch = logging.StreamHandler()
        ch.setLevel(level)
        formatter = logging.Formatter(fmt='%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s')
        formatter.default_msec_format = '%s.%03d'
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

        if log_file is not None and not log_file.lower() == 'none':
            filename = Utility().normalize_path(log_dir + '/' + log_file) + '.log'
            fh = RotatingFileHandler(filename, maxBytes=1024 * 1024, backupCount=10)
            fh.setLevel(level)
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)

    def get_logger(self):
        return self.logger
