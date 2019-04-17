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


class Singleton:
    """
    This helper class wraps any Class into a singleton object.

    Enhanced from answer#81 of
    http://stackoverflow.com/questions/42558/python-and-the-singleton-pattern

    Example:

    TO DECLARE A SINGLETON CLASS

    from mercury.system.system import Singleton

    @Singleton
    class SystemConfig(BaseConfig):

        def __init__(self, *args, **kwargs):
            print "got some parameters", *args
            print "got some keyword parameters", **kwargs
            #
            # call the parent constructor if needed
            #
            super(self.__class__, self).__init__(*args, **kwargs)
            #

    TO "INSTANTIATE" A SINGLETON CLASS

    from sysconf.config import SystemConfig

    config = SystemConfig('hello world')
    another_config = SystemConfig('parameter to be ignored in 2nd instantiation')
    print "same instance of SystemConfig?", config = another_config

    """

    _instance = None

    def __init__(self, decorated):
        self._decorated = decorated

    def __call__(self, *args, **kwargs):
        if self._instance is None: self._instance = self._decorated(*args, **kwargs)
        return self._instance

