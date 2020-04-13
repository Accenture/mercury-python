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


class MultiLevelDict:

    def __init__(self, data=None):
        self.normalized = False
        self.dataset = dict() if data is None else data
        if not isinstance(self.dataset, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))

    def get_dict(self):
        return self.dataset

    @staticmethod
    def get_parent_path(path):
        return path[0: path.rindex('.')] if '.' in path else None

    @staticmethod
    def is_digits(n: str):
        for i in n:
            if i < '0' or i > '9':
                return False
        return True

    @staticmethod
    def is_list_element(item: str):
        return '[' in item and item.endswith(']') and (not item.startswith('['))

    @staticmethod
    def _get_index(item: str):
        bracket_start = item.index('[')
        bracket_end = item.rindex(']')
        return int(item[bracket_start+1: bracket_end])

    def set_element(self, composite_path: str, value: any, source_data: dict = None):
        if composite_path is None:
            raise ValueError('Missing composite_path')
        data = self.dataset if source_data is None else source_data
        if not isinstance(data, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))
        parts = composite_path.split('.')
        size = len(parts)
        element = parts[size - 1]
        parent_path = self.get_parent_path(composite_path)
        current = self.get_element(composite_path, data)
        if current is not None:
            if self.is_list_element(element):
                parent = composite_path[0: composite_path.rindex('[')]
                n = self._get_index(element)
                cp = self.get_element(parent, data)
                if isinstance(cp, list):
                    cp_list = list(cp)
                    cp_list[n] = value
            else:
                if parent_path is None:
                    data[element] = value
                else:
                    o = self.get_element(parent_path, data)
                    if isinstance(o, dict):
                        o[element] = value
        else:
            if size == 1:
                if self.is_list_element(element):
                    list_element = element[0: element.rindex('[')]
                    parent = composite_path[0: composite_path.rindex('[')]
                    n = self._get_index(element)
                    cp = self.get_element(parent, data)
                    if cp is None:
                        n_list = list()
                        for ii in range(n):
                            n_list[ii] = None
                        n_list.append(value)
                        data[list_element] = n_list
                    elif isinstance(cp, list):
                        o_list = cp
                        if len(o_list) > 0:
                            o_list[n] = value
                        else:
                            for ii in range(n):
                                cp[ii] = None
                            cp.append(value)
                else:
                    data[element] = value
            else:
                if self.is_list_element(element):
                    list_element = element[0: element.rindex('[')]
                    parent = composite_path[0: composite_path.rindex('[')]
                    n = self._get_index(element)
                    cp = self.get_element(parent, data)
                    if cp is None:
                        n_list = list()
                        for _ in range(n):
                            n_list.append(None)
                        n_list.append(value)
                        o = self.get_element(parent_path, data)
                        if isinstance(o, dict):
                            o[list_element] = n_list
                        else:
                            c = dict()
                            c[list_element] = n_list
                            self.set_element(parent_path, c, data)
                    elif isinstance(cp, list):
                        if len(cp) == 0:
                            for _ in range(n):
                                cp.append(None)
                        cp.append(value)
                else:
                    o = self.get_element(parent_path, data)
                    if isinstance(o, dict):
                        o[element] = value
                    else:
                        c = dict()
                        c[element] = value
                        self.set_element(parent_path, c, data)

    def get_element(self, composite_path: str, source_data: dict = None):
        if composite_path is None:
            return None
        data = self.dataset if source_data is None else source_data
        if not isinstance(data, dict):
            raise ValueError('Invalid input - Expect: dict, Actual: '+str(type(data)))
        # special case for top level element that is using composite itself
        if composite_path in data:
            return data[composite_path]
        if ('.' not in composite_path) and ('[' not in composite_path):
            return data[composite_path] if composite_path in data else None
        parts = composite_path.split('.')
        o = dict(data)
        size = len(parts)
        n = 0
        for p in parts:
            n += 1
            if self.is_list_element(p):
                bracket_start = p.index('[')
                bracket_end = p.rindex(']')
                if bracket_start > bracket_end:
                    break

                key = p[0: bracket_start]
                index = p[bracket_start+1: bracket_end].strip()

                if len(index) == 0:
                    break
                if not self.is_digits(index):
                    break
                i = int(index)
                if key in o:
                    x = o[key]
                    if isinstance(x, list):
                        y = list(x)
                        if i >= len(y):
                            break
                        else:
                            if n == size:
                                return y[i]
                            elif isinstance(y[i], dict):
                                o = y[i]
                                continue
            else:
                if p in o:
                    x = o[p]
                    if n == size:
                        return x
                    elif isinstance(x, dict):
                        o = x
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
