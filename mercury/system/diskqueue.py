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

import io
import os
import msgpack

from mercury.system.utility import Utility


class ElasticQueue:

    DATA = b'\x01'
    EOF = b'\x00'
    QUEUE = "data-"
    MEMORY_BUFFER = 10
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self, queue_dir: str = None, queue_id: str = None):
        # print('-----------', queue_dir, queue_id)
        # automatically create queue directory
        if queue_dir is None or queue_id is None:
            raise ValueError('Missing queue_dir or queue_id')
        self.queue_id = queue_id
        if not os.path.exists(queue_dir):
            os.makedirs(queue_dir)
        self.util = Utility()
        self._dir = self.util.normalize_path(queue_dir + '/' + queue_id)
        self._empty = False
        self._create_dir = False
        self._memory = list()
        self._read_file_no = 1
        self._write_file_no = 1
        self._read_counter = 0
        self._write_counter = 0
        self._file = None
        self._peeked = None
        self.initialize()

    def get_id(self):
        return self.queue_id

    def initialize(self):
        if not self._empty:
            self._empty = True
            if os.path.exists(self._dir):
                self.util.cleanup_dir(self._dir, clear_dir=False)
                self._create_dir = False
            else:
                self._create_dir = True
            self._memory = list()
            self._read_file_no = 1
            self._write_file_no = 1
            self._read_counter = 0
            self._write_counter = 0

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        self.initialize()

    def is_closed(self):
        return self._file is None and self._write_counter == 0

    def destroy(self):
        self.close()
        if self.is_closed():
            self.util.cleanup_dir(self._dir)

    async def write(self, data: dict):
        if self._write_counter < self.MEMORY_BUFFER:
            self._memory.append(data)
            self._write_counter += 1
            self._empty = False
        else:
            if self._create_dir:
                self._create_dir = False
                os.makedirs(self._dir)
            filename = self.util.normalize_path(self._dir + '/' + self.QUEUE + str(self._write_file_no))
            if not os.path.exists(filename):
                open(filename, 'w').close()
            # pack data as bytes
            block = msgpack.packb(data, use_bin_type=True)
            file_size = os.path.getsize(filename)
            with open(filename, 'ab') as f:
                buffer = io.BytesIO()
                buffer.write(self.DATA)
                buffer.write(self.util.int_to_bytes(len(block)))
                buffer.write(block)
                file_size += len(block)
                if file_size > self.MAX_FILE_SIZE:
                    buffer.write(self.EOF)
                    self._write_file_no += 1
                f.write(buffer.getvalue())
                self._write_counter += 1
                self._empty = False

    def peek(self):
        if self._peeked is not None:
            return self._peeked
        self._peeked = self.read()
        return self._peeked

    def read(self):
        if self._peeked is not None:
            result = self._peeked
            self._peeked = None
            return result
        if self._read_counter >= self._write_counter:
            # catch up with writes and thus nothing to read
            self.close()
            return None
        if self._read_counter < self.MEMORY_BUFFER:
            data = self._memory.pop(0)
            if data is not None:
                self._read_counter += 1
            return data
        filename = self.util.normalize_path(self._dir + '/' + self.QUEUE + str(self._read_file_no))
        if self._file is None:
            if not os.path.exists(filename):
                return None
            self._file = open(filename, 'rb')
        # read control indicator
        ctl = self._file.read(1)
        if ctl is None:
            return None
        if ctl == self.EOF:
            # EOF - drop file and increment read sequence
            self._file.close()
            self._file = None
            os.remove(filename)
            self._read_file_no += 1
            return self.read()
        if ctl != self.DATA:
            raise ValueError("Corrupted queue for "+self.queue_id)
        # read data block size
        size = self._file.read(4)
        if size is None or len(size) != 4:
            raise ValueError("Corrupted queue for " + self.queue_id)
        block_size = self.util.bytes_to_int(size)
        block = self._file.read(block_size)
        if block is None or len(block) != block_size:
            raise ValueError("Corrupted queue for " + self.queue_id)
        self._read_counter += 1
        # unpack from bytes into the original data
        return msgpack.unpackb(block, raw=False)
