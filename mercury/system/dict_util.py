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

from mercury.system.utility import Utility


class MultiLevelDict:

    def __init__(self, data=None):
        self.util = Utility()
        self.normalized = False
        self.dataset = dict() if data is None else data
        if not isinstance(self.dataset, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))

    def get_dict(self):
        return self.dataset

    @staticmethod
    def is_digits(n: str):
        for i in n:
            if i < '0' or i > '9':
                return False
        return True

    @staticmethod
    def is_list_element(item: str):
        return '[' in item and item.endswith(']') and (not item.startswith('['))

    def set_element(self, composite_path: str, value: any, source_data: dict = None):
        if composite_path is None:
            raise ValueError('Missing composite_path')
        self.validate_composite_path_syntax(composite_path)
        data = self.dataset if source_data is None else source_data
        if not isinstance(data, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))
        segments = self.util.multi_split(composite_path, './')
        if len(segments) == 0:
            return
        current = data
        size = len(segments)
        n = 0
        composite = ''
        for p in segments:
            n += 1
            if self.is_list_element(p):
                sep = p.index('[')
                indexes = self._get_indexes(p[sep:])
                element = p[0:sep]
                parent = self.get_element(composite+element, source_data)
                if n == size:
                    if isinstance(parent, list):
                        self._set_list_element(indexes, parent, value)
                    else:
                        new_list = list()
                        self._set_list_element(indexes, new_list, value)
                        current[element] = new_list
                    break
                else:
                    if isinstance(parent, list):
                        next_dict = self.get_element(composite+p, source_data)
                        if isinstance(next_dict, dict):
                            current = next_dict
                        else:
                            m = dict()
                            self._set_list_element(indexes, parent, m)
                            current = m
            else:
                if n == size:
                    current[p] = value
                    break
                else:
                    if p in current and isinstance(current[p], dict):
                        current = current[p]
                    else:
                        next_map = dict()
                        current[p] = next_map
                        current = next_map
            composite = composite + p + '.'

    def _set_list_element(self, indexes: list, source_data: list, value: any):
        current = self._expand_list(indexes, source_data)
        size = len(indexes)
        for i in range(0, size):
            idx = indexes[i]
            if i == size - 1:
                current[idx] = value
            else:
                o = current[idx]
                if isinstance(o, list):
                    current = o

    @staticmethod
    def _expand_list(indexes: list, source_data: list):
        current = source_data
        size = len(indexes)
        for i in range(0, size):
            idx = indexes[i]
            if idx >= len(current):
                diff = idx - len(current)
                while diff >= 0:
                    current.append(None)
                    diff -= 1
            if i == size - 1:
                break
            o = current[idx]
            if isinstance(o, list):
                current = o
            else:
                new_list = list()
                current[idx] = new_list
                current = new_list
        return source_data

    @staticmethod
    def _is_composite(path: str):
        return True if '.' in path or '/' in path or '[' in path or ']' in path else False

    def _get_indexes(self, index_segment: str):
        result = list()
        indexes = self.util.multi_split(index_segment, '[]')
        for i in indexes:
            if self.is_digits(i):
                result.append(int(i))
            else:
                result.append(-1)
        return result

    @staticmethod
    def _get_list_element(indexes: list, source_data: list):
        if (not isinstance(indexes, list)) or (not isinstance(source_data, list)) \
                or len(indexes) == 0 or len(source_data) == 0:
            return None
        current = source_data
        n = 0
        size = len(indexes)
        for i in indexes:
            n += 1
            if not isinstance(i, int):
                return None
            if i < 0 or i >= len(current):
                break
            o = current[i]
            if n == size:
                return o
            if isinstance(o, list):
                current = o
            else:
                break
        return None

    def get_element(self, composite_path: str, source_data: dict = None):
        if composite_path is None:
            return None
        data = self.dataset if source_data is None else source_data
        if not isinstance(data, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))
        if len(data) == 0:
            return None
        # special case for top level element that is using composite itself
        if composite_path in data:
            return data[composite_path]
        if not self._is_composite(composite_path):
            return data[composite_path] if composite_path in data else None
        parts = self.util.multi_split(composite_path, './')
        current = dict(data)
        size = len(parts)
        n = 0
        for p in parts:
            n += 1
            if self.is_list_element(p):
                start = p.index('[')
                end = p.index(']', start)
                if end == -1:
                    break
                key = p[0: start]
                index = p[start+1: end].strip()
                if len(index) == 0 or not self.is_digits(index):
                    break
                if key in current:
                    next_list = current[key]
                    if isinstance(next_list, list):
                        indexes = self._get_indexes(p[start:])
                        next_result = self._get_list_element(indexes, next_list)
                        if n == size:
                            return next_result
                        if isinstance(next_result, dict):
                            current = next_result
                            continue
            else:
                if p in current:
                    next_dict = current[p]
                    if n == size:
                        return next_dict
                    elif isinstance(next_dict, dict):
                        current = next_dict
                        continue
            # item not found
            break
        return None

    def normalize_map(self):
        if not self.normalized:
            # do only once
            self.normalized = True
            flat_map = self.get_flat_map(self.dataset)
            result = dict()
            for k in flat_map:
                self.set_element(k, flat_map[k], result)
            self.dataset = result

    def get_flat_map(self, data: dict = None):
        if not isinstance(data, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))
        result = dict()
        self._get_flat_map(None, data, result)
        return result

    def _get_flat_map(self, prefix: any, src: dict, target: dict):
        for k in src:
            v = src[k]
            key = k if prefix is None else prefix + "." + k
            if isinstance(v, dict):
                self._get_flat_map(key, v, target)
            elif isinstance(v, list):
                self._get_flat_list(key, v, target)
            else:
                target[key] = v

    def _get_flat_list(self, prefix: str, src: list, target: dict):
        n = 0
        for v in src:
            key = prefix + "[" + str(n) + "]"
            n += 1
            if isinstance(v, dict):
                self._get_flat_map(key, v, target)
            elif isinstance(v, list):
                self._get_flat_list(key, v, target)
            else:
                target[key] = v

    def validate_composite_path_syntax(self, path: str):
        segments = self.util.multi_split(path, './')
        if len(segments) == 0:
            raise ValueError('Missing composite path')
        for s in segments:
            if '[' in s or ']' in s:
                if '[' not in s:
                    raise ValueError('Invalid composite path - missing start bracket')
                if not s.endswith(']'):
                    raise ValueError('Invalid composite path - missing end bracket')
                sep1 = s.index('[')
                sep2 = s.index(']')
                if sep2 < sep1:
                    raise ValueError('Invalid composite path - missing start bracket')
                start = False
                for c in s[sep1:]:
                    if c == '[':
                        if start:
                            raise ValueError('Invalid composite path - missing end bracket')
                        else:
                            start = True
                    elif c == ']':
                        if not start:
                            raise ValueError('Invalid composite path - duplicated end bracket')
                        else:
                            start = False
                    else:
                        if start:
                            if c < '0' or c > '9':
                                raise ValueError('Invalid composite path - indexes must be digits')
                        else:
                            raise ValueError('Invalid composite path - invalid indexes')
