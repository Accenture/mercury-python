"""REST automation, the pure half: the routing table ``rest.yaml`` declares and the rules of the
HTTP edge that need no server - the engines' grammar, keys, messages and error body, never a pack
dialect (RFC-0018 in mercury-composable).

``rest.yaml`` is the router: an entry maps a URL and its methods to a composable function, with
CORS, header transforms, an authentication function, a timeout, tracing and static content. This
module mirrors the Rust engine's ``automation/routing.rs`` and the pure rules of its
``automation/server.rs`` (the Java ``RoutingEntry``, ``HttpRouter`` and ``AsyncHttpResponse``):
the parser with its load-time refusals, the URL matching with its precedence, the header
transforms, the request-body dispatch by content type, the ``Accept`` negotiation, the payload
rendering and the mapping of a reply envelope to an HTTP response. Everything here is pinned by the
shared vector file ``tests/vectors/rest-automation-vectors.json``, byte-identical with the Node
pack.

What a pack refuses by name, so the grammar itself shows the scope fence: a ``flow`` binding
(``flow:`` or ``service: http.flow.adapter`` - flows run in the engines), the HTTP relay (an
``http(s)://`` service) and the A/B dual service (a list of two routes). Deferred as in the Rust
port: ``upload``, ``custom.content.types``, ``mime.types`` and XML.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import unquote_plus

import yaml

from .exceptions import AppException

ALLOWED_METHODS = ("GET", "PUT", "POST", "DELETE", "HEAD", "PATCH")
DEFAULT_TIMEOUT_MS = 30_000
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 300_000
FLOW_ADAPTER = "http.flow.adapter"
# the engines' built-in default endpoints (platform-core's default-rest.yaml): merged only when the
# application's rest.yaml does not already claim the URL - a user entry always wins
DEFAULT_REST_YAML = """
rest:
  - service: "event.api.service"
    methods: ['POST']
    url: "/api/event"
    timeout: 60s
    tracing: true
  - service: "info.actuator.service"
    methods: ['GET']
    url: "/info"
    timeout: 10s
  - service: "routes.actuator.service"
    methods: ['GET']
    url: "/info/routes"
    timeout: 10s
  - service: "env.actuator.service"
    methods: ['GET']
    url: "/env"
    timeout: 10s
  - service: "health.actuator.service"
    methods: ['GET']
    url: "/health"
    timeout: 30s
  - service: "liveness.actuator.service"
    methods: ['GET']
    url: "/livenessprobe"
    timeout: 10s
"""
# the reserved envelope metadata that never reaches the HTTP wire (the engines' protected
# metadata: the my_* keys of the worker's header view)
_PROTECTED_PREFIX = "my_"
HTML_START = "<html><body><pre>\n"
HTML_END = "\n</pre></body></html>"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_timeout(value: Any) -> int:
    """A duration like ``30s``, ``2m``, ``1500ms`` or a bare number of seconds, in milliseconds;
    the default 30 s when unparsable, clamped to 1 s - 5 min (the engines' rule)."""
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, (int, float)):
        parsed = int(value) * 1000
    elif isinstance(value, str):
        text = value.strip().lower()
        try:
            if text.endswith("ms"):
                parsed = int(text[:-2].strip())
            elif text.endswith("m"):
                parsed = int(text[:-1].strip()) * 60_000
            elif text.endswith("s"):
                parsed = int(text[:-1].strip()) * 1000
            else:
                parsed = int(text) * 1000
        except ValueError:
            parsed = None
    if parsed is None or parsed < 0:
        parsed = DEFAULT_TIMEOUT_MS
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, parsed))


