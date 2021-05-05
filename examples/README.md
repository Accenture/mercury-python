# Worked Examples

# Standalone application

The `standalone-demo.py` uses Mercury to build a standalone application. 
The main thread and all the service functions are running concurrently and independently.

The concurrency is supported using Python Async-IO's event-loop.

# Hello World service

The `hello-world.py` demonstrates writing a simple hello world service and exposing it as a named service "hello.world".

Note that this application requires connection to the Mercury language connector.

# More use cases

The `more-demo.py` illustrates additional features. It requires connection to the Mercury language connector.

Features demonstrated:
1. Defining a service using either a method or class
2. Async - making a drop-n-forget call
3. RPC - making a request-response. The caller is blocked until the service responds or timeout.
4. Fork-n-Join - making parallel requests
   
# Native publish/subscribe

The `subscriber-demo.py` demonstrates creating a service to listen to a Kafka topic.

Note that this demo requires the use of Kafka.

# User defined distributed trace aggregator

The `user-defined-tracer.py` demonstrates how to write a distributed trace aggregator. 
Application instances in your Mercury powered cloud native system will push performance metrics to the aggregator.
It is just a demo and thus the trace metrics are simply written to standard out.

For a real-world project, you should save the metrics to a database or search engine. e.g. Elastic Search.
