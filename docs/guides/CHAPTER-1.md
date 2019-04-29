# Introduction

Mercury is all about microservices that are minimalist, event-driven and context bounded.

To this end, we use anonymous functions to encapsulate different domains of business logic and library dependencies.

Under the Mercury framework, business logic wrapped in anonymous functions are callable using a `route name`. Mercury resolves routing automatically so that you do not need to care whether the calling and called functions are in the same memory space or in different application instances. Mercury will route requests using a high performance memory event bus when the calling and called functions are int he same memory space and route requests through a network event stream system when the calling and called parties reside in different containers.

## Building the mercury framework libraries

Please follow the [README](../../README.md) file in the project root to build the Mercury framework libraries from source code.

## Writing your first microservices function

Your first function may look like this using Python 3 syntax:
```
def regular_service(headers: dict, body: any, instance: int):
    # some business logic
    return some_thing
```

The easiest way to write your first microservices module is to use the "demo.py" as a template.

## Installing the Mercury python library

To install the Mercury python library, you can do this:

```
pip install git+https://github.com/Accenture/mercury-python.git
```

You will then see "mercury" using the "pip list" command.


## Application unit

The demo.py is a deployable application unit. Behind the curtain, the mercury framework is using Python futures and asyncio event loops. We recommend using Python to build microservices modules. If you have a need for REST or websocket endpoints, use Java JAX-RS or other Python HTTP servers such as Flask.

## Language connector

You can use Mercury to write standalone Python microservices. To create a scalable application, you can use the Mercury `language-suppoort` module in the main [Mercury project](https://github.com/Accenture/mercury/tree/master/language-packs/language-support). Once you build and deploy the language-support module as a sidecar, your python microservices module can connect to it using port 8090. Your Python services will appear in the distributed routing table of all application instances, providing a truly polyglot experience.

## Main application

For each application unit, you will need a main application. This is the entry of your application unit.

In Python, this is super easy.
```
if __name__ == '__main__':
    main()

# where main() is your main method
```

## Calling a function

Unlike traditional programming, you call a function by sending an event instead of calling its method. Mercury resolves routing automatically so events are delivered correctly no matter where the target function is, in the same memory space or another computer elsewhere in the network.

To make a service call to a function, you may do the following:
```
# demonstrate a RPC request
try:
    result = po.request('hello.world.2', 2.0, headers={'some_key': 'some_value'}, body='hello world')
    if isinstance(result, EventEnvelope):
        print('Received RPC response:')
        print("HEADERS =", result.get_headers(), ", BODY =", result.get_body(),
                ", STATUS =",  result.get_status(),
                ", EXEC =", result.get_exec_time(), ", ROUND TRIP =", result.get_round_trip(), "ms")
except TimeoutError as e:
    print("Exception: ", str(e))

# for async call
po.send('hello.world.1', headers={'one': 1}, body='hello world one')
```

## Massive parallel processing

A function is invoked when an event happens. Before the event arrives, the function is just an entry in a routing table and it does not consume any additional resources like threads.

All functions are running in parallel without special coding. Behind the curtain, the system uses Python futures and asyncio event loops for very efficient function execution.


| Chapter-2                           | Home                                     |
| :----------------------------------:|:----------------------------------------:|
| [Platform API](CHAPTER-2.md)        | [Table of Contents](TABLE-OF-CONTENTS.md)|


