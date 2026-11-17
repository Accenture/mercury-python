# The built-in agent tools

The essential tools every second agent use case needs - **files** inside one workspace, **the web** inside an allowlist
of sites, and **a clock** - as composable functions of a pack extension (RFC-0013 in the mercury-composable repository,
`draft-design-specs/agent-essential-tools.md`; the knowledge-graph tools and `file.extract` follow in the next step). A
graph's agent loop (`llm.run` in the engines) names a route of this application in its tool catalog, the engine reaches
the route through declarative Event-over-HTTP like any other function, and the model's arguments arrive as the event
body. The loop, the catalog and the graph see nothing special about a built-in tool: **a route names the capability**
(`file.read`, `web.fetch`), never the implementation, so a built-in may later be replaced by an MCP tool behind the
same route, or the other way round.

Three principles shape the extension:

- **The sandbox is deployment configuration, never an argument of a call.** Which directory tree the file tools may
  touch and which sites the web tools may reach are keys of this application's configuration, certified with the
  deployment like the MCP binding's server file. **A tool whose sandbox is not configured does not register**, so an
  unconfigured tool is a 404 to the loop's pre-flight check, not an open door.
- **Every refusal is a 400 that names the rule.** The loop returns it to the model as that call's error, the model
  reads it and chooses another step, and the node never fails because of a tool.
- **Results are text, capped, and untrusted.** A tool returns text or a small map, never a binary; it caps its own
  result; and what it returns is data a model reads, never an instruction the loop obeys. A page that says "ignore
  your instructions" is a page.

The Node.js twin (mercury-nodejs `extensions/agent-tools`) serves the same routes with the same catalog rows.

## Run

```bash
pip install mercury-composable                      # no extra: files, web and clock add no dependency
mercury-serve extensions/agent-tools/agent_tools.py -Dtools.file.root=/srv/workspace -Dtools.web.allow=docs.example.com
python extensions/agent-tools/agent_tools.py        # the same without mercury-serve
```

The extension reads `resources/application.yml` next to `agent_tools.py`, whichever directory the command runs from;
`-Dkey=value` overrides any key, and a value may carry `${ENV_VAR:default}` references. It listens on port 8089 by
default (`rest.server.port`). Its boot log says what registered:

```text
File tools registered - workspace /srv/workspace
Web tools registered - 1 allowed site pattern(s)
Built-in tools ready - file.read, file.write, file.list, file.search, web.fetch, web.crawl, time.now
```

Without `tools.file.root` the file tools stay out (`File tools not registered - tools.file.root is not set`); without
`tools.web.allow` the web tools stay out; `time.now` registers unless `tools.time=false`. A root that is not a
directory, an allowlist entry that is not a host pattern or a limit that is not a number stops the application by name.

## The tools

### Files, inside one workspace

The workspace is `tools.file.root`, one directory tree. Every `path` is relative to it, resolved with symbolic links
followed, and refused when the result is outside the root (`..`, an absolute path, a link that escapes). Hidden entries
(a name starting with `.`) are invisible to every file tool unless `tools.file.hidden=true`: a `.env` or a `.git`
folder inside the workspace stays out of a model's reach by default. Run the process as a user with no rights beyond
the tree.

| Route | Row | Arguments | Result |
| --- | --- | --- | --- |
| `file.read` | `file_read` | `path` (required); `max_bytes` (default and cap `tools.file.max.bytes`, 256 KiB) | `{path, bytes, text, truncated}` - `bytes` is the file's size, `truncated` says the text was cut at `max_bytes`; UTF-8 text only, a binary file is a 400 naming the detected type (a PDF, a PNG image, a ZIP archive or an Office document, …) |
| `file.write` | `file_write`, **`write: true`** | `path`, `content` (required); `mode`: `create` (default), `overwrite`, `append` | `{path, bytes, mode}`; `create` refuses an existing file with a 409; parent folders are created inside the root; the content is bounded by `tools.file.max.bytes`; `tools.file.write.extensions` may restrict the suffixes |
| `file.list` | `file_list` | `path` (a folder, default `.`); `glob` (optional, `*.md` or `**/*.md`); `max_entries` (default 200, cap 2000) | `{entries: [{path, type, bytes, modified}], truncated}`, sorted by path; `type` is `file`, `directory` or `other`; `modified` in UTC |
| `file.search` | `file_search` | `pattern` (required; a literal, or a regular expression with `regex: true`); `path` (default `.`); `glob` (default `**/*`); `case_sensitive` (default false); `context` (default 0, cap 3); `max_results` (default 100, cap 1000) | `{matches: [{path, line, text, before, after}], files_searched, truncated}`; text files only, binaries skipped, each line cut to `tools.file.line.max.chars` (500) |

