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

import asyncio
import concurrent.futures
import os
import sys
import signal
import time
import threading
import uuid
from asyncio import QueueEmpty
from queue import Queue, Empty

from mercury.system.config_util import ConfigReader
from mercury.system.connector import NetworkConnector
from mercury.system.distributed_trace import DistributedTrace
from mercury.system.diskqueue import ElasticQueue
from mercury.system.logger import LoggingService
from mercury.system.models import EventEnvelope, AppException, TraceInfo
from mercury.system.singleton import Singleton
from mercury.system.utility import Utility, FunctionType
from mercury.system.throttle import Throttle


class ServiceQueue:

    def __init__(self, loop, executor, queue, route, user_function, total_instances):
        self.platform = Platform()
        self.util = Utility()
        self.log = self.platform.log
        queue_dir = self.util.normalize_path(f'{self.platform.work_dir}/queues/{self.platform.get_origin()}')

        self.disk_queue = ElasticQueue(queue_dir=queue_dir, queue_id=route)
        self._loop = loop
        self._executor = executor
        self.queue = queue
        self.route = route
        self.user_function = user_function
        self.ready_queue = asyncio.Queue()
        self.worker_list = dict()
        self._peek_worker = None
        self._buffering = True
        self._interceptor = total_instances == 0
        self._singleton = True if total_instances < 1 else False
        self._loop.create_task(self.listen(total_instances))

    def peek_next_worker(self):
        if self._peek_worker is None:
            self._peek_worker = self._fetch_next_worker()
        return self._peek_worker

    def get_next_worker(self):
        if self._peek_worker is not None:
            result = self._peek_worker
            self._peek_worker = None
            return result
        return self._fetch_next_worker()

    def _fetch_next_worker(self):
        try:
            worker_number = self.ready_queue.get_nowait()
            if worker_number:
                self.ready_queue.task_done()
            return worker_number
        except QueueEmpty:
            return None

    def send_to_worker(self, item):
        worker_number = self.get_next_worker()
        if worker_number:
            wq = self.worker_list[worker_number]
            if wq:
                wq.put_nowait(item)
            else:
                self.log.error(f'Event for {self.route} dropped because worker #{worker_number} not found')
        else:
            self.log.error(f'Event for {self.route} dropped because there are no workers available')

    async def listen(self, total_instances):
        # create concurrent workers and
        total = 1 if self._singleton else total_instances
        for i in range(total):
            instance_number = i + 1
            worker_queue = asyncio.Queue()
            self.worker_list[instance_number] = worker_queue
            WorkerQueue(self._loop, self._executor, self.queue, worker_queue,
                        self.route, self.user_function, instance_number, self._singleton, self._interceptor)
            # populate the ready queue with an initial set of worker numbers
            await self.queue.put(instance_number)

        route_type = 'PRIVATE' if self.platform.route_is_private(self.route) else 'PUBLIC'
        # minimize logging for temporary inbox that starts with the "r" prefix
        s = 's' if total > 1 else ''
        if self._interceptor and self.util.is_inbox(self.route):
            self.log.debug(f'{route_type} {self.route} with {total} instance{s} started')
        else:
            self.log.info(f'{route_type} {self.route} with {total} instance{s} started')

        # listen for incoming events
        while True:
            event = await self.queue.get()
            self.queue.task_done()
            if event is None:
                break
            else:
                if isinstance(event, int):
                    # ready signal from a worker
                    await self.ready_queue.put(event)
                    if self._buffering:
                        buffered = self.disk_queue.read()
                        if buffered:
                            self.send_to_worker(buffered)
                        else:
                            # nothing buffered in disk queue
                            self._buffering = False
                            self.disk_queue.close()

                if isinstance(event, dict):
                    # it is a data item
                    if self._buffering:
                        # Once buffering is started, continue to spool items to disk to guarantee items in order
                        await self.disk_queue.write(event)

                    else:
                        w = self.peek_next_worker()
                        if w:
                            # Nothing buffered in disk queue. Find a worker to receive the item.
                            self.send_to_worker(event)
                        else:
                            # start buffered because there are no available workers
                            self._buffering = True
                            await self.disk_queue.write(event)

        # tell workers to stop
        for i in self.worker_list:
            wq = self.worker_list[i]
            wq.put_nowait(None)
        # destroy disk queue
        self.disk_queue.destroy()

        # minimize logging for temporary inbox that starts with the "r" prefix
        if self._interceptor and self.util.is_inbox(self.route):
            self.log.debug(f'{self.route} stopped')
        else:
            self.log.info(f'{self.route} stopped')


