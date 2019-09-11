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

from mercury.resources.constants import AppConfig
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
        queue_dir = self.util.normalize_path(self.platform.work_dir + "/queues/" + self.platform.get_origin())
        self.disk_queue = ElasticQueue(queue_dir=queue_dir, queue_id=route)
        self._loop = loop
        self._executor = executor
        self.queue = queue
        self.route = route
        self.user_function = user_function
        self.ready_queue = asyncio.Queue(loop=self._loop)
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
                self.log.error("Event for " + self.route + " dropped because worker #"+str(worker_number) + "not found")
        else:
            self.log.error("Event for " + self.route + " dropped because there are no workers available")

    async def listen(self, total_instances):
        # create concurrent workers and
        total = 1 if self._singleton else total_instances
        for i in range(total):
            instance_number = i + 1
            worker_queue = asyncio.Queue(loop=self._loop)
            self.worker_list[instance_number] = worker_queue
            WorkerQueue(self._loop, self._executor, self.queue, worker_queue,
                        self.route, self.user_function, instance_number, self._singleton, self._interceptor)
            # populate the ready queue with an initial set of worker numbers
            await self.queue.put(instance_number)

        route_type = 'PRIVATE' if self.platform.route_is_private(self.route) else 'PUBLIC'
        # minimize logging for temporary inbox that starts with the "r" prefix
        if self._interceptor and self.util.is_inbox(self.route):
            self.log.debug(route_type+' ' + self.route + " with " + str(total) + " instance" +
                           ('s' if total > 1 else '') + " started")
        else:
            self.log.info(route_type+' ' + self.route + " with " + str(total) + " instance" +
                          ('s' if total > 1 else '')+" started")

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
            self.log.debug(self.route + " stopped")
        else:
            self.log.info(self.route + " stopped")


class WorkerQueue:

    DISTRIBUTED_TRACING = "distributed.tracing"

    def __init__(self, loop, executor, manager_queue, worker_queue, route, user_function, instance,
                 singleton, interceptor):
        self.platform = Platform()
        self.log = self.platform.log
        self._loop = loop
        self._executor = executor
        self.manager_queue = manager_queue
        self.worker_queue = worker_queue
        self.route = route
        # trace all routes except ws.outgoing
        self.tracing = route != 'ws.outgoing'
        self.user_function = user_function
        self.instance = instance
        self.singleton = singleton
        self.interceptor = interceptor
        self._loop.create_task(self.listen())
        self.log.debug(route + " #" + str(self.instance) + " started")

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

        self.log.debug(self.route + " #" + str(self.instance) + " stopped")

    def handle_event(self, event, instance):
        headers = dict() if 'headers' not in event else event['headers']
        body = None if 'body' not in event else event['body']
        result = None
        error_code = None
        error_msg = None
        # start distributed tracing if the event contains trace_id and trace_path
        if 'trace_id' in event and 'trace_path' in event:
            self.platform.start_tracing(event['trace_id'], event['trace_path'])
        # execute user function
        begin = end = time.time()
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
            end = time.time()
        except AppException as e:
            error_code = e.get_status()
            error_msg = e.get_message()
        except ValueError as e:
            error_code = 400
            error_msg = str(e)
        except Exception as e:
            error_code = 500
            error_msg = str(e)

        # execution time is rounded to 3 decimal points
        exec_time = float(format((end - begin) * 1000, '.3f'))

        if error_code:
            if 'reply_to' in event:
                # set exception as result
                result = EventEnvelope().set_status(error_code).set_body(error_msg)
            else:
                self.log.warn(
                    "Unhandled exception for " + self.route + " - code=" + str(error_code) + ", message=" + error_msg)

        if not self.interceptor and 'reply_to' in event:
            reply_to = event['reply_to']
            # in case this is a RPC call from within
            if reply_to.startswith('->'):
                reply_to = reply_to[2:]
            response = EventEnvelope().set_to(reply_to)
            if not error_code:
                response.set_exec_time(exec_time, False)
            if 'extra' in event:
                response.set_extra(event['extra'])
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
                self.log.warn("Event dropped because "+str(e))

        # send tracing info to distributed trace logger
        trace_info = self.platform.stop_tracing()
        if self.tracing and trace_info is not None and isinstance(trace_info, TraceInfo):
            dt = EventEnvelope().set_to(self.DISTRIBUTED_TRACING).set_body(trace_info.get_annotations())
            dt.set_header('origin', self.platform.get_origin())
            dt.set_header('id', trace_info.get_id()).set_header('path', trace_info.get_path())
            dt.set_header('service', self.route).set_header('start', trace_info.get_start_time())
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
        self.begin = time.time()
        self.temp_route = 'r.' + (''.join(str(uuid.uuid4()).split('-')))
        self.inbox_queue = Queue()
        self.platform = platform
        self.platform.register(self.temp_route, self.listener, 1, is_private=True)

    # inbox is an interceptor service which must be defined with the parameter "envelope" as below
    def listener(self, event: EventEnvelope):
        event.set_round_trip((time.time() - self.begin) * 1000)
        self.inbox_queue.put(event)

    def get_route(self):
        return self.temp_route

    def get_queue(self):
        return self.inbox_queue

    def close(self):
        self.platform.release(self.temp_route)


