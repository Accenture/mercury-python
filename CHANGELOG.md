# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## Version 1.12.60, 8/8/2020

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
