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

import json
import os
import yaml
from mercury.system.dict_util import MultiLevelDict


class ConfigReader:

    def __init__(self, filename=None):
        if filename is None:
            script_dir = os.path.dirname(os.path.realpath(__file__))
            parent = os.path.abspath(os.path.join(script_dir, os.pardir))
            resources = os.path.join(parent, 'resources')
            filename = os.path.join(resources, 'application.yml')

        if not isinstance(filename, str):
            raise ValueError('Invalid filename - Expect: str, Actual: '+str(type(filename)))
        if not os.path.exists(filename):
            raise ValueError('File '+filename+' does not exist')

        with open(filename, 'r') as f:
            if filename.endswith('.yml') or filename.endswith('.yaml'):
                data = yaml.safe_load(f)
            elif filename.endswith('.json'):
                data = json.loads(f.read())
            else:
                raise ValueError('Filename must end with .yml, .yaml or .json')
            self._data = MultiLevelDict(data)
            # normalize key-values such that these 2 cases are the same
            # Case 1 -
            # hello:
            #   world: some_value
            # Case 2 -
            # hello.world: some_value
            self._data.normalize_map()

    def get_dict(self):
        return self._data.get_dict()

    def get(self, key, default_value: any = None):
        result = self._data.get_element(key)
        return result if result is not None else default_value

    def get_property(self, key, default_value: any = None):
        result = self.get(key, default_value)
        return result if isinstance(result, str) else str(result)
