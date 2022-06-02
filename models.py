#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018-2022 Accenture Technology
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

import uuid
import msgpack
import time


class AppException(Exception):

    def __init__(self, status: int, message: str):
        Exception.__init__(self, {'status': status, 'message': message})
        self.status = status if isinstance(status, int) else 500
        self.message = str(message)

    def get_status(self):
        return self.status

    def get_message(self):
        return self.message


class AsyncHttpRequest:
    """
    Convenient wrapper for HTTP event from rest-automation.
    May also be used to create HTTP events for unit tests.
    """

    def __init__(self, data: dict = None):
        self.method = None
        self.query_string = None
        self.url = None
        self.ip = None
        self.upload = None
        self.headers = dict()
        self.query_params = dict()
        self.path_params = dict()
        self.cookies = dict()
        self.session = dict()
        self.body = None
        self.stream_route = None
        self.file_name = None
        self.relay = None
        self.trust_all_cert = False
        self.https = False
        self.size = -1
        self.timeout_seconds = -1
        if data is not None:
            if isinstance(data, dict):
                self.from_map(data)
            else:
                raise ValueError('Input should be a dictionary containing a HTTP request')

    def get_method(self):
        return self.method

    def set_method(self, method):
        self.method = method
        return self

    def get_query_string(self):
        return self.query_string

    def set_query_string(self, query_string):
        self.query_string = query_string
        return self

    def get_url(self):
        return self.url

    def set_url(self, url):
        self.url = url
        return self

    def get_ip(self):
        return self.ip

    def set_ip(self, ip):
        self.ip = ip
        return self

    def get_upload_tag(self):
        return self.upload

    def set_upload_tag(self, value: str):
        self.upload = value
        return self

    def set_header(self, key: str, value: any):
        self.headers[key.lower()] = value if isinstance(value, str) else str(value)
        return self

    def get_header(self, key: str):
        lk = key.lower()
        if lk in self.headers:
            return self.headers[lk]
        else:
            return None

    def get_headers(self):
        return self.headers

    def set_query_param(self, key: str, value: any):
        # query parameter can be a str or list of str
        self.query_params[key.lower()] = value if isinstance(value, str) or isinstance(value, list) else str(value)
        return self

    def get_query_param(self, key: str):
        lk = key.lower()
        if lk in self.query_params:
            return self.query_params[lk]
        else:
            return None

    def get_query_params(self):
        return self.query_params

    def set_path_param(self, key: str, value: str):
        self.path_params[key.lower()] = value
        return self

    def get_path_param(self, key: str):
        lk = key.lower()
        if lk in self.path_params:
            return self.path_params[lk]
        else:
            return None

    def get_path_params(self):
        return self.path_params

    def set_cookie(self, key: str, value: str):
        self.cookies[key.lower()] = value
        return self

    def get_cookie(self, key: str):
        lk = key.lower()
        if lk in self.cookies:
            return self.cookies[lk]
        else:
            return None

    def get_cookies(self):
        return self.cookies

    def set_session_info(self, key: str, value: str):
        self.session[key.lower()] = value
        return self

    def get_session_info(self, key: str):
        lk = key.lower()
        if lk in self.session:
            return self.session[lk]
        else:
            return None

    def get_session(self):
        return self.session

    def set_body(self, body: any):
        self.body = body
        return self

    def get_body(self):
        return self.body

    def get_stream_route(self):
        return self.stream_route

    def set_stream_route(self, value: str):
        self.stream_route = value
        return self

    def get_file_name(self):
        return self.file_name

    def set_file_name(self, value: str):
        self.file_name = value
        return self

    def get_relay(self):
        return self.relay

    def set_relay(self, value: str):
        self.relay = value
        return self

    def set_trust_all_cert(self, trust_all_cert: bool):
        self.trust_all_cert = trust_all_cert
        return self

    def is_trust_all_cert(self):
        return self.trust_all_cert

    def set_secure(self, https: bool):
        self.https = https
        return self

    def is_secure(self):
        return self.https

    def get_size(self):
        return self.size

    def set_size(self, value: int):
        if isinstance(value, int):
            self.size = value
            return self
        else:
            raise ValueError('size must be int')

    def get_timeout_seconds(self):
        return self.timeout_seconds

    def set_timeout_seconds(self, value: int):
        if isinstance(value, int):
            self.timeout_seconds = value
            return self
        else:
            raise ValueError('timeout_seconds must be int')

    def to_map(self):
        result = dict()
        result['headers'] = dict() if not self.headers else self.headers
        result['cookies'] = dict() if not self.cookies else self.cookies
        result['session'] = dict() if not self.session else self.session
        result['https'] = self.https
        if self.method:
            result['method'] = self.method
        if self.ip:
            result['ip'] = self.ip
        if self.url:
            result['url'] = self.url
        if self.timeout_seconds:
            result['timeout'] = self.timeout_seconds
        if self.file_name:
            result['filename'] = self.file_name
        if self.size:
            result['size'] = self.size
        if self.stream_route:
            result['stream'] = self.stream_route
        if self.body:
            result['body'] = self.body
        if self.query_string:
            result['query'] = self.query_string
        if self.upload:
            result['upload'] = self.upload
        if len(self.path_params) > 0 or len(self.query_params):
            parameters = dict()
            if len(self.path_params) > 0:
                parameters['path'] = self.path_params
            if len(self.query_params) > 0:
                parameters['query'] = self.query_params
            result['parameters'] = parameters
        #
        # Optional HTTP host name in the "relay" field
        #
        # This is used by the rest-automation "async.http.request" service
        # when forwarding HTTP request to a target HTTP endpoint.
        #
        if self.relay:
            result['relay'] = self.relay
            result['trust_all_cert'] = self.trust_all_cert
        return result

    def from_map(self, data: dict):
        if 'headers' in data and isinstance(data['headers'], dict):
            self.headers = data['headers']
        if 'cookies' in data and isinstance(data['cookies'], dict):
            self.cookies = data['cookies']
        if 'session' in data and isinstance(data['session'], dict):
            self.session = data['session']
        if 'method' in data:
            self.method = data['method']
        if 'ip' in data:
            self.ip = data['ip']
        if 'url' in data:
            self.url = data['url']
        if 'timeout' in data and isinstance(data['timeout'], int):
            self.timeout_seconds = data['timeout']
        if 'filename' in data:
            self.file_name = data['filename']
        if 'size' in data:
            self.size = data['size']
        if 'stream' in data:
            self.stream_route = data['stream']
        if 'body' in data:
            self.body = data['body']
        if 'query' in data:
            self.query_string = data['query']
        if 'https' in data:
            self.https = data['https']
        if 'relay' in data:
            self.relay = data['relay']
            self.trust_all_cert = data['trust_all_cert'] if 'trust_all_cert' in data else False
        if 'upload' in data:
            self.upload = data['upload']
        if 'parameters' in data:
            parameters = data['parameters']
            if 'query' in parameters:
                self.query_params = parameters['query']
            if 'path' in parameters:
                self.path_params = parameters['path']
        return self


