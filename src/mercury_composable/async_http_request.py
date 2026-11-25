"""The ``AsyncHttpRequest`` model: the request a REST automation entry delivers to its function,
with the engines' keys.

A function behind a ``rest.yaml`` entry receives the whole request as its body - a map with
``method``, ``url``, ``ip``, ``https``, ``host``, ``headers``, ``parameters`` (``path`` and
``query``), ``body``, ``timeout``, ``query`` (the raw query string), ``cookies`` and ``session`` (the
authentication function's verdict headers) - never the bare HTTP body. This class reads that map
with the engines' accessor vocabulary and builds it at the edge, so the pack's wire shape has one
definition::

    @preload(route="profile.lookup")
    async def lookup(headers: dict[str, str], body: Body):
        request = AsyncHttpRequest(body)
        profile_id = request.path_parameter("id")      # from url: /api/profile/{id}
        lang = request.query_parameter("lang")          # the first value
        return {"profile_id": profile_id, "lang": lang}

A reply's status and headers come from a returned :class:`EventEnvelope`
(``EventEnvelope().set_status(201).set_header("Location", url).set_body(result)``); an error is a
raised ``AppException(status, message)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_UNSET: Any = object()


class AsyncHttpRequest:
    """The engines' request model over the map a REST entry delivers."""

    def __init__(self, data: Any = None) -> None:
        self._method: str | None = None
        self._url: str | None = None
        self._host: str | None = None
        self._headers: dict[str, str] = {}
        self._body: Any = _UNSET
        self._query: dict[str, str | list[str]] = {}
        self._path: dict[str, str] = {}
        self._cookies: dict[str, str] = {}
        self._session: dict[str, str] = {}
        self._ip: str | None = None
        self._https: bool | None = None
        self._timeout: int | None = None
        self._query_string: str | None = None
        if isinstance(data, Mapping):
            self._read(data)

    def _read(self, data: Mapping[str, Any]) -> None:
        self._method = _text(data.get("method"))
        self._url = _text(data.get("url"))
        self._host = _text(data.get("host"))
        self._ip = _text(data.get("ip"))
        https = data.get("https")
        self._https = https if isinstance(https, bool) else None
        timeout = data.get("timeout")
        self._timeout = timeout if isinstance(timeout, int) and not isinstance(timeout, bool) else None
        self._query_string = _text(data.get("query"))
        if "body" in data:
            self._body = data["body"]
        self._headers = _text_map(data.get("headers"))
        self._cookies = _text_map(data.get("cookies"))
        self._session = _text_map(data.get("session"))
        parameters = data.get("parameters")
        if isinstance(parameters, Mapping):
            query = parameters.get("query")
            if isinstance(query, Mapping):
                for key, value in query.items():
                    if isinstance(value, list):
                        self._query[str(key)] = [str(v) for v in value]
                    else:
                        self._query[str(key)] = str(value)
            self._path = _text_map(parameters.get("path"))

    # ---- the wire shape ----

    def to_map(self) -> dict[str, Any]:
        """The engines' ``toMap()`` shape: ``parameters`` always carries both sub-maps, ``cookies``
        and ``session`` appear only when present, and a body set to None stays on the wire."""
        result: dict[str, Any] = {}
        if self._method is not None:
            result["method"] = self._method
        if self._url is not None:
            result["url"] = self._url
        if self._host is not None:
            result["host"] = self._host
        if self._headers:
            result["headers"] = dict(self._headers)
        if self._body is not _UNSET:
            result["body"] = self._body
        if self._ip is not None:
            result["ip"] = self._ip
        if self._https is not None:
            result["https"] = self._https
        if self._timeout is not None:
            result["timeout"] = self._timeout
        if self._query_string is not None:
            result["query"] = self._query_string
        if self._cookies:
            result["cookies"] = dict(self._cookies)
        if self._session:
            result["session"] = dict(self._session)
        result["parameters"] = {"query": dict(self._query), "path": dict(self._path)}
        return result

    # ---- reading ----

    @property
    def method(self) -> str:
        return self._method or "GET"

    @property
    def url(self) -> str:
        return self._url or "/"

    @property
    def target_host(self) -> str | None:
        return self._host

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self._headers.items():
            if key.lower() == lowered:
                return value
        return None

    @property
    def body(self) -> Any:
        return None if self._body is _UNSET else self._body

    @property
    def path_parameters(self) -> dict[str, str]:
        return dict(self._path)

    def path_parameter(self, name: str) -> str | None:
        return self._path.get(name)

    @property
    def query_parameters_map(self) -> dict[str, str | list[str]]:
        return dict(self._query)

    def query_parameter(self, name: str) -> str | None:
        """The first value of a query parameter."""
        value = self._query.get(name)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def query_parameters(self, name: str) -> list[str]:
        """Every value of a repeated query parameter."""
        value = self._query.get(name)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def cookie(self, name: str) -> str | None:
        return self._cookies.get(name)

    @property
    def session(self) -> dict[str, str]:
        return dict(self._session)

    def session_info(self, name: str) -> str | None:
        return self._session.get(name)

    @property
    def remote_ip(self) -> str | None:
        return self._ip

    @property
    def is_secure(self) -> bool:
        return bool(self._https)

    @property
    def timeout_seconds(self) -> int | None:
        """The entry's timeout in seconds; a caller-sent ``x-ttl`` (milliseconds) wins."""
        ttl = self.header("x-ttl")
        if ttl is not None:
            try:
                return max(1, int(ttl.strip()) // 1000)
            except ValueError:
                pass
        return self._timeout

    @property
    def query_string(self) -> str | None:
        return self._query_string

    # ---- building (the edge) ----

    def set_method(self, method: str) -> AsyncHttpRequest:
        self._method = method
        return self

    def set_url(self, url: str) -> AsyncHttpRequest:
        self._url = url
        return self

    def set_target_host(self, host: str) -> AsyncHttpRequest:
        self._host = host
        return self

    def set_header(self, name: str, value: str) -> AsyncHttpRequest:
        lowered = name.lower()
        self._headers = {k: v for k, v in self._headers.items() if k.lower() != lowered}
        self._headers[name] = value
        return self

    def set_body(self, body: Any) -> AsyncHttpRequest:
        self._body = body
        return self

    def set_query_parameter(self, name: str, value: str | list[str]) -> AsyncHttpRequest:
        self._query[name] = list(value) if isinstance(value, list) else value
        return self

    def set_path_parameter(self, name: str, value: str) -> AsyncHttpRequest:
        self._path[name] = value
        return self

    def set_cookie(self, name: str, value: str) -> AsyncHttpRequest:
        self._cookies[name] = value
        return self

    def set_session_info(self, name: str, value: str) -> AsyncHttpRequest:
        self._session[name] = value
        return self

    def set_remote_ip(self, ip: str) -> AsyncHttpRequest:
        self._ip = ip
        return self

    def set_secure(self, https: bool) -> AsyncHttpRequest:
        self._https = https
        return self

    def set_timeout_seconds(self, seconds: int) -> AsyncHttpRequest:
        self._timeout = seconds
        return self

    def set_query_string(self, query: str) -> AsyncHttpRequest:
        self._query_string = query
        return self


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v if isinstance(v, str) else str(v) for k, v in value.items()}
