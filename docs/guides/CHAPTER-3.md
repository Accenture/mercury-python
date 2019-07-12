# Post Office API

Post Office is a platform abstraction layer that routes events among functions. It maintains a distributed routing table to ensure that service discovery is instantaneous,

## Obtain an instance of the post office object

```python
po = PostOffice()

# the Post Office is a singleton class
```

## Communication patterns

- RPC `“Request-response”, best for interactivity`
- Asynchronous `e.g. Drop-n-forget and long queries`
- Call-back `e.g. Progressive rendering`
- Pipeline `e.g. Work-flow application`
- Streaming `e.g. Data ingest`
- Broadcast `e.g. Concurrent processing of the same dataset with different outcomes`

### RPC (Request-response)

The Mercury framework is 100% event-driven and all communications are asynchronous. To emulate a synchronous RPC, it suspends the calling function and uses temporary Inbox as a callback function. The called function will send the reply to the callback function which in turns wakes up the calling function.

To make a RPC call, you can use the `request` method.

```python
request(self, route: str, timeout_seconds: float,
                headers: dict = None, body: any = None,
                correlation_id: str = None) -> EventEnvelope

# example
result = po.request('hello.world.2', 2.0, headers={'some_key': 'some_value'}, body='hello world')
print(result.get_body())

```

Note that Mercury supports Python primitive or dictionary in the message body. If you put other object, it may throw serialization exception or the object may become empty.

### Asynchronous / Drop-n-forget

To make an asynchronous call, use the `send` method.

```python
send(self, route: str, headers: dict = None, body: any = None, reply_to: str = None, me=True) -> None
```

You may put key-value pairs in the "headers" field for holding parameters. For message payload, put Python primitive or dictionary in the "body" field.

### Deferred delivery

```python
send_later(self, route: str, headers: dict = None, body: any = None, seconds: float = 1.0) -> None
```

Minimum time is 0.1 seconds and maximum time is 24 hours. The deferred delivery service runs in the language connector. Without the language connector, this method will raise exception.

### Call-back

You can register a call back function and uses its route name as the "reply_to" address in the send method.

### Pipeline

In a pipeline operation, there is stepwise event propagation. e.g. Function A sends to B and set the "reply_to" as C. Function B sends to C and set the "reply_to" as D, etc.

To pass a list of stepwise targets, you may send the list as a parameter. Each function of the pipeline should forward the pipeline list to the next function.

### Streaming

You can use streams for functional programming. There are two ways to do streaming.

1. Singleton functions

To create a singleton, you can set `instances` of the calling and called functions to 1. When you send events from the calling function to the called function, the platform guarantees that the event sequencing of the data stream.

To guarantee that there is only one instance of the calling and called function, you should register them with a globally unique route name. e.g. using UUID like "producer-b351e7df-827f-449c-904f-a80f9f3ecafe" and "consumer-d15b639a-44d9-4bc2-bb54-79db4f866fe3".

Note that you can programmatically `register` and `release` a function at run-time.

If you create the functions at run-time, please remember to release the functions when processing is completed to avoid wasting system resources.

2. Object stream

To do object streaming, you can use the ObjectStreamIO to create a new stream or open an existing stream.
Then, you can write to the stream using the `write` method and read from the stream using the `read` generator.

For the producer, you can use the `send_eof` to signal that that there are no more events to the stream.

For the consumer, When you detect the end of stream, you can close the input stream to release the stream and all resources associated with it.

I/O stream consumes resources and thus you must close the input stream at the end of stream processing.
The system will automatically close the stream upon an expiry timer that you provide when a new stream is created.

The following sample code demonstrates this use case.

```python
from mercury.system.objstream import ObjectStreamIO
# create a new stream with 60 seconds inactivity expiry
producer = ObjectStreamIO(expiry_seconds=60)
producer.write('hello world 1')
producer.write('hello world 2')
# signal EOF so the input stream generator will finish
producer.send_eof()

# the producer should obtain the stream_id and send it to the consumer
stream_id = producer.get_route()

# the consumer will open the existing stream with the stream_id
consumer = ObjectStreamIO(route=stream_id, expiry_seconds=60)
try:
    # set a 10 seconds read timeout for the input stream generator
    for i in consumer.reaed(10):
        print(i)
except TimeoutError as te:
    # you may retry reading from the generator again if you want to wait for more input

# close and release the stream
consumer.close()
```

### Broadcast

Broadcast is the easiest way to do "pub/sub". To broadcast an event to multiple application instances, use the `broadcast` method.

```python
broadcast(self, route: str, headers: dict = None, body: any = None) -> None

# example
po.broadcast("hello.world.1", body="this is a broadcast message from "+platform.get_origin())

```

### Join-n-fork

You can perform join-n-fork RPC calls using a parallel version of the request, `parallel_request` method.

```python
parallel_request(self, events: list, timeout_seconds: float) -> list

# illustrate parallel RPC requests
event_list = list()
event_list.append(EventEnvelope().set_to('hello.world.1').set_body("first request"))
event_list.append(EventEnvelope().set_to('hello.world.2').set_body("second request"))
try:
    result = po.parallel_request(event_list, 2.0)
    if isinstance(result, list):
        print('Received', len(result), 'RPC responses:')
        for res in result:
            print("HEADERS =", res.get_headers(), ", BODY =", res.get_body(),
                    ", STATUS =",  res.get_status(),
                    ", EXEC =", res.get_exec_time(), ", ROUND TRIP =", res.get_round_trip(), "ms")
except TimeoutError as e:
    print("Exception: ", str(e))
```


### Pub/Sub for store-n-forward event streaming

Native Pub/Sub will be automatically enabled if the underlying cloud connector supports it. e.g. Kafka.

Mercury provides real-time inter-service event streaming and you do not need to deal with low-level messaging.

However, if you want to do store-n-forward pub/sub for certain use cases, you may use the `PubSub` class.
Following are some useful pub/sub API:

```python
def feature_enabled()
def create_topic(topic: str)
def delete_topic(topic: str)
def publish(topic: str, headers: dict = None, body: any = None)
def subscribe(self, topic: str, route: str, parameters: list = None)
def unsubscribe(self, topic: str, route: str)
def exists(topic: str)
def list_topics()

```
Some pub/sub engine would require additional parameters when subscribing a topic. For Kafka, you must provide the following parameters

1. clientId
2. groupId
3. optional read offset pointer

If the offset pointer is not given, Kafka will position the read pointer to the latest when the clientId and groupId are first seen.
Thereafter, Kafka will remember the read pointer for the groupId and resume read from the last read pointer.

As a result, for proper subscription, you must create the topic first and then provide a route to a function to subscribe to the topic before publishing anything to the topic.

To read the event stream of a topic from the beginning, you can set offset to "0".

The system encapsulates the headers and body (aka payload) in an event envelope so that you do not need to do serialization yourself.
The payload can be a dict, bool, str, int or float.

### Check if a target service is available

To check if a target service is available, you can use the `exists` method.

```python
exists(self, routes: any)

# input can be a route name or a list of routes
# it will return true only when all routes are available
# examples

if po.exists("hello.world"):
    # do something

if po.exists(['hello.math', 'v1.diff.equation']):
    # do other things

```
This service discovery process is instantaneous using distributed routing table.

[Table of Contents](TABLE-OF-CONTENTS.md)