@dataclass
class HeaderTransform:
    """A request or response transform of a ``headers`` block: ``keep`` retains only the listed
    names (and takes precedence over ``drop``, which is then ignored - the Java engine's rule),
    else ``drop`` removes the listed names; then ``add`` inserts - names matched
    case-insensitively."""

    add: list[tuple[str, str]] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)

    def apply(self, headers: Mapping[str, str]) -> dict[str, str]:
        result = dict(headers)
        if self.keep:
            keep = {name.lower() for name in self.keep}
            result = {name: value for name, value in result.items() if name.lower() in keep}
        else:
            for name in self.drop:
                lowered = name.lower()
                result = {k: v for k, v in result.items() if k.lower() != lowered}
        for name, value in self.add:
            lowered = name.lower()
            result = {k: v for k, v in result.items() if k.lower() != lowered}
            result[name] = value
        return result

    @property
    def empty(self) -> bool:
        return not (self.add or self.drop or self.keep)


@dataclass
class HeaderInfo:
    id: str
    request: HeaderTransform = field(default_factory=HeaderTransform)
    response: HeaderTransform = field(default_factory=HeaderTransform)


@dataclass
class CorsInfo:
    """A reusable ``cors`` block: the ``options`` lines answer the preflight, the ``headers`` lines
    ride every normal response; every line is an ``Access-Control-*: value``."""

    id: str
    options: list[tuple[str, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SimpleHttpFilter:
    """The static-content filter: a function that inspects the static requests matching ``path``
    but not ``exclusion`` (patterns: exact, ``prefix*`` or ``*suffix``)."""

    path_list: list[str]
    exclusion_list: list[str]
    service: str


@dataclass
class StaticContentConfig:
    """The ``static-content`` block: the pages served with no-cache headers instead of the ETag
    protocol (the entry pages, by default) and the optional request filter."""

    no_cache_pages: list[str] = field(default_factory=lambda: ["/", "/index.html"])
    filter: SimpleHttpFilter | None = None


def matched_element(patterns: list[str], path: str) -> bool:
    """A path against exact, ``prefix*`` and ``*suffix`` patterns (the engines' rule)."""
    for pattern in patterns:
        if pattern.endswith("*"):
            if path.startswith(pattern[:-1]):
                return True
        elif pattern.startswith("*"):
            if path.endswith(pattern[1:]):
                return True
        elif path == pattern:
            return True
    return False


def _valid_patterns(patterns: list[str]) -> bool:
    for pattern in patterns:
        if not pattern:
            return False
        stars = pattern.count("*")
        if stars > 1 or (stars == 1 and not (pattern.startswith("*") or pattern.endswith("*"))):
            return False
    return True


# a URL segment: a case-insensitive literal, a {param} capture, a bare * (any one segment) or a
# foo* prefix; the last two in trailing position let the URL run longer
_LITERAL, _PARAM, _ANY, _PREFIX = "literal", "param", "any", "prefix"


@dataclass
class RouteInfo:
    """One ``rest`` entry, parsed."""

    service: str
    methods: list[str]
    url: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    cors: CorsInfo | None = None
    headers: HeaderInfo | None = None
    authentication: str | None = None
    tracing: bool = False
    stream: bool = False
    trace_id_header: str | None = None
    correlation_id_header: str | None = None
    traceparent_header: str | None = None
    segments: list[tuple[str, str]] = field(default_factory=list)

    @property
    def timeout_seconds(self) -> int:
        return max(1, self.timeout_ms // 1000)

    def is_open_ended(self) -> bool:
        return bool(self.segments) and self.segments[-1][0] in (_ANY, _PREFIX)

    @property
    def literal_count(self) -> int:
        return sum(1 for kind, _ in self.segments if kind == _LITERAL)

    def match_path(self, request_segments: list[str]) -> dict[str, str] | None:
        """The ``{param}`` values when the segments match this entry (literals and prefixes
        case-insensitively; a trailing wildcard needs at least one remaining segment), else None."""
        if self.is_open_ended():
            if len(self.segments) > len(request_segments):
                return None
        elif len(self.segments) != len(request_segments):
            return None
        params: dict[str, str] = {}
        for index, (kind, value) in enumerate(self.segments):
            actual = request_segments[index]
            if kind == _LITERAL:
                if actual.lower() != value:
                    return None
            elif kind == _PREFIX:
                if not actual.lower().startswith(value):
                    return None
            elif kind == _PARAM:
                params[value] = actual
        return params


class AssignedRoute(NamedTuple):
    info: RouteInfo
    path_params: dict[str, str]


def _segments_of(url: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for part in (p for p in url.split("/") if p):
        if part.startswith("{") and part.endswith("}") and len(part) > 2:
            segments.append((_PARAM, part[1:-1]))
        elif part == "*":
            segments.append((_ANY, ""))
        elif part.endswith("*"):
            segments.append((_PREFIX, part[:-1].lower()))
        else:
            segments.append((_LITERAL, part.lower()))
    return segments


def _lookup(entry: Mapping[str, Any], key: str) -> Any:
    """A possibly dotted key: the direct key first, then the nested maps a YAML author may have
    written (``trace: {id: {header: ...}}``) - the engines' ConfigReader rule."""
    if key in entry:
        return entry[key]
    current: Any = entry
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _parse_header_lines(lines: Any, cors_id: str, kind: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(lines, list):
        return out
    for line in lines:
        if not isinstance(line, str):
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise AppException(400, f"cors '{cors_id}' {kind} line '{line}' is not 'name: value'")
        name = name.strip()
        if not name.lower().startswith("access-control-"):
            raise AppException(
                400, f"cors '{cors_id}' {kind} line '{name}' must be an Access-Control-* header")
        out.append((name, value.strip()))
    return out


def _parse_cors_blocks(section: Any) -> dict[str, CorsInfo]:
    blocks: dict[str, CorsInfo] = {}
    if not isinstance(section, list):
        return blocks
    for entry in section:
        if not isinstance(entry, Mapping):
            continue
        cors_id = _text(entry.get("id"))
        if cors_id is None:
            raise AppException(400, "cors block missing 'id'")
        blocks[cors_id] = CorsInfo(cors_id, _parse_header_lines(entry.get("options"), cors_id, "options"),
                                   _parse_header_lines(entry.get("headers"), cors_id, "headers"))
    return blocks


def _parse_transform(section: Any) -> HeaderTransform:
    transform = HeaderTransform()
    if not isinstance(section, Mapping):
        return transform
    add = section.get("add")
    if isinstance(add, list):
        for line in add:
            if isinstance(line, str) and ":" in line:
                name, _, value = line.partition(":")
                transform.add.append((name.strip(), value.strip()))
    for key, target in (("drop", transform.drop), ("keep", transform.keep)):
        names = section.get(key)
        if isinstance(names, list):
            target.extend(name for name in names if isinstance(name, str))
    return transform


def _parse_header_blocks(section: Any) -> dict[str, HeaderInfo]:
    blocks: dict[str, HeaderInfo] = {}
    if not isinstance(section, list):
        return blocks
    for entry in section:
        if not isinstance(entry, Mapping):
            continue
        header_id = _text(entry.get("id"))
        if header_id is None:
            raise AppException(400, "headers block missing 'id'")
        blocks[header_id] = HeaderInfo(header_id, _parse_transform(entry.get("request")),
                                       _parse_transform(entry.get("response")))
    return blocks


def _parse_static_content(data: Mapping[str, Any]) -> StaticContentConfig:
    result = StaticContentConfig()
    section = data.get("static-content")
    if not isinstance(section, Mapping):
        return result
    if "no-cache-pages" in section:
        pages = section.get("no-cache-pages")
        listed = [p for p in pages if isinstance(p, str)] if isinstance(pages, list) else []
        if not listed or not _valid_patterns(listed):
            raise AppException(400, "static-content.no-cache-pages has invalid syntax")
        result.no_cache_pages = listed
    if "filter" in section:
        filter_section = section.get("filter")
        paths = filter_section.get("path") if isinstance(filter_section, Mapping) else None
        if not isinstance(paths, list):
            raise AppException(400, "static-content.filter.path must be a list")
        path_list = [p for p in paths if isinstance(p, str)]
        service = _text(filter_section.get("service")) if isinstance(filter_section, Mapping) else None
        if service is None:
            raise AppException(400, "static-content.filter.service is required")
        exclusion = filter_section.get("exclusion") if isinstance(filter_section, Mapping) else None
        exclusion_list = [p for p in exclusion if isinstance(p, str)] if isinstance(exclusion, list) else []
        if not path_list or not _valid_patterns(path_list) or not _valid_patterns(exclusion_list):
            raise AppException(400, "static-content.filter path/exclusion has invalid syntax")
        result.filter = SimpleHttpFilter(path_list, exclusion_list, service)
    return result


def _parse_route(index: int, entry: Mapping[str, Any], cors_blocks: Mapping[str, CorsInfo],
                 header_blocks: Mapping[str, HeaderInfo]) -> RouteInfo:
    service_value = _lookup(entry, "service")
    if isinstance(service_value, list):
        raise AppException(
            400, f"rest[{index}] service must be a single route - the A/B dual service is not"
                 " available in a language pack")
    service = _text(service_value)
    if service is None:
        raise AppException(400, f"rest[{index}] missing 'service'")
    lowered = service.lower()
    if lowered.startswith(("http://", "https://")):
        raise AppException(
            400, f"rest[{index}] HTTP relay is not available in a language pack (service '{service}')")
    flow = _text(_lookup(entry, "flow"))
    if flow is not None or lowered == FLOW_ADAPTER:
        binding = f"flow '{flow}'" if flow is not None else f"service '{service}'"
        raise AppException(
            400, f"rest[{index}] flow binding is not available in a language pack - flows run in the"
                 f" engines ({binding})")
    if _lookup(entry, "upload") is True:
        raise AppException(400, f"rest[{index}] 'upload' is not available in this version of the language pack")
    url = _text(_lookup(entry, "url"))
    if url is None:
        raise AppException(400, f"rest[{index}] missing 'url'")
    raw_methods = _lookup(entry, "methods")
    if not isinstance(raw_methods, list):
        raise AppException(400, f"rest[{index}] missing 'methods' list")
    methods: list[str] = []
    for raw in raw_methods:
        method = _text(raw)
        if method is None:
            raise AppException(400, f"rest[{index}] method must be text")
        method = method.upper()
        if method not in ALLOWED_METHODS:
            allowed = ", ".join(f'"{m}"' for m in ALLOWED_METHODS)
            raise AppException(400, f"rest[{index}] invalid method '{method}' (allowed: [{allowed}])")
        methods.append(method)
    cors: CorsInfo | None = None
    cors_id = _text(_lookup(entry, "cors"))
    if cors_id is not None:
        cors = cors_blocks.get(cors_id)
        if cors is None:
            raise AppException(400, f"rest[{index}] unknown cors id '{cors_id}'")
    headers: HeaderInfo | None = None
    headers_id = _text(_lookup(entry, "headers"))
    if headers_id is not None:
        headers = header_blocks.get(headers_id)
        if headers is None:
            raise AppException(400, f"rest[{index}] unknown headers id '{headers_id}'")
    return RouteInfo(
        service=service, methods=methods, url=url,
        timeout_ms=parse_timeout(_lookup(entry, "timeout")),
        cors=cors, headers=headers,
        authentication=_text(_lookup(entry, "authentication")),
        tracing=_lookup(entry, "tracing") is True,
        stream=_lookup(entry, "stream") is True,
        trace_id_header=_text(_lookup(entry, "trace.id.header")),
        correlation_id_header=_text(_lookup(entry, "correlation.id.header")),
        traceparent_header=_text(_lookup(entry, "traceparent.header")),
        segments=_segments_of(url),
    )


def substitute_values(data: Any, resolve: Callable[[str], Any]) -> Any:
    """Every string of a loaded YAML document through the configuration's ``${ENV:default}``
    substitution (the engines resolve ``${api.origin:*}`` in a cors line the same way)."""
    if isinstance(data, str):
        resolved = resolve(data)
        return data if resolved is None else resolved if isinstance(resolved, str) else str(resolved)
    if isinstance(data, list):
        return [substitute_values(item, resolve) for item in data]
    if isinstance(data, Mapping):
        return {key: substitute_values(value, resolve) for key, value in data.items()}
    return data


@dataclass
class RoutingTable:
    """The parsed routing table (the engines' ``RoutingEntry``)."""

    routes: list[RouteInfo] = field(default_factory=list)
    static_content: StaticContentConfig = field(default_factory=StaticContentConfig)

    @classmethod
    def load(cls, data: Any) -> RoutingTable:
        """Load and validate a ``rest.yaml`` document (a parsed YAML mapping). An invalid entry
        fails the load with the engines' message - a misconfigured endpoint is a startup error."""
        if not isinstance(data, Mapping):
            raise AppException(400, "rest.yaml text must be a YAML mapping")
        cors_blocks = _parse_cors_blocks(data.get("cors"))
        header_blocks = _parse_header_blocks(data.get("headers"))
        entries = data.get("rest")
        if not isinstance(entries, list):
            raise AppException(400, "rest.yaml has no 'rest' section")
        routes: list[RouteInfo] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise AppException(400, f"rest[{index}] is not a map")
            routes.append(_parse_route(index, entry, cors_blocks, header_blocks))
        return cls(routes, _parse_static_content(data))

    @classmethod
    def from_yaml_text(cls, text: str) -> RoutingTable:
        return cls.load(yaml.safe_load(text))

    def has_url(self, url: str) -> bool:
        return any(route.url.lower() == url.lower() for route in self.routes)

    def add_route(self, route: RouteInfo) -> None:
        self.routes.append(route)

    def merge_defaults(self) -> None:
        """The engines' default endpoints, added when the file does not claim their URLs."""
        for route in RoutingTable.from_yaml_text(DEFAULT_REST_YAML).routes:
            if not self.has_url(route.url):
                self.add_route(route)

    def retain_available(self, is_registered: Callable[[str], bool]) -> list[tuple[str, str, str]]:
        """Drop every entry whose service is not registered, the engines' load-time rule (``Skip
        [GET] /api/x - Service x not available``): an entry whose function was left out must not
        become a live URL. Returns the skipped ``("[methods]", url, service)`` for the log."""
        skipped: list[tuple[str, str, str]] = []
        kept: list[RouteInfo] = []
        for route in self.routes:
            if is_registered(route.service):
                kept.append(route)
            else:
                skipped.append((f"[{', '.join(route.methods)}]", route.url, route.service))
        self.routes = kept
        return skipped

    def find(self, method: str, path: str) -> AssignedRoute | None:
        """The entry a request matches: every segment must match; a non-wildcard entry beats a
        wildcard one, then more literal segments win. ``OPTIONS`` matches any entry (the CORS
        preflight is auto-added per the grammar)."""
        segments = [s for s in path.split("/") if s]
        best: tuple[int, bool, AssignedRoute] | None = None
        for info in self.routes:
            if method != "OPTIONS" and method not in info.methods:
                continue
            params = info.match_path(segments)
            if params is None:
                continue
            literals, wildcard = info.literal_count, info.is_open_ended()
            if best is None or (not wildcard and best[1]) or \
                    (wildcard == best[1] and literals > best[0]):
                best = (literals, wildcard, AssignedRoute(info, params))
        return best[2] if best else None

    def path_matches_any_method(self, path: str) -> bool:
        """True when the path matches some entry regardless of method - a known path under a
        wrong method is 405, never 404."""
        segments = [s for s in path.split("/") if s]
        return any(info.match_path(segments) is not None for info in self.routes)


# ---- the pure rules of the edge ----


def url_decode(text: str) -> str:
    """Percent-decoding with ``+`` as a space, for path and query values."""
    return unquote_plus(text, errors="replace")


def parse_query(query_text: str) -> dict[str, str | list[str]]:
    """Query parameters: one occurrence is a string, a repeated key becomes a list (the engines
    keep every value)."""
    query: dict[str, str | list[str]] = {}
    for pair in (p for p in query_text.split("&") if p):
        name, _, value = pair.partition("=")
        name, value = url_decode(name), url_decode(value)
        existing = query.get(name)
        if existing is None:
            query[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            query[name] = [existing, value]
    return query


def parse_cookies(header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for item in header.split(";"):
        name, sep, value = item.partition("=")
        if sep:
            cookies[name.strip()] = value.strip()
    return cookies


def base_content_type(content_type: str | None) -> str | None:
    """The content type without a ``;charset=...`` suffix; the value matched case-sensitively."""
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip()


class ParsedBody(NamedTuple):
    """The request body by its declared content type: ``value`` (a map, a list, text or None),
    ``form`` (fields that become query parameters) or ``bytes``."""

    kind: str
    value: Any


def parse_body(content_type: str | None, data: bytes) -> ParsedBody:
    """The engines' content-type dispatch: ``application/json`` - an empty body is ``{}``, a body
    in matching brackets is parsed and a parse failure falls back to the raw text, anything else
    stays text, and there is no JSON sniffing under any other type; ``application/xml``,
    ``text/html`` and ``text/plain`` - text; ``application/x-www-form-urlencoded`` - form fields;
    anything else or no content type - raw bytes, an empty payload None."""
    ct = base_content_type(content_type) or "?"
    if ct.startswith("application/json"):
        text = data.decode("utf-8", errors="replace")
        trimmed = text.strip()
        if not trimmed:
            return ParsedBody("value", {})
        if (trimmed.startswith("{") and trimmed.endswith("}")) or \
                (trimmed.startswith("[") and trimmed.endswith("]")):
            try:
                return ParsedBody("value", json.loads(text))
            except ValueError:
                return ParsedBody("value", text)
        return ParsedBody("value", text)
    if ct == "application/x-www-form-urlencoded":
        fields: dict[str, str] = {}
        for pair in (p for p in data.decode("utf-8", errors="replace").split("&") if p):
            name, _, value = pair.partition("=")
            fields[url_decode(name)] = url_decode(value)
        return ParsedBody("form", fields)
    if ct.startswith(("application/xml", "text/html", "text/plain")):
        return ParsedBody("value", data.decode("utf-8", errors="replace"))
    if not data:
        return ParsedBody("value", None)
    return ParsedBody("bytes", bytes(data))


def accept_fallback_type(accept: str | None) -> str | None:
    """The response content type when the function set none: ``text/html`` for a browser,
    ``application/json`` for json, ``*/*`` and xml (the port's XML deferral), no ``Accept`` means no
    content-type header at all, anything else ``text/plain``."""
    if accept is None:
        return None
    if "text/html" in accept:
        return "text/html"
    if "application/json" in accept or "*/*" in accept or "application/xml" in accept:
        return "application/json"
    return "text/plain"


def negotiate_stream_type(accept: str | None) -> str:
    """The content type of a streamed response when the head sets none."""
    if accept is None:
        return "application/json"
    if "*/*" in accept or "application/json" in accept:
        return "application/json"
    if "text/event-stream" in accept:
        return "text/event-stream"
    if "text/html" in accept:
        return "text/html"
    if "application/xml" in accept:
        return "application/xml"
    return "text/plain"


def strip_nulls(value: Any) -> Any:
    """Map entries whose value is None are omitted (the engines' serializer default)."""
    if isinstance(value, Mapping):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def pretty_json(value: Any) -> str:
    """A map or list body as the engines render it: pretty-printed, two spaces, Unicode kept."""
    return json.dumps(strip_nulls(value), indent=2, ensure_ascii=False)


def compact_json(value: Any) -> str:
    return json.dumps(strip_nulls(value), separators=(",", ":"), ensure_ascii=False)


def stream_text(body: Any) -> str:
    """A segment body as line-oriented text: text as is, bytes as UTF-8, a structure as compact
    JSON (the engines' stream framing is line-oriented)."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return compact_json(body)


def render_payload(body: Any, content_type: str | None) -> bytes:
    """The response body per the effective content type: text and bytes ride raw regardless of
    the negotiated type; a map or list renders as pretty JSON, wrapped in the HTML shell when the
    type is ``text/html``."""
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    text = pretty_json(body)
    if content_type is not None and content_type.startswith("text/html") and isinstance(body, (Mapping, list)):
        return f"{HTML_START}{text}{HTML_END}".encode()
    return text.encode("utf-8")


def error_body(status: int, message: str) -> dict[str, Any]:
    """The engines' error shape: ``{"status": n, "message": "...", "type": "error"}``."""
    return {"status": status, "message": message, "type": "error"}


def error_payload(status: int, message: str) -> bytes:
    return pretty_json(error_body(status, message)).encode("utf-8")


class MappedResponse(NamedTuple):
    """A reply envelope mapped to HTTP: the status, the header lines in order (``set-cookie`` may
    repeat) and the body bytes."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes


def map_response(*, status: int, reply_headers: Mapping[str, str], body: Any, accept: str | None,
                 is_head: bool, transform: HeaderTransform | None, cors: CorsInfo | None,
                 cid: str, cid_header: str) -> MappedResponse:
    """The engines' mapping of a function's reply to the HTTP response. A failure - an error
    status with a text body and no headers of its own, the shape of a raised ``AppException`` -
    renders as the standard error body unless the text already looks like JSON or XML. The
    reserved ``my_*`` metadata and the streaming handles never reach the wire; a function-set
    ``content-type`` wins over the ``Accept`` negotiation (not for HEAD); ``set-cookie`` values
    separated by ``|`` become one header each; the entry's response transform filters the headers
    (content type and cookies bypass it); the correlation id is echoed under its header name
    unless the function set one; the CORS headers come last; a HEAD response carries no body."""
    standard_error = False
    if status >= 400 and isinstance(body, str) and not reply_headers:
        message = body.strip()
        if not message.startswith(("{", "[", "<")):
            body = error_body(status, message)
            standard_error = True
    content_type: str | None = None
    cookies: list[str] = []
    headers: dict[str, str] = {}
    for name, value in reply_headers.items():
        key = name.lower()
        if key.startswith(_PROTECTED_PREFIX):
            continue
        if key == "x-stream-id" and value.startswith("stream.") and ".in" in value:
            continue
        if key == "x-ttl":
            continue
        if key == "content-type":
            if not is_head:
                content_type = value.lower()
        elif key == "set-cookie":
            cookies.extend(c.strip() for c in value.split("|"))
        else:
            headers[key] = value
    if content_type is None and not is_head:
        content_type = accept_fallback_type(accept)
    if standard_error and content_type is None and not is_head:
        content_type = "application/json"
    payload = b"" if is_head else render_payload(body, content_type)
    if transform is not None:
        headers = transform.apply(headers)
    if not any(name.lower() == cid_header for name in headers):
        headers[cid_header] = cid
    if content_type is not None:
        headers["content-type"] = content_type
    if cors is not None:
        for name, value in cors.headers:
            headers[name.lower()] = value
    lines = list(headers.items())
    lines.extend(("set-cookie", cookie) for cookie in cookies if cookie)
    return MappedResponse(status, lines, payload)
