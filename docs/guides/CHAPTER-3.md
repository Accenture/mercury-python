# Post Office API

Post Office is a platform abstraction layer that routes events among functions. It maintains a distributed routing table to ensure that service discovery is instantaneous,

## Obtain an instance of the post office object

```
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

```
request(self, route: str, timeout_seconds: float,
                headers: dict = None, body: any = None,
                correlation_id: str = None) -> EventEnvelope

e.g.
result = po.request('hello.world.2', 2.0, headers={'some_key': 'some_value'}, body='hello world')
print(result.get_body())

```

Note that Mercury supports Java primitive, Map and PoJo in the message body. If you put other object, it may throw serialization exception or the object may become empty.

### Asynchronous / Drop-n-forget

To make an asynchronous call, use the `send` method.

```
send(self, route: str, headers: dict = None, body: any = None, reply_to: str = None, me=True) -> None
```
You may put key-value pairs in the "headers" field for holding parameters. For message payload, put Python primitive or dictionary in the "body" field.

### Call-back

You can register a call back function and uses its route name as the "reply-to" address in the send method.

### Pipeline

In a pipeline operation, there is stepwise event propagation. e.g. Function A sends to B and set the "reply-to" as C. Function B sends to C and set the "reply-to" as D, etc.

To pass a list of stepwise targets, you may send the list as a parameter. Each function of the pipeline should forward the pipeline list to the next function.


### Streaming

You can use streams for functional programming. One of the approach is to use a singleton function.

1. Singleton functions

To create a singleton, you can set `instances` of the calling and called functions to 1. When you send events from the calling function to the called function, the platform guarantees that the event sequencing of the data stream.

To guarantee that there is only one instance of the calling and called function, you should register them with a globally unique route name. e.g. using UUID like "producer-b351e7df-827f-449c-904f-a80f9f3ecafe" and "consumer-d15b639a-44d9-4bc2-bb54-79db4f866fe3".

Note that you can programmatically `register` and `release` a function at run-time.

If you create the functions at run-time, please remember to release the functions when processing is completed to avoid wasting system resources.

### Broadcast

Broadcast is the easiest way to do "pub/sub". To broadcast an event to multiple application instances, use the `broadcast` method.

```
broadcast(self, route: str, headers: dict = None, body: any = None) -> None

e.g.
po.broadcast("hello.world.1", body="this is a broadcast message from "+platform.get_origin())

```

### Join-n-fork

You can perform join-n-fork RPC calls using a parallel version of the `parallel_request` method.

```
parallel_request(self, events: list, timeout_seconds: float) -> list

e.g.
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

### Check if a target service is available

To check if a target service is available, you can use the `exists` method.

```
exists(self, route: str)

e.g.
if po.exists("hello.world"):
    # do something

This service discovery process is instantaneous using distributed routing table.

```


| Chapter-4                                 | Home                                     |
| :----------------------------------------:|:----------------------------------------:|
| [Upcoming features](CHAPTER-4.md)         | [Table of Contents](TABLE-OF-CONTENTS.md)|
