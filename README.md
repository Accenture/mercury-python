# Python language pack for the Mercury microservices framework

This module requires the Mercury Language Connector application as a sidecar.
The main Mercury project is available at https://github.com/Accenture/mercury

Please clone the main project and follow the README file to build the mercury core libraries and the language-connector application.

## Welcome to the Mercury project

The Mercury project is created with one primary objective - `to make software easy to write, read, test, deploy, scale and manage`.

To this end, it introduces the concept of platform abstraction and takes event driven programming to the next level of simplicity.

Everything can be expressed as anonymous functions and they communicate with each other using events. However, event driven and reactive programming can be challenging. The Mercury framework hides all the complexity of event driven and reactive patterns and the magic of inter-service communication.

If you want digital decoupling, this is the technology that you should invest 30 minutes of your time to try it out.

The pre-requisites are very minimal. The foundation technology requires only Java 1.8 JDK or above (Oracle or OpenJDK) and the Maven build system ("mvn"). Docker/Kubernetes are optional. The application modules that you create using the Mercury framework will run in bare metal, VM and any cloud environments.

This project is created by architects and computer scientists who have spent years to perfect software decoupling, scalability and resilience, high performance and massively parallel processing,

With a very high level of decoupling, you can focus in writing business logic without distraction.

Since everything can be expressed as anonymous function, the framework itself is written using this approach. All the cloud connectors and language packs are microservices that are written as anonymous functions. In this way, you can add new connectors, plugins and language packs as you like. The framework is extensible.

The concept is simple. You write your business logic as anonymous functions and packaged them in one or more executables. These executables may be composed as Docker images or alike. You can then deploy them. The containers communicate with each other through an event stream system like Hazelcast or Kafka.

We make the event stream system works as a service mesh. Functions will talk to each other magically without configuration.

If you have your own preference of a different event stream system, you can follow the Hazelcast connector as an example to build your own connector.

Hope you enjoy this journey to improve the world.

Best regards, the Mercury team

June 2019

p.s. This project is originated from the "platformlambda project" and contributed by Accenture.


## Rationale

The microservices movement is gaining a lot of momentum in recent years. Very much inspired with the need to modernize service-oriented architecture and to transform monolithic applications as manageable and reusable pieces, it was first mentioned in 2011 to advocate an architectural style that defines an application as a set of loosely coupled single purpose services.

Classical model of microservices architecture often focuses in the use of REST as interface and the self-containment of data and process. Oftentimes, there is a need for inter-service communication because one service may consume another service. Usually this is done with a service broker. This is an elegant architectural concept. However, many production systems face operational challenges. In reality, it is quite difficult to decompose a solution down to functional level. This applies to both green field development or application modernization. As a result, many microservices modules are indeed smaller subsystems. Within a microservice, business logic is tightly coupled with 3rd party and open sources libraries including cloud platform client components and drivers. This is suboptimal.

## Architecture principles

For simplicity, we advocate 3 architecture principles to write microservices

- minimalist
- event driven
- context bound

Minimalist means we want user software to be as small as possible. The Mercury framework allows you to write business logic down to functional level using simple input-process-output pattern.

Event driven promotes loose coupling. All functions should run concurrently and independently of each other.

Lastly, context bound means high level of encapsulation so that a function only expose API contract and nothing else.

### Platform abstraction

Mercury offers the highest level of decoupling where each piece of business logic can be expressed as an anonymous function. A microservices module is a collection of one or more functions. These functions connect to each other using events.

The framework hides the complexity of event-driven programming and cloud platform integration. For the latter, the service mesh interface is fully encapsulated so that user functions do not need to be aware of network connectivity and details of the cloud platform.

### Simple Input-Process-Output function

This is a best practice in software development. Input is fed into an anonymous function. It will process the input according to some API contract and produce some output.

This simplifies software development and unit tests.


## Defining a microservice

An application can declare one or more Python functions as microservices.
Your microservices function or method must use one of the following signatures:

```
# For singleton service
f(headers: dict, body: any)

# For service that have concurrent workers
f(headers: dict, body: any, instance: int)

# For interceptor service - this allows your application to inspect event metdata
f(event: EventEnvelope)

# f can be any function or method name you like
# headers are input parameters in key-value pairs
# body is application specific. It can be string, byte array, dictionary, etc.
# instance is the instance number when the lambda function is registered to support multiple instances
# event is the event envelope that contains routing metadata, input parameters and message body
#
# headers, body, instance and envelope are standard parameter names that your functions should use.
```

You may then register your microservices like this:

`platform.register(route: str, user_function: any, total_instances: int, is_private: bool = False)`

```python
from microservices.platform import Platform

def hello(headers: dict, body: any, instance: int):
    # business logic here
    return result

#
# Once you have defined a microservice, you should register it with a route name.
# The route name for your anonymous function is like the home address of a house so it can receive letters.
#
platform = Platform()
platform.register("hello.world", hello, 10)
```

To make the service available to other nodes in the system, you can connect your application to the cloud.

```
platform.connect_to_cloud()
```

## making a RPC call

You may make a RPC service call like this. Note that everything is non-blocking in the Mercury framework.

RPC uses a temporary inbox service to simulate a synchronous request-response.

```python
#
# The signature of the request method is:
#    request(self, route: str, timeout_seconds: float,
#            headers: dict = None, body: any = None,
#            correlation_id: str = None) -> EventEnvelope
#

po = PostOffice()
try:
    result = po.request('hello.world', 2.0, headers={'some_key': 'some_value'}, body='test message')
    if isinstance(result, EventEnvelope):
        print('Received RPC response:')
        print("HEADERS =", result.get_headers(), ", BODY =", result.get_body(),
              ", STATUS =",  result.get_status(),
              ", EXEC =", result.get_exec_time(), ", ROUND TRIP =", result.get_round_trip(), "ms")
except TimeoutError as e:
    print("Exception: ", str(e))
```

## making an asynchronous call

You can make a "drop-n-forget" asynchronous request to a service like this:

```python
#
# The signature of the send method is:
# send(self, route: str, headers: dict = None, body: any = None, reply_to: str = None, me=True) -> None
#

po.send('hello.world', headers={'one': 1}, body='hello world one')

```

## call back

You can set the route of a call back function in the "reply to" of the request in the send() method. 
When the service responds, the result will be delivered asynchronously to the call back function.

The default value for the "me" parameter is true. It guarantees that the response will be returned to your calling application.

If you want the system to send the response to any function with the same route name, set the "me" parameter to false.

## stopping the platform event loop

The platform kernel is running in an event loop using Python asyncio. 
You may want to call the following when your application quits.
This will ask the system to release resources and stop gracefully.

```
platform.stop()
```

## stopping the application using Control-C

You may enable Control-C and Kill signal detection by calling the "run_forever()" method. Note that this must be done with the main thread.

```
platform.run_forever()
```

## Installing mercury

You may install mercury using pip as follows:
```
pip install git+https://github.com/Accenture/mercury-python.git
```

If you have accidentally installed mercury using "python setup.py install", you will see the following error when trying to upgrade or uninstall.
```
Cannot uninstall 'mercury'. It is a distutils installed project and thus we cannot accurately determine which files belong to it which would lead to only a partial uninstall.
```
You can resolve this issue by reinstalling mercury with the option "--ignore-installed". This will restore the metadata information for pip.
```
pip install --ignore-installed git+https://github.com/Accenture/mercury-python.git
```

## python3 version

Mercury requires python 3.6.7 or above

## Developer guide

For more details, please refer to the [Developer Guide](docs/guides/TABLE-OF-CONTENTS.md)