def _normalize_exception(cls: str, e: Exception):
    message = e.message if hasattr(e, 'message') else str(e)
    cls_name = cls + ': '
    return message if message.startswith(cls_name) else cls_name + message


class WorkerQueue:
    DISTRIBUTED_TRACING = 'distributed.tracing'

    def __init__(self, loop, executor, manager_queue, worker_queue, route, user_function, instance,
                 singleton, interceptor):
        self.platform = Platform()
        self.util = Utility()
        self.log = self.platform.log
        self._loop = loop
        self._executor = executor
        self.manager_queue = manager_queue
        self.worker_queue = worker_queue
        self.route = route
        # trace all routes except ws.outgoing
        normal_service = not (interceptor and self.util.is_inbox(route))
        self.tracing = normal_service and route != 'ws.outgoing'
        self.user_function = user_function
        self.instance = instance
        self.singleton = singleton
        self.interceptor = interceptor
        self._loop.create_task(self.listen())
        self.log.debug(f'{self.route} #{self.instance} started')

    async def listen(self):
        while True:
            event = await self.worker_queue.get()
            self.worker_queue.task_done()
            if event is None:
                break
            else:
                # Execute the user function in parallel
                if self.interceptor:
                    self._loop.run_in_executor(self._executor, self.handle_event, event, 0)
                elif self.singleton:
                    self._loop.run_in_executor(self._executor, self.handle_event, event, -1)
                else:
                    self._loop.run_in_executor(self._executor, self.handle_event, event, self.instance)
        self.log.debug(f'{self.route} #{self.instance} stopped')

    def handle_event(self, event, instance):
        headers = dict() if 'headers' not in event else event['headers']
        body = None if 'body' not in event else event['body']
        result = None
        error_code = None
        error_msg = None
        # start distributed tracing if the event contains trace_id and trace_path
        if 'trace_id' in event and 'trace_path' in event:
            self.platform.start_tracing(self.route, trace_id=event['trace_id'], trace_path=event['trace_path'])
        else:
            self.platform.start_tracing(self.route)
        # execute user function
        begin = end = time.perf_counter()
        has_error = False
        try:
            if instance == 0:
                # service is an interceptor. e.g. inbox for RPC call
                self.user_function(EventEnvelope().from_map(event))
            elif instance == -1:
                # service is a singleton
                result = self.user_function(headers, body)
            else:
                # service with multiple instances
                result = self.user_function(headers, body, instance)
            end = time.perf_counter()
        except AppException as e:
            has_error = True
            error_code = e.get_status()
            error_msg = _normalize_exception('AppException', e)
        except ValueError as e:
            has_error = True
            error_code = 400
            error_msg = _normalize_exception('ValueError', e)
        except Exception as e:
            has_error = True
            error_code = 500
            error_msg = _normalize_exception(type(e).__name__, e)

        # execution time is rounded to 3 decimal points
        exec_time = round((end - begin) * 1000, 3)

        if error_code:
            if 'reply_to' in event:
                # set exception as result
                result = EventEnvelope().set_status(error_code).set_body(error_msg)
            else:
                self.log.warn(f'Unhandled exception for {self.route} - code={error_code}, message={error_msg}')
        #
        # interceptor should not send regular response because it will forward the request to another function.
        # However, if error_code exists, the system will send the exception response.
        # This allows interceptor to simply throw exception to indicate an error case.
        #
        if 'reply_to' in event and (error_code or not self.interceptor):
            reply_to = event['reply_to']
            # in case this is an RPC call from within
            if reply_to.startswith('->'):
                reply_to = reply_to[2:]
            response = EventEnvelope().set_to(reply_to)
            if not error_code:
                response.set_exec_time(exec_time)
            if 'extra' in event:
                response.set_extra(event['extra'])
            if has_error:
                # adding the 'exception' tag would throw exception to the caller
                response.add_tag('exception')
            if 'cid' in event:
                response.set_correlation_id(event['cid'])
            if 'trace_id' in event and 'trace_path' in event:
                response.set_trace(event['trace_id'], event['trace_path'])
            if isinstance(result, EventEnvelope):
                for h in result.get_headers():
                    response.set_header(h, result.get_header(h))
                response.set_body(result.get_body())
                response.set_status(result.get_status())
            else:
                response.set_body(result)

            try:
                self.platform.send_event(response.set_from(self.route))
            except Exception as e:
                self.log.warn(f'Event dropped because {e}')

        # send tracing info to distributed trace logger
        trace_info = self.platform.stop_tracing()
        if self.tracing and trace_info is not None and isinstance(trace_info, TraceInfo) \
                and trace_info.get_id() is not None and trace_info.get_path() is not None \
                and self.platform.has_route(self.DISTRIBUTED_TRACING):
            dt = EventEnvelope().set_to(self.DISTRIBUTED_TRACING).set_body(trace_info.get_annotations())
            dt.set_header('origin', self.platform.get_origin())
            dt.set_header('id', trace_info.get_id()).set_header('path', trace_info.get_path())
            dt.set_header('service', self.route).set_header('start', trace_info.get_start_time())
            if 'from' in event:
                dt.set_header('from', event['from'])
            if not error_code:
                dt.set_header('success', 'true')
                dt.set_header('exec_time', exec_time)
            else:
                dt.set_header('success', 'false')
                dt.set_header('status', error_code)
                dt.set_header('exception', error_msg)
            self.platform.send_event(dt)

        self._loop.call_soon_threadsafe(self._ack)

    def _ack(self):
        self.manager_queue.put_nowait(self.instance)


