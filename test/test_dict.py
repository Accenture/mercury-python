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

import unittest

from mercury.system.dict_util import MultiLevelDict


class TestDict(unittest.TestCase):

    def test_multi_level_map(self):
        data = {'a': {'b': [1, 2, 3, [4]]}}
        mm = MultiLevelDict(data)
        self.assertEqual(4, mm.get_element('a.b[3][0]'))
        # verify set_element method
        composite_path = 'a.b[3][4][1]'
        value = 'hello world'
        mm.set_element(composite_path, value)
        self.assertEqual(value, mm.get_element(composite_path))
        try:
            mm.set_element('invalid[x]', value)
        except ValueError as e:
            self.assertTrue('indexes must be digits' in str(e))
