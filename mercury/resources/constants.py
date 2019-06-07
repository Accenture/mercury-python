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

from mercury.system.singleton import Singleton


@Singleton
class AppConfig:

    #
    # Normally, language connector should be a sidecar in the same memory space.
    # Therefore the url is ws://127.0.0.1:8090/ws/lang
    #
    # In case multiple python applications want to share the same set of
    # language connectors, you may change the NETWORK_CONNECTOR to a list of
    # comma separated URLs
    #
    NETWORK_CONNECTOR = 'ws://127.0.0.1:8090/ws/lang'
    API_KEY = 'cb21eba8-3dcd-4553-8ef6-165256be5b4b'
    #
    # temporary work directory
    # (for cloud native apps, local file system must be considered transient)
    #
    WORK_DIRECTORY = '/tmp/python'
    #
    # Logger will write logs to the local file system if LOG_FILE is provided.
    # To log to console only, set it to None.
    #
    # LOG_FILE = 'mercury'
    LOG_FILE = None
    #
    # DEBUG | INFO | WARN | ERROR | FATAL
    #
    LOG_LEVEL = 'INFO'

    #
    # Max number of threads for ThreadPoolExecutor
    #
    MAX_THREADS = 200
