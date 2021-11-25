# API reference

Mercury has a small set of API for declaring microservices functions and sending events to functions.

## Basic platform classes and utilities

1. `Platform` - the platform class is a singleton object for managing life cycle of functions.
2. `PostOffice` - a singleton object for sending events to functions.

## EventEnvelope

EventEnvelope is a vehicle for storing and transporting an event that contains headers and body. `headers` can be used 
to carry parameters and `body` is the message payload. Each event should have either or both of headers and body. 
You can set Python primitive or dictionary (aka Map) into the body.

Mercury automatically performs serialization and deserialization using the EventEnvelope's `to_bytes()` and 
`from_bytes(bytes)` methods. For performance and network efficiency, it is using [MsgPack](https://msgpack.org/) 
for serialization.

EventEnvelope is used for both input and output. For simple use cases in asynchronous operation, you do not need to 
use the EventEnvelope. For RPC call, the response object is an EventEnvelope. The service response is usually stored 
in the "body" in the envelope. A service may also return key-values in the "headers" field.

Mercury is truly schemaless. It does not care if you are sending a primitive or dictionary. The calling function and 
the called function must understand each other's API interface contract to communicate properly.

## Platform API

### Obtain an instance of the platform object

```python
platform = Platform();
```
platform is a singleton object. Therefore, it is safe to invoke this class multiple times


### application.yml

The default application configuration file is in the embedded resources folder.

If you want to specify your own configuration file, you may override it when you start the platform.

```python
platform = Platform(your_config_yaml_file_path);
```

### Register a public function

To register a function, you can assign a route name to a function instance. You can also set the maximum number of 
concurrent workers in an application instance. This provides vertical scalability in addition to horizontal scaling 
by Docker/Kubernetes.

To create a singleton function, set instances to 1.

```python
register(self, route: str, user_function: any, total_instances: int, is_private: bool = False) -> None

e.g.
# you can register a method of a class
platform.register('hello.world.1', Hi().hello, 5)
# or register a function
platform.register('hello.world.2', hello, 10)
```

### service function signatures

Your service function must use one of the following signatures:

```python
def regular_service(headers: dict, body: any, instance: int):
def singleton_service(headers: dict, body: any):
def interceptor(event: EventEnvelope):

# You can use any function names but the argument names and types must be exactly the same as the signatures above.
```

A regular function would accept input parameters as "headers", message payload as "body". The worker instance number 
is provided as "instance". You may define more than one worker in the instances during the "registration" phase 
described in the last section.

A singleton function guarantees singleton within one application instance. Since your application unit is 
independently deployed and scalable, you may have more than one instance running horizontally in a parallel fashion. 
You would need other techniques to guarantee "single consumer" pattern in a distributed environment.

An interceptor is used for advanced orchestration. Instead of passing headers and body, the raw EventEnvelope is 
provided as input so that the interceptor can inspect its routing information and metadata.

### Register a private function

Public functions are advertised to the whole system while private functions are encapsulated within an application
instance.

You may define your function as `private` if it is used internally by other functions in the same application instance. 
Use the `is_private` parameter in the register method.

### Release a function

A function can be long term or transient. When a function is no longer required, you can cancel the function using 
the "release" method.

```python
release(self, route: str) -> None
```

### Connect to the cloud

You can write truly event-driven microservices as a standalone application. However, it would be more interesting to 
connect the services together through a network event stream system.

To do this, you can ask the platform to connect to the cloud.

```python
platform.connect_to_cloud()
```

| Chapter-3                              | Home                                     |
| :-------------------------------------:|:----------------------------------------:|
| [Post Office API](CHAPTER-3.md)        | [Table of Contents](TABLE-OF-CONTENTS.md)|