`file.write` carries the write mark, so under the node's default mode a person approves it; the other three are reads
and run unasked.

### The web, inside an allowlist

The web tools register only when `tools.web.allow` names at least one host pattern. A URL is fetched only when its host
matches, over `https` (an `http` host must be listed with its scheme), and **every redirect is checked against the same
list**. The host is resolved before the request, every address it resolves to is checked, and the request is made to
the address that was checked, so a second resolution cannot point elsewhere. Addresses in loopback, private,
link-local, multicast, reserved and unspecified ranges are refused unless `tools.web.allow.private=true`, which closes
the request-forgery route to a cloud metadata service or an internal admin port. The download stops at
`tools.web.max.bytes` (1 MiB), the request at `tools.web.timeout.ms` (10 s), cookies are never kept, and the tool
identifies itself as `Mercury-Composable-AgentTools/<version>`.

| Route | Row | Arguments | Result |
| --- | --- | --- | --- |
| `web.fetch` | `web_fetch` | `url` (required); `as`: `text` (default), `markdown`, `raw` | `{url, status, content_type, title, text, bytes, truncated}`; HTML is reduced to its readable text (scripts, styles, `nav`, `header` and `footer` dropped; headings, lists, code and links kept as text or as Markdown); a text resource (`text/*`, JSON, XML) is returned as is; `raw` returns the body as received; a binary response is a 400; the page's own status (a 404, a 500) is a result, not an error |
| `web.crawl` | `web_crawl` | `url` (the start page); `max_pages` (default 10, cap `tools.web.crawl.max.pages`, 50); `max_depth` (default 2, cap 5); `include` (a path prefix the followed links must start with) | `{pages: [{url, title, text, bytes}], visited, truncated}`; same host only, links followed breadth-first, `robots.txt` honoured, `tools.web.crawl.delay.ms` (500) between requests, each page's text cut to `tools.web.page.max.chars` (20 000), the whole result to 60 000 characters |

The crawl is a bounded reader of a documentation section or a small site, not a spider: the page cap, the depth, the
include prefix and the delay are all limits, and a model that needs more asks again with a narrower start. HTML is read
by a small in-house parser (no dependency), enough for documentation and articles; an installation whose pages need
main-content extraction may put a library behind the same route.

### The clock

`time.now` (`time_now`) takes an optional `timezone` (an IANA name, default `UTC`) and returns
`{utc, local, timezone, offset, weekday, epoch_ms}`. It reads nothing and has no sandbox to miss. A system without a
time zone database (some containers) needs the `tzdata` package for any zone but UTC.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `tools.file.root` | unset | the workspace; unset, the file tools do not register |
| `tools.file.max.bytes` | `262144` | the most bytes `file.read` returns and `file.write` accepts |
| `tools.file.hidden` | `false` | expose hidden entries (a name starting with `.`) to the file tools |
| `tools.file.write.extensions` | any | the suffixes `file.write` accepts, comma-separated (`.md, .txt`) |
| `tools.file.line.max.chars` | `500` | the longest line `file.search` returns |
| `tools.web.allow` | empty | the host patterns the web tools may reach, comma-separated: `docs.example.com`, `*.example.org`, `http://intranet.example`, `host:8443`; empty, the web tools do not register |
| `tools.web.allow.private` | `false` | allow addresses in loopback, private, link-local and multicast ranges |
| `tools.web.max.bytes` | `1048576` | the most bytes read from one response |
| `tools.web.timeout.ms` | `10000` | how long one request may take |
| `tools.web.crawl.max.pages` | `50` | the most pages one crawl may ask for; the tool's cap, a deployment may lower it |
| `tools.web.crawl.delay.ms` | `500` | the pause between two requests of a crawl |
| `tools.web.page.max.chars` | `20000` | the characters kept of a page's text |
| `tools.time` | `true` | register `time.now` |