class Inbox:

    def __init__(self, platform):
        self.start_time = time.perf_counter()
        self.temp_route = 'r.' + (''.join(str(uuid.uuid4()).split('-')))
        self.inbox_queue = Queue()
        self.platform = platform
        self.platform.register(self.temp_route, self.listener, 1, is_private=True)

    # inbox is an interceptor service which must be defined with the parameter "envelope" as below
    def listener(self, event: EventEnvelope):
        diff = time.perf_counter() - self.start_time
        event.set_round_trip(diff * 1000)
        self.inbox_queue.put(event)

    def get_route(self):
        return self.temp_route

    def get_queue(self):
        return self.inbox_queue

    def close(self):
        self.platform.release(self.temp_route)


@Singleton
class Platform:
    """
    Event system platform instance
    """
    SERVICE_QUERY = 'system.service.query'

    def __init__(self, config_file: str = None):
        if sys.version_info.major < 3:
            python_version = f'{sys.version_info.major}.{sys.version_info.minor}'
            raise RuntimeError(f'Requires python 3.6 and above. Actual: {python_version}')
        self.origin = 'py-' + (''.join(str(uuid.uuid4()).split('-')))
        self.config = ConfigReader(config_file)
        self.util = Utility()
        log_level = self.config.get_property('log.level')
        self._max_threads = self.config.get('max.threads')
        self.work_dir = self.config.get_property('work.directory')
        self.log = LoggingService(log_level).get_logger()
        self._loop = asyncio.new_event_loop()
        # DO NOT CHANGE 'distributed.trace.processor' which is an optional user defined trace aggregator
        my_tracer = DistributedTrace(self, 'distributed.trace.processor')
        my_nc = self.config.get_property('network.connector')
        self._cloud = NetworkConnector(self, my_tracer, self._loop, my_nc, self.origin)
        self._function_queues = dict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_threads)
        self.log.info(f'Concurrent thread pool = {self._max_threads}')
        #
        # Before we figure out how to solve blocking file I/O, we will regulate event output rate.
        #
        my_test_dir = self.util.normalize_path(f'{self.work_dir}/safe_to_delete_when_apps_stop')
        if not os.path.exists(my_test_dir):
            os.makedirs(my_test_dir, exist_ok=True)
        self._throttle = Throttle(self.util.normalize_path(f'{my_test_dir}/'+self.origin), log=self.log)
        self._seq = 0
        self.log.info(f'Estimated performance is {format(self._throttle.get_tps(), ",d")} events per second')
        self.running = True
        self.stopped = False
        # distributed trace sessions
        self._traces = {}
        self.trace_aggregation = True

        # start event loop in a new thread to avoid blocking the main thread
        def main_event_loop():
            self._loop.run_forever()
            self._loop.close()

        threading.Thread(target=main_event_loop).start()

    def get_origin(self):
        """
        Get the origin ID of this application instance

        Returns: origin ID

        """
        return self.origin

    def get_logger(self):
        """
        Get Logger

        Returns: logger instance

        """
        return self.log

    def is_trace_supported(self):
        return self.trace_aggregation

    def set_trace_support(self, enabled: bool = True):
        self.trace_aggregation = enabled
        status = 'ON' if enabled else 'OFF'
        self.log.info(f'Trace aggregation is {status}')

    def get_trace_id(self) -> str:
        """
        Get trace ID for a transaction

        Returns: trace ID

        """
        trace_info = self.get_trace()
        return trace_info.get_id() if trace_info is not None else None

    def get_trace(self) -> TraceInfo:
        """
        Get trace info for a transaction

        Returns: trace info

        """
        thread_id = threading.get_ident()
        return self._traces[thread_id] if thread_id in self._traces else None

    def annotate_trace(self, key: str, value: str) -> None:
        """
        Annotate a trace at the current point of a transaction

        Args:
            key: any key
            value: any value

        Returns: None

        """
        trace_info = self.get_trace()
        if trace_info is not None and isinstance(trace_info, TraceInfo):
            trace_info.annotate(key, value)

    def start_tracing(self, route: str, trace_id: str = None, trace_path: str = None) -> None:
        """
        This method is reserved for system use. DO NOT call this from a user application.

        Args:
            route: route name
            trace_id: id
            trace_path: path such as Method and URI

        Returns: None

        """
        thread_id = threading.get_ident()
        self._traces[thread_id] = TraceInfo(route, trace_id, trace_path)

    def stop_tracing(self) -> TraceInfo:
        """
        This method is reserved for system use. DO NOT call this from a user application.

        Returns: trace info

        """
        thread_id = threading.get_ident()
        if thread_id in self._traces:
            trace_info = self.get_trace()
            self._traces.pop(thread_id)
            return trace_info

    def run_forever(self) -> None:
        """
        Tell the platform to run in the background until user presses CTL-C or the application is stopped by admin

        Returns: None

        """
        def graceful_shutdown(signum, frame):
            self.running = False
            if frame is not None:
                self.log.warn('Control-C detected' if signal.SIGINT == signum else 'KILL signal detected')

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, graceful_shutdown)
            signal.signal(signal.SIGINT, graceful_shutdown)
            # keep the main thread running so CTL-C can be detected
            self.log.info('To stop this application, press Control-C')
            while self.running:
                time.sleep(0.1)
            # exit forever loop and ask platform to end event loop
            self.stop()
        else:
            raise ValueError('Unable to register Control-C and KILL signals because this is not the main thread')

    def register(self, route: str, user_function: any, total_instances: int = 1, is_private: bool = False) -> None:
        """
        Register a user function

        Args:
            route: ID of the function
            user_function: the lambda function given by you
            total_instances: 1 for singleton or more for concurrency
            is_private: true if internal function within this application instance

        Returns: None

        """
        self.util.validate_service_name(route)
        if not isinstance(total_instances, int):
            raise ValueError(f'Expect total_instances to be int, actual: {type(total_instances)}')
        if total_instances < 1:
            raise ValueError('total_instances must be at least 1')
        if total_instances > self._max_threads:
            raise ValueError(f'total_instances must not exceed max threads of {self._max_threads}')
        function_type = self.util.get_function_type(user_function)
        if function_type == FunctionType.NOT_SUPPORTED:
            raise ValueError('Function signature should be (headers: dict, body: any, instance: int) or ' +
                             '(headers: dict, body: any) or (event: EventEnvelope)')
        if route in self._function_queues:
            self.log.warn(f'{route} will be reloaded')
            self.release(route)
        queue = asyncio.Queue()
        if function_type == FunctionType.INTERCEPTOR:
            self._function_queues[route] = {'queue': queue, 'private': is_private, 'instances': 1}
            ServiceQueue(self._loop, self._executor, queue, route, user_function, 0)
        elif function_type == FunctionType.REGULAR:
            self._function_queues[route] = {'queue': queue, 'private': is_private, 'instances': total_instances}
            ServiceQueue(self._loop, self._executor, queue, route, user_function, total_instances)
        else:
            # function_type == FunctionType.SINGLETON
            self._function_queues[route] = {'queue': queue, 'private': is_private, 'instances': 1}
            ServiceQueue(self._loop, self._executor, queue, route, user_function, -1)
        # advertise the new route to the network
        if self._cloud.is_ready() and not is_private:
            self._cloud.send_payload({'type': 'add', 'route': route})

    def cloud_ready(self):
        return self._cloud.is_ready()

    def subscribe_life_cycle(self, callback: str):
        self._cloud.subscribe_life_cycle(callback)

    def unsubscribe_life_cycle(self, callback: str):
        self._cloud.unsubscribe_life_cycle(callback)

    def release(self, route: str) -> None:
        # this will un-register a route
        if not isinstance(route, str):
            raise ValueError(f'Expect route to be str, actual: {type(route)}')
        if route not in self._function_queues:
            raise ValueError(f'route {route} not found')
        # advertise the deleted route to the network
        if self._cloud.is_ready() and self.route_is_private(route):
            self._cloud.send_payload({'type': 'remove', 'route': route})
        self._remove_route(route)

    def has_route(self, route: str) -> bool:
        if not isinstance(route, str):
            raise ValueError(f'Expect route to be str, actual: {type(route)}')
        return route in self._function_queues

    def get_routes(self, options: str = 'all'):
        result = list()
        if 'public' == options:
            for route in self._function_queues:
                if not self.route_is_private(route):
                    result.append(route)
            return result
        elif 'private' == options:
            for route in self._function_queues:
                if self.route_is_private(route):
                    result.append(route)
            return result
        elif 'all' == options:
            return list(self._function_queues.keys())
        else:
            return result

    def route_is_private(self, route: str) -> bool:
        config = self._function_queues[route]
        if config and 'private' in config:
            return config['private']
        else:
            return False

    def route_instances(self, route: str) -> int:
        config = self._function_queues[route]
        if config and 'instances' in config:
            return config['instances']
        else:
            return 0

    def send_parallel_requests(self, events: list, timeout_seconds: float):
        timeout_value = self.util.get_float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError('timeout value in seconds must be positive number')
        if not isinstance(events, list):
            raise ValueError('events must be a list of EventEnvelope')
        if len(events) == 0:
            raise ValueError('event list is empty')
        if len(events) == 1:
            result = list()
            result.append(self.send_request(events[0], timeout_value))
            return result
        for evt in events:
            if not isinstance(evt, EventEnvelope):
                raise ValueError('events must be a list of EventEnvelope')

        # retrieve distributed tracing info if any
        trace_info = self.get_trace()
        # emulate RPC
        inbox = Inbox(self)
        temp_route = inbox.get_route()
        inbox_queue = inbox.get_queue()
        try:
            for evt in events:
                # restore distributed tracing info from current thread
                if trace_info:
                    if trace_info.get_route() is not None and evt.get_from() is None:
                        evt.set_from(trace_info.get_route())
                    if trace_info.get_id() is not None and trace_info.get_path() is not None:
                        evt.set_trace(trace_info.get_id(), trace_info.get_path())

                route = evt.get_to()
                evt.set_reply_to(temp_route, me=True)
                if route in self._function_queues:
                    self._loop.call_soon_threadsafe(self._send, route, evt.to_map())
                else:
                    if self._cloud.is_connected():
                        self._cloud.send_payload({'type': 'event', 'event': evt.to_map()})
                    else:
                        raise ValueError(f'route {route} not found')

            total_requests = len(events)
            result_list = list()
            while True:
                try:
                    # wait until all response events are delivered to the inbox
                    result_list.append(inbox_queue.get(True, timeout_value))
                    if len(result_list) == len(events):
                        return result_list
                except Empty:
                    raise TimeoutError(f'Request timeout for {round(timeout_value, 3)} seconds. '
                                       f'Expect: {total_requests} responses, actual: {len(result_list)}')
        finally:
            inbox.close()

    def send_request(self, event: EventEnvelope, timeout_seconds: float):
        timeout_value = self.util.get_float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError('timeout value in seconds must be positive number')
        if not isinstance(event, EventEnvelope):
            raise ValueError('event object must be an EventEnvelope')
        # restore distributed tracing info from current thread
        trace_info = self.get_trace()
        if trace_info:
            if trace_info.get_route() is not None and event.get_from() is None:
                event.set_from(trace_info.get_route())
            if trace_info.get_id() is not None and trace_info.get_path() is not None:
                event.set_trace(trace_info.get_id(), trace_info.get_path())
        # emulate RPC
        inbox = Inbox(self)
        temp_route = inbox.get_route()
        inbox_queue = inbox.get_queue()
        try:
            route = event.get_to()
            event.set_reply_to(temp_route, me=True)
            if route in self._function_queues:
                self._loop.call_soon_threadsafe(self._send, route, event.to_map())
            else:
                if self._cloud.is_connected():
                    self._cloud.send_payload({'type': 'event', 'event': event.to_map()})
                else:
                    raise ValueError(f'route {route} not found')
            # wait until response event is delivered to the inbox
            return inbox_queue.get(True, timeout_value)
        except Empty:
            raise TimeoutError(f'Route {event.get_to()} timeout for {round(timeout_value, 3)} seconds')
        finally:
            inbox.close()

    def send_event(self, event: EventEnvelope, broadcast=False) -> None:
        if not isinstance(event, EventEnvelope):
            raise ValueError('event object must be an EventEnvelope class')
        # restore distributed tracing info from current thread
        trace_info = self.get_trace()
        if trace_info:
            if trace_info.get_route() is not None and event.get_from() is None:
                event.set_from(trace_info.get_route())
            if trace_info.get_id() is not None and trace_info.get_path() is not None:
                event.set_trace(trace_info.get_id(), trace_info.get_path())
        # regulate rate for best performance
        self._seq += 1
        self._throttle.regulate_rate(self._seq)
        route = event.get_to()
        if broadcast:
            event.set_broadcast(True)
        reply_to = event.get_reply_to()
        if reply_to:
            target = reply_to[2:] if reply_to.startswith('->') else reply_to
            if route == target:
                raise ValueError('route and reply_to must not be the same')
        if route in self._function_queues:
            if event.is_broadcast() and self._cloud.is_connected():
                self._cloud.send_payload({'type': 'event', 'event': event.to_map()})
            else:
                self._loop.call_soon_threadsafe(self._send, route, event.to_map())
        else:
            if self._cloud.is_connected():
                self._cloud.send_payload({'type': 'event', 'event': event.to_map()})
            else:
                raise ValueError(f'route {route} not found')

    def send_event_later(self, event: EventEnvelope, delay_in_seconds: float) -> None:
        self._loop.call_later(delay_in_seconds, self.send_event, event)

    def exists(self, routes: any) -> bool:
        if isinstance(routes, str):
            single_route = routes
            if self.has_route(single_route):
                return True
            if self.cloud_ready():
                event = EventEnvelope()
                event.set_to(self.SERVICE_QUERY).set_header('type', 'find').set_header('route', single_route)
                result = self.send_request(event, 8.0)
                if isinstance(result, EventEnvelope):
                    if result.get_body() is not None:
                        return result.get_body()
        if isinstance(routes, list):
            if len(routes) > 0:
                remote_routes = list()
                for r in routes:
                    if not self.has_route(r):
                        remote_routes.append(r)
                if len(remote_routes) == 0:
                    return True
                if self.cloud_ready():
                    # tell service query to use the route list in body
                    event = EventEnvelope()
                    event.set_to(self.SERVICE_QUERY).set_header('type', 'find')
                    event.set_header('route', '*').set_body(routes)
                    result = self.send_request(event, 8.0)
                    if isinstance(result, EventEnvelope) and result.get_body() is not None:
                        return result.get_body()
        return False

    def _remove_route(self, route):
        if route in self._function_queues:
            self._send(route, None)
            self._function_queues.pop(route)

    def _send(self, route, event):
        if route in self._function_queues:
            config = self._function_queues[route]
            if 'queue' in config:
                config['queue'].put_nowait(event)

    def connect_to_cloud(self):
        self._loop.run_in_executor(self._executor, self._cloud.start_connection)

    def stop(self):
        #
        # to allow user application to invoke the "stop" method from a registered service,
        # the system must start a new thread so that the service can finish first.
        #
        if not self.stopped:
            self.log.info('Bye')
            # guarantee this stop function to execute only once
            self.stopped = True
            # exit the run_forever loop if any
            self.running = False
            # in case the calling function has just send an event asynchronously
            time.sleep(1.0)
            threading.Thread(target=self._bye).start()

    def _bye(self):
        def stopping():
            route_list = []
            for route in self.get_routes():
                route_list.append(route)
            for route in route_list:
                self._remove_route(route)
            self._loop.create_task(full_stop())

        async def full_stop():
            # give time for registered services to stop
            await asyncio.sleep(1.0)
            queue_dir = self.util.normalize_path(f'{self.work_dir}/queues/{self.get_origin()}')
            self.util.cleanup_dir(queue_dir)
            self._loop.stop()

        self._cloud.close_connection(1000, f'Application {self.get_origin()} is stopping', stop_engine=True)
        self._loop.call_soon_threadsafe(stopping)