class TraceInfo:

    def __init__(self, route: str, trace_id: str, path: str):
        self._route = str(route)
        self._start_time = self._get_timestamp()
        self._annotations = {}
        if trace_id is None:
            self._id = None
            self._path = None
        else:
            self._id = str(trace_id)
            self._path = "?" if path is None else str(path)

    def get_route(self):
        return self._route

    def get_id(self):
        return self._id

    def get_path(self):
        return self._path

    def get_start_time(self):
        return self._start_time

    def get_annotations(self):
        return self._annotations

    def annotate(self, key: str, value: str):
        self._annotations[str(key)] = str(value)

    @staticmethod
    def _get_timestamp():
        seconds = time.time()
        utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
        ms = (str(round(seconds - int(seconds), 3)) + '000')[1:5]
        return utc + ms + 'Z'


class EventEnvelope:

    def __init__(self):
        self.event_id = 'py'+str(uuid.uuid4()).replace('-', '')
        self.headers = dict()
        self.body = None
        self.status = None
        self.to = None
        self.sender = None
        self.reply_to = None
        self.extra = None
        self.correlation_id = None
        self.trace_id = None
        self.trace_path = None
        self.broadcast = False
        self.exec_time = -1.0
        self.round_trip = -1.0

    def set_event_id(self, event_id: str):
        if isinstance(event_id, str):
            self.event_id = event_id
        else:
            raise ValueError('id must be str')
        return self

    def get_event_id(self):
        return self.event_id

    def set_to(self, to: str):
        self.to = str(to)
        return self

    def set_from(self, sender: str):
        self.sender = str(sender)
        return self

    def get_to(self):
        return self.to

    def get_from(self):
        return self.sender

    def get_status(self):
        return self.status if self.status else 200

    def set_status(self, status: int):
        if isinstance(status, int):
            self.status = status
        else:
            raise ValueError('status must be int')
        return self

    def set_reply_to(self, reply_to: str, me=False):
        if isinstance(reply_to, str):
            self.reply_to = '->' + reply_to if me else reply_to
        else:
            raise ValueError('reply_to must be str')
        return self

    def get_reply_to(self):
        return self.reply_to

    def set_extra(self, extra: str):
        if isinstance(extra, str):
            self.extra = extra
        else:
            raise ValueError('extra must be str')
        return self

    def add_tag(self, key: str, value: str = ''):
        if key and isinstance(key, str) and len(key) > 0:
            m = extra_to_key_values(self.extra)
            m[key] = value if isinstance(value, str) else ''
            self.extra = map_to_string(m)
        return self

    def remove_tag(self, key: str):
        if key and isinstance(key, str) and len(key) > 0:
            m = extra_to_key_values(self.extra)
            if key in m:
                del m[key]
            self.extra = map_to_string(m)
        return self

    def get_tag(self, key: str):
        if key and isinstance(key, str) and len(key) > 0:
            m = extra_to_key_values(self.extra)
            return m[key] if key in m else None
        else:
            return None

    def get_extra(self):
        return self.extra

    def set_correlation_id(self, correlation_id: str):
        self.correlation_id = correlation_id if isinstance(correlation_id, str) else str(correlation_id)
        return self

    def get_correlation_id(self):
        return self.correlation_id

    def set_trace(self, trace_id: str, trace_path: str):
        self.trace_id = trace_id
        self.trace_path = trace_path
        return self

    def get_trace_id(self):
        return self.trace_id

    def get_trace_path(self):
        return self.trace_path

    def set_headers(self, headers: dict):
        if isinstance(headers, dict):
            self.headers = headers
        return self

    def set_header(self, key: str, value: any):
        self.headers[key] = value if isinstance(value, str) else str(value)
        return self

    def get_header(self, key: str):
        if key in self.headers:
            return self.headers[key]
        else:
            return None

    def get_headers(self):
        return self.headers

    def set_body(self, body: any):
        self.body = body
        return self

    def get_body(self):
        return self.body

    def set_broadcast(self, broadcast: bool):
        self.broadcast = broadcast
        return self

    def is_broadcast(self):
        return self.broadcast

    def get_exec_time(self):
        return self.exec_time

    def set_exec_time(self, exec_time):
        self.exec_time = round(exec_time, 3)
        return self

    def get_round_trip(self):
        return self.round_trip

    def set_round_trip(self, round_trip):
        self.round_trip = round(round_trip, 3)
        return self

    def to_map(self):
        result = dict()
        if self.to:
            result['to'] = self.to
        if self.sender:
            result['from'] = self.sender
        result['headers'] = dict() if not self.headers else self.headers
        result['id'] = self.event_id
        if self.body is not None:
            result['body'] = self.body
        if self.reply_to:
            result['reply_to'] = self.reply_to
        if self.extra:
            result['extra'] = self.extra
        if self.correlation_id:
            result['cid'] = self.correlation_id
        if self.trace_id and self.trace_path:
            result['trace_id'] = self.trace_id
            result['trace_path'] = self.trace_path
        if self.broadcast:
            result['broadcast'] = True
        if self.status:
            result['status'] = self.status
        if self.exec_time >= 0:
            result['exec_time'] = self.exec_time
        if self.round_trip >= 0:
            result['round_trip'] = self.round_trip
        return result

    def from_map(self, data: dict):
        if 'id' in data and isinstance(data['id'], str):
            self.event_id = data['id']
        if 'to' in data and isinstance(data['to'], str):
            self.to = data['to']
        if 'from' in data and isinstance(data['from'], str):
            self.sender = data['from']
        if 'headers' in data and isinstance(data['headers'], dict):
            self.headers = data['headers']
        if 'body' in data:
            self.body = data['body']
        if 'reply_to' in data and isinstance(data['reply_to'], str):
            self.reply_to = data['reply_to']
        if 'extra' in data and isinstance(data['extra'], str):
            self.extra = data['extra']
        if 'cid' in data:
            self.correlation_id = data['cid']
        if 'trace_id' in data and 'trace_path' in data:
            self.trace_id = data['trace_id']
            self.trace_path = data['trace_path']
        if 'status' in data:
            self.status = data['status']
        if 'broadcast' in data:
            self.broadcast = data['broadcast']
        if 'exec_time' in data:
            self.set_exec_time(data['exec_time'])
        if 'round_trip' in data:
            self.set_round_trip(data['round_trip'])
        return self

    def to_bytes(self):
        return msgpack.packb(self.to_map(), use_bin_type=True)

    def from_bytes(self, data):
        return self.from_map(msgpack.unpackb(data, raw=False))


def extra_to_key_values(extra: str) -> dict:
    result = dict()
    if extra and isinstance(extra, str) and len(extra) > 0:
        elements = [x for x in extra.split('|') if x]
        for kv in elements:
            if '=' in kv:
                sep = kv.index('=')
                k = kv[0:sep]
                v = kv[sep+1:]
                result[k] = v
            else:
                result[kv] = ''
    return result


def map_to_string(m: dict) -> str:
    result = ''
    if len(m) > 0:
        for k in m:
            result = result + k
            v = m[k]
            if v and len(v) > 0:
                result = result + '=' + v
            result = result + '|'
        result = result[0: len(result)-1]
    return result