Limits are defaults a deployment lowers, never raises past the tool's caps (a `tools.web.crawl.max.pages` above 50 is
logged and capped). The shipped `application.yml` takes the root and the allowlist from `${TOOLS_FILE_ROOT:}` and
`${TOOLS_WEB_ALLOW:}`, so a deployment sets two environment variables or two `-D` flags.

## The function contract

| | |
| --- | --- |
| Request | The event body is the tool's arguments, a map of the row's properties. Anything else, an unknown argument or a wrong type is a 400 naming it. |
| Reply | The result map of the table above. |
| 400 | A refusal by rule: a path outside the workspace, a hidden entry, a binary file, a site that is not allowed, a private address, a bad argument, too many redirects, a response that is not text. The message names the rule and the key that governs it. |
| 404 | The file or folder does not exist. |
| 408 | The site did not answer within `tools.web.timeout.ms`. |
| 409 | `file.write` in `create` mode found the file. |
| 502 | The host cannot be resolved or the connection failed. |

A Java or Rust engine maps the routes in its `yaml.event.over.http` file to this application's `/api/event`, and a
graph's catalog row names the route:

```yaml
event:
  http:
    - route: 'file.read, file.write, file.list, file.search, web.fetch, web.crawl, time.now'
      target: 'http://127.0.0.1:8089/api/event'
```

**Logs.** A tool logs its route, the status, the byte, entry or page count and the host of a URL - never a path's
content, a page's text or a full URL (a query string may carry a token). The engines' trace names the route like any
function's.

## The catalog rows

Each tool ships the row an author copies onto the graph: the name the model sees, the description it reads, the input
schema the loop validates against (a closed object - an unknown argument is refused), the route and the write mark. No
row sets `strict`; the graph's author adds it when the model in use constrains optional properties by grammar.

```bash
python extensions/agent-tools/agent_tools.py describe              # every row
python extensions/agent-tools/agent_tools.py describe file_write time.now
```

```text
# 7 built-in tool(s) - the catalog rows a graph copies, and what registers here

## file.read (file_read) - registers (workspace /srv/workspace)
{
  "name": "file_read",
  "description": "Read a UTF-8 text file from the agent's workspace. ...",
  "input_schema": { "type": "object", "properties": { ... }, "required": ["path"], "additionalProperties": false },
  "route": "file.read"
}
```

The same rows are kept as files under [`catalog/`](./catalog) (`file_read.json`, …) for a copy without running
anything; a test pins each file to the row the module publishes, byte for byte, and the Node.js twin carries the same
files. A catalog node on a graph takes them as its `tools` list:

```json
[ { "name": "file_read", "route": "file.read", "description": "...", "input_schema": { ... } },
  { "name": "file_write", "route": "file.write", "write": true, "description": "...", "input_schema": { ... } } ]
```

## Health

`tools.health` (the `/health` actuator, `mandatory.health.dependencies`) reports the registered routes, the workspace
and the allowed site patterns - the answer to "what can this agent reach?" without reading code.

## Security

- **The catalog is still the boundary.** A graph names the tools its agent reaches; a built-in the graph does not name
  is as unreachable as any route, and a deployment whose sandbox is not configured fails the graph's pre-flight check
  before a token is spent.
- **The root, the allowlist and the private-address gate are certification artifacts**, reviewed with the deployment.
- **No shell, no arbitrary request.** A URL the model chooses is checked by host, scheme, port, resolved address and
  on every redirect; a path the model chooses is resolved and checked against the root. There is no generic
  `http.request` and no command execution, by design.
- **Untrusted content.** A page or a file may carry instructions; the loop never obeys a result, and a graph's system
  prompt should say that fetched and stored content is data.

## Tests

```bash
pip install -e '.[dev]'
pytest tests/test_agent_tools.py
```

The file tools run against a workspace built in a temporary directory (a link that escapes it, hidden entries, a
binary, a file that is not UTF-8, a large file); the HTML reader against a fixture page; the allowlist and the
address rules without a network; the web tools against a small site served in-process by aiohttp (pages that link each
other, `robots.txt`, redirects to allowed, private and foreign hosts, a slow page, a large body, charsets); the boot,
the health route, the `describe` output and the catalog files complete the suite.