@Singleton
class Platform:

    SERVICE_QUERY = 'system.service.query'

    def __init__(self, work_dir: str = None, log_file: str = None, log_level: str = None, max_threads: int = None,
                 network_connector: str = None):
        if sys.version_info.major < 3:
            python_version = str(sys.version_info.major)+"."+str(sys.version_info.minor)
            raise RuntimeError("Requires python 3.6 and above. Actual: "+python_version)

        self.util = Utility()
        self.origin = 'py'+(''.join(str(uuid.uuid4()).split('-')))
        config = AppConfig()
        my_log_file = (config.LOG_FILE if hasattr(config, 'LOG_FILE') else None) if log_file is None else log_file
        my_log_level = config.LOG_LEVEL if log_level is None else log_level
        self._max_threads = config.MAX_THREADS if max_threads is None else max_threads
        self.work_dir = config.WORK_DIRECTORY if work_dir is None else work_dir
        self.log = LoggingService(log_dir=self.util.normalize_path(self.work_dir + "/log"),
                                  log_file=my_log_file, log_level=my_log_level).get_logger()
        self._loop = asyncio.new_event_loop()
        my_distributed_trace = DistributedTrace(self, config.DISTRIBUTED_TRACE_PROCESSOR)
        my_connector = config.NETWORK_CONNECTOR if network_connector is None else network_connector
        self._cloud = NetworkConnector(self, my_distributed_trace, self._loop, my_connector, self.origin)
        self._function_queues = dict()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_threads)
        self.log.info("Concurrent thread pool = "+str(self._max_threads))
        #
        # Before we figure out how to solve blocking file I/O, we will regulate event output rate.
        #
        my_test_dir = self.util.normalize_path(self.work_dir + "/test")
        if not os.path.exists(my_test_dir):
            os.makedirs(my_test_dir)
        self._throttle = Throttle(self.util.normalize_path(my_test_dir + "/to_be_deleted"), log=self.log)
        self._seq = 0
        self.util.cleanup_dir(my_test_dir)
        self.log.debug("Estimated processing rate is "+format(self._throttle.get_tps(), ',d')
                      + " events per second for this computer")
        self.running = True
        self.stopped = False
        # distributed trace sessions
        self._traces = {}

        # start event loop in a new thread to avoid blocking the main thread
        def main_event_loop():
            self.log.info("Event system started")
            self._loop.run_forever()
            self.log.info("Event system stopped")
            self._loop.close()

        threading.Thread(target=main_event_loop).start()


    def get_origin(self):
        """
        get the origin ID of this application instance
        :return: origin ID
        """
        return self.origin

    def get_trace_id(self) -> str:
        """
        get trace ID for a transaction
        :return: trace ID
        """
        trace_info = self.get_trace()
        return trace_info.get_id() if trace_info is not None else None

    def get_trace(self) -> TraceInfo:
        """
        get trace info for a transaction
        :return:
        """
        thread_id = threading.get_ident()
        return self._traces[thread_id] if thread_id in self._traces else None

    def annotate_trace(self, key: str, value: str):
        """
        Annotate a trace at a point of a transaction
        :param key: any key
        :param value: any value
        :return:
        """
        trace_info = self.get_trace()
        if trace_info is not None and isinstance(trace_info, TraceInfo):
            trace_info.annotate(key, value)

    def start_tracing(self, trace_id: str, trace_path: str):
        """
        IMPORTANT: This method is reserved for system use. DO NOT call this from a user application.
        :param trace_id: id
        :param trace_path: path such as URI
        :return: None
        """
        thread_id = threading.get_ident()
        self._traces[thread_id] = TraceInfo(trace_id, trace_path)

    def stop_tracing(self):
        """
        IMPORTANT: This method is reserved for system use. DO NOT call this from a user application.
        :return: TraceInfo
        """
        thread_id = threading.get_ident()
        if thread_id in self._traces:
            trace_info = self.get_trace()
            self._traces.pop(thread_id)
            return trace_info

    def run_forever(self):
        """
        Tell the platform to run in the background until user presses CTL-C or the application is stopped by admin
        :return: None
        """
        def graceful_shutdown(signum, frame):
            self.log.warn("Control-C detected" if signal.SIGINT == signum else "KILL signal detected")
            self.running = False
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, graceful_shutdown)
            signal.signal(signal.SIGINT, graceful_shutdown)
            # keep the main thread running so CTL-C can be detected
            self.log.info("To stop this application, press Control-C")
            while self.running:
                time.sleep(0.1)
            # exit forever loop and ask platform to end event loop
            self.stop()
        else:
            raise ValueError('Unable to register Control-C and KILL signals because this is not the main thread')

    def register(self, route: str, user_function: any, total_instances: int, is_private: bool = False) -> None:
        """
        Register a user function
        :param route: ID of the function
        :param user_function: the lambda function given by you
        :param total_instances: 1 for singleton or more for concurrency
        :param is_private: true if internal function within this application instance
        :return:
        """
        self.util.validate_service_name(route)
        if route in self._function_queues:
            raise ValueError("route "+route+" already registered")
        if not isinstance(total_instances, int):
            raise ValueError("Expect total_instances to be int, actual: "+str(type(total_instances)))
        if total_instances < 1:
            raise ValueError("total_instances must be at least 1")
        if total_instances > self._max_threads:
            raise ValueError("total_instances must not exceed max threads of "+str(self._max_threads))
        function_type = self.util.get_function_type(user_function)
        if function_type == FunctionType.NOT_SUPPORTED:
            raise ValueError("Function signature should be (headers: dict, body: any, instance: int) or " +
                             "(headers: dict, body: any) or (event: EventEnvelope)")

        queue = asyncio.Queue(loop=self._loop)
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

    def release(self, route: str) -> None:
        # this will un-register a route
        if not isinstance(route, str):
            raise ValueError("Expect route to be str, actual: "+str(type(route)))
        if route not in self._function_queues:
            raise ValueError("route "+route+" not found")
        # advertise the deleted route to the network
        if self._cloud.is_ready() and self.route_is_private(route):
            self._cloud.send_payload({'type': 'remove', 'route': route})
        self._remove_route(route)

    def has_route(self, route: str) -> bool:
        if not isinstance(route, str):
            raise ValueError("Expect route to be str, actual: "+str(type(route)))
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

    def parallel_request(self, events: list, timeout_seconds: float):
        timeout_value = self.util.get_float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError("timeout value in seconds must be positive number")
        if not isinstance(events, list):
            raise ValueError("events must be a list of EventEnvelope")
        if len(events) == 0:
            raise ValueError("event list is empty")
        if len(events) == 1:
            result = list()
            result.append(self.request(events[0], timeout_value))
            return result
        for evt in events:
            if not isinstance(evt, EventEnvelope):
                raise ValueError("events must be a list of EventEnvelope")

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
                    evt.set_trace(trace_info.get_id(), trace_info.get_path())
                route = evt.get_to()
                evt.set_reply_to(temp_route, me=True)
                if route in self._function_queues:
                    self._loop.call_soon_threadsafe(self._send, route, evt.to_map())
                else:
                    if self._cloud.is_connected():
                        self._cloud.send_payload({'type': 'event', 'event': evt.to_map()})
                    else:
                        raise ValueError("route " + route + " not found")

            total_requests = len(events)
            result_list = list()
            while True:
                try:
                    # wait until all response events are delivered to the inbox
                    result_list.append(inbox_queue.get(True, timeout_value))
                    if len(result_list) == len(events):
                        return result_list
                except Empty:
                    raise TimeoutError('Requests timeout for '+format(timeout_value, '.3f')+" seconds. Expect: " +
                                       str(total_requests) + " responses, actual: " + str(len(result_list)))
        finally:
            inbox.close()

    def request(self, event: EventEnvelope, timeout_seconds: float):
        timeout_value = self.util.get_float(timeout_seconds)
        if timeout_value <= 0:
            raise ValueError("timeout value in seconds must be positive number")
        if not isinstance(event, EventEnvelope):
            raise ValueError("event object must be an EventEnvelope")
        # restore distributed tracing info from current thread
        trace_info = self.get_trace()
        if trace_info:
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
                    raise ValueError("route " + route + " not found")
            # wait until response event is delivered to the inbox
            return inbox_queue.get(True, timeout_value)
        except Empty:
            raise TimeoutError('Route '+event.get_to()+' timeout for '+format(timeout_value, '.3f')+" seconds")
        finally:
            inbox.close()

    def send_event(self, event: EventEnvelope, broadcast=False) -> None:
        if not isinstance(event, EventEnvelope):
            raise ValueError("event object must be an EventEnvelope class")
        # restore distributed tracing info from current thread
        trace_info = self.get_trace()
        if trace_info:
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
                raise ValueError("route and reply_to must not be the same")
        if route in self._function_queues:
            if event.is_broadcast() and self._cloud.is_connected():
                self._cloud.send_payload({'type': 'event', 'event': event.to_map()})
            else:
                self._loop.call_soon_threadsafe(self._send, route, event.to_map())
        else:
            if self._cloud.is_connected():
                self._cloud.send_payload({'type': 'event', 'event': event.to_map()})
            else:
                raise ValueError("route "+route+" not found")

    def exists(self, routes: any):
        if isinstance(routes, str):
            single_route = routes
            if self.has_route(single_route):
                return True
            if self.cloud_ready():
                event = EventEnvelope()
                event.set_to(self.SERVICE_QUERY).set_header('type', 'find').set_header('route', single_route)
                result = self.request(event, 8.0)
                if isinstance(result, EventEnvelope):
                    if result.get_body() is not None:
                        return result.get_body()
        if isinstance(routes, list):
            if len(routes) > 0:
                remote_routes = list()
                for r in routes:
                    if not self.platform.has_route(r):
                        remote_routes.append(r)
                if len(remote_routes) == 0:
                    return True
                if self.platform.cloud_ready():
                    # tell service query to use the route list in body
                    event = EventEnvelope()
                    event.set_to(self.SERVICE_QUERY).set_header('type', 'find')
                    event.set_header('route', '*').set_body(routes)
                    result = self.request(event, 8.0)
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
            time.sleep(0.5)
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
            queue_dir = self.util.normalize_path(self.work_dir + "/queues/" + self.get_origin())
            self.util.cleanup_dir(queue_dir)
            self._loop.stop()

        self._cloud.close_connection(1000, 'bye', stop_engine=True)
        self._loop.call_soon_threadsafe(stopping)
