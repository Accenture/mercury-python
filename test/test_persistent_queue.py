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
import asyncio
from mercury.system.diskqueue import ElasticQueue


class TestDiskQueue(unittest.TestCase):

    def test_read_write(self):
        byte_value = b'0x01'
        total = 10
        queue = ElasticQueue(queue_dir='/tmp', queue_id='test')

        async def test_write():
            for n in range(total):
                await queue.write({'v': 'hello world', 'n': n, 'b': byte_value})

        loop = asyncio.get_event_loop()
        loop.run_until_complete(test_write())

        for i in range(total):
            s = queue.read()
            self.assertIsNotNone(s)
            self.assertTrue('n' in s)
            self.assertTrue('v' in s)
            self.assertTrue('b' in s)
            self.assertEqual(s['n'], i)
            self.assertEqual(s['b'], byte_value)

        queue.close()
        queue.destroy()
        self.assertTrue(queue.is_closed())
