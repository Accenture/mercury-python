# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## Version 2.3.6, 6/16/2022

### Added

1. Support multiple pub/sub cluster instances
2. Added cloud connection life cycle service

### Removed

N/A

### Changed

Update to use python "f" string formatter

---
## Version 2.3.5, 5/30/2022

### Added

1. Add tagging feature in EventEnvelope for compatibility with Node.js language pack.
2. Improved exception handling for non-exist target service route when making RPC call.

### Removed

Retired temp file log for logger as a cloud native best practice

### Changed

Streamlined language connector transport protocol for compatibility with both Python and Node.js

---
## Version 2.3.4, 5/18/2022

### Added

Support deferred delivery in standalone mode

### Removed

N/A

### Changed

N/A

---
## Version 2.3.3, 5/6/2022

### Added

For unhandled exception, return exception class name and error message.

### Removed

N/A

### Changed

1. Updated README and application.yml files
2. Enhanced AsyncHttpRequest

---
## Version 2.3.0, 1/8/2022

### Added

N/A

### Removed

N/A

### Changed

Version sync up with parent Mercury parent

---
## Version 2.2.2, 11/25/2021

### Added

User application can provide its own application.yml

### Removed

N/A

### Changed

Minor refactoring for asyncio usage to Python 3.10.0

---
## Version 2.1.0, 5/25/2021

### Added

N/A

### Removed

N/A

### Changed

1. For consistency, ObjectStreamIO refactored.
2. ObjectStreamIO is used to create a new I/O stream. 
3. ObjectStreamWriter is used for writing to the output stream and ObjectStreamReader for reading the input stream.

---
## Version 2.0.0, 5/5/2021

### Added

N/A

### Removed

N/A

### Changed

Change default environment variable for language API key to 'LANGUAGE_PACK_KEY'

If secret key is not available, it will read the secret from /tmp/config/lang-api-key.txt

---
## Version 1.13.0, 4/12/2021

### Added

N/A

### Removed

N/A

### Changed

Update reserved tags for automatic payload segmentation protocol

---
## Version 1.12.66, 1/20/2021

### Added

N/A

### Removed

N/A

### Changed

Sync up version with parent Mercury project

---
## Version 1.12.57, 8/7/2020

### Added

N/A

### Removed

N/A

### Changed

Improved distributed trace - set the "from" address in EventEnvelope automatically.

---
## Version 1.12.54, 6/28/2020

### Added

N/A

### Removed

N/A

### Changed

objectstream.py - added transport for "bytes" in addition to dict, str, int, float and bool.

---
## Version 1.12.52, 6/5/2020

### Added

N/A

### Removed

N/A

### Changed

Sync up to parent project's version

---
## Version 1.12.39, 5/3/2020

### Added

1. Application configuration is now stored in the resources folder as "application.yml".
2. ConfigReader to parse YAML and JSON config files
3. A convenient MultiLevelDict class for reading key-values using the dot-bracket notation (e.g. "my.config.key[0]")
4. Support nested arrays in MultiLevelDict

### Removed

constants.py

### Changed

1. Updated platform.py and connector.py to use ConfigReader
2. Added package_data in setup.py

---
## Version 1.12.32, 3/14/2020

### Added

Native pub/sub examples

### Removed

N/A

### Changed

N/A

---
## Version 1.12.30, 1/22/2020

### Added

AsyncHttpRequest is a convenient class to read HTTP events from the rest-automation app.

### Removed

N/A

### Changed

N/A

---
## Version 1.12.17, 12/16/2019

### Added

1. get_route() method is added to PostOffice so that current service can retrieve its own route name
2. The route name of the current service is added to an outgoing event when the "from" field is not present

### Removed

N/A

### Changed

N/A

---
## Version 1.12.12, 10/26/2019

Sync up version number with main mercury project to support multi-tenancy for event streams.

### Added

N/A

### Removed

N/A

### Changed

N/A

---
## Version 1.12.9, 8/27/2019

Sync up version number with main mercury project

### Added

Distributed tracing feature

### Removed

N/A

### Changed

language pack API key obtained from environment variable

---
## Version 1.12.8, 8/15/2019

Sync up version number with main mercury project

### Added

N/A

### Removed

N/A

### Changed

N/A

---
## Version 1.12.7, 7/15/2019

Support discovery of multiple route in the updated `po.exists` API

### Added

N/A

### Removed

N/A

### Changed

N/A

---

## Version 1.12.4, 6/24/2019

### Added

1. Store-n-forward pub/sub API will be automatically enabled if the underlying cloud connector supports it. e.g. kafka
2. ObjectStreamIO, a convenient wrapper class, to provide event stream I/O API.
3. Object stream feature is now a standard feature instead of optional.
4. Deferred delivery example in demo.py
5. Add inactivity expiry timer to ObjectStreamIO so that house-keeper can clean up resources that are idle

### Removed

N/A

### Changed

Bug fix - update EventEnvelope with missing field "extra" which is used as additional routing information for language packs.
This allows correct RPC routing between python applications via the language connector.

---

## Version 1.11.40, 4/29/2019

### Added

Version number sync up with main Mercury project.

### Removed

N/A

### Changed

N/A

---

## Version 1.11.39, 4/29/2019

### Added

First release. Version number sync up with main Mercury project.

### Removed

N/A

### Changed

N/A
