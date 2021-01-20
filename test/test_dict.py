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

import unittest

from mercury.system.dict_util import MultiLevelDict


class TestDict(unittest.TestCase):

    def test_multi_level_map(self):
        # mixed dict and list
        mix_path = 'hello.world[0].headers[0]'
        value = 'hello world'
        m1 = MultiLevelDict()
        m1.set_element(mix_path, value)
        m1_flatmap = m1.get_flat_map(m1.get_dict())
        self.assertEqual(value, m1_flatmap.get(mix_path))
        self.assertEqual(m1_flatmap.get(mix_path), m1.get_element(mix_path))
        # nested arrays
        data = {'a': {'b': [1, 2, 3, [4]]}}
        mm = MultiLevelDict(data)
        self.assertEqual(4, mm.get_element('a.b[3][0]'))
        # verify set_element method
        composite_path = 'a.b[3][4][1]'
        mm.set_element(composite_path, value)
        self.assertEqual(value, mm.get_element(composite_path))
        # test flatten map
        flat_map = mm.get_flat_map(mm.get_dict())
        m2 = MultiLevelDict()
        for k in flat_map:
            m2.set_element(k, flat_map.get(k))
        # the original and the reconstructed dictionaries must match
        self.assertEqual(mm.get_dict(), m2.get_dict())
        # the individual values must match using two different retrieval methods
        for k in flat_map:
            self.assertEqual(flat_map.get(k), m2.get_element(k))
        has_error = False
        try:
            mm.set_element('this.is.invalid[0', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('missing end bracket' in str(e))
        self.assertTrue(has_error)
        has_error = False
        try:
            mm.set_element('this.is.invalid[0][', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('missing end bracket' in str(e))
        self.assertTrue(has_error)
        has_error = False
        try:
            mm.set_element('this.is.invalid[0][x', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('missing end bracket' in str(e))
        self.assertTrue(has_error)
        has_error = False
        try:
            mm.set_element('this.is.invalid[0][1', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('missing end bracket' in str(e))
        self.assertTrue(has_error)
        has_error = False
        try:
            mm.set_element('this.is.invalid[0][1][x]', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('indexes must be digits' in str(e))
        self.assertTrue(has_error)
        has_error = False
        try:
            mm.set_element('this.is.invalid 0][1][x]', value)
        except ValueError as e:
            has_error = True
            self.assertTrue('missing start bracket' in str(e))
        self.assertTrue(has_error)
