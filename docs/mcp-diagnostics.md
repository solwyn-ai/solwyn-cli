# MCP configuration and diagnostics

Use an installed Solwyn wheel or an installed development checkout. The package
has not been assumed to exist on PyPI. These commands do not install a package,
modify a host's configuration, or store credentials.

```sh
solwyn --json --project proj_0123456789abcdef01234567 mcp-config
solwyn --json --project proj_0123456789abcdef01234567 doctor --mcp
```

`mcp-config` is offline. It resolves this interpreter's installed `solwyn` console
executable to an absolute path (`Scripts/solwyn.exe` on Windows), and prints the
`solwyn.mcp-config.v1` envelope with `command` and `args`. Extract those fields
for the configuration format your host supports. Arguments remain separate array
entries, including paths with spaces. Global `--project` and `--api-url` precede
`mcp`; `--profile` follows it. The default generated profile is `read`.

Use `mcp-config --executable /absolute/path/to/solwyn` to select another trusted
installed executable. A missing or non-executable explicit target is an error;
it never falls back to another installation. The generator does not run that
executable. Configure credentials separately in the host environment or OS
keychain. No secret values or host-specific interpolation placeholders appear in
the descriptor. `--json` follows the CLI's usual schema-bearing output convention;
errors use stderr.

## Reading a diagnostic result

`doctor --mcp` emits `solwyn.doctor.mcp.v1`; plain `doctor` keeps its existing
checks and output. MCP diagnostics default to `--profile read`. Select
`--profile guardrails` to check the full catalog. `--executable` selects the
trusted Solwyn server executable to run for discovery and a possible read.

The result distinguishes:

- `selected_project` and `project_source`: the CLI, environment, or configuration
  selector used for reads, or `none` when absent.
- `credential`: captured kind and source, with separate statuses for available,
  empty, invalid, missing, incomplete, and unavailable credentials. A stored
  login is chosen before `SOLWYN_API_KEY`. Without one, an empty or invalid
  `SOLWYN_API_KEY` prevents fallback to the stored project key.
- `key_context`: the supplied key's actual project and `full` or `read_only`
  scope, obtained with `GET /api/v1/auth/key`. This is independent of selected
  project. A wrong or missing selector does not hide a known key identity.
  The response must contain a canonical Core project ID (`proj_` followed by
  24 lowercase hex characters). Malformed remote identity yields a fixed failed
  check with unknown project and scope.
- `http_read`: a separate selected-project budget GET, or an account GET for
  a captured login with no project selected. Requests never refresh credentials.
- `mcp`: discovery status, identity and version match against this CLI
  installation, negotiated protocol, observed known tools, unexpected-tool
  count, and comparison with the actual local profile catalog. Remote free text,
  unknown names and arbitrary server versions are not copied into the output.
- `mcp_read`: the result of one read-only `budget_status` call using the captured
  project key. No budget check, confirmation, recording or provider call occurs.
- `tools`: discovered flags and credential constraints from the actual catalog.
  Account alert history requires a Solwyn login; project alert history accepts
  project keys.

Login diagnostics use the captured login only for the parent HTTP check. The child
has no supported environment interface for a captured login, so its authenticated MCP
read is explicitly **skipped**. Discovery does not validate credentials. The
result remains `degraded` with exit 1 when an authenticated MCP read was skipped,
including login-only diagnostics. Exit 0 means discovery, context and the selected
key read all passed; exit 2 means usage error.

A 404 from the identity route means `unsupported`: deploy the Core version that
includes the read-only key identity endpoint before expecting scope diagnostics.
A successful budget read never implies full scope. Profiles expose tools; they
do not authorize spending, and full key scope does not guarantee a later budget
check will allow spend. Diagnostics do not rotate, revoke, refresh or save keys.

## Process and privacy boundaries

The SDK client runs in a small packaged helper with the current interpreter's
`-I` isolated mode. Only an explicit OS environment baseline reaches it; ambient
provider credentials, Python bootstrap settings, tracing context, exporter
settings and unrelated Solwyn variables are omitted. Child-only OTel settings
are established before SDK imports. Parent tracer and propagator objects are
untouched. The helper uses the public SDK `Client` and stdio transport, suppresses
its own raw SDK stderr, and emits only a capped typed result. The intended API
key travels over a private capped input pipe and into the server's supported
`SOLWYN_API_KEY`; it is never an argument or output field.

The helper accepts at most 8 KiB of private input and the parent accepts at most
16 KiB of output. Discovery and the MCP read have a 10-second internal deadline.
Parent cancellation closes the liveness pipe first and allows 10 seconds for SDK
cleanup. Normal completion cancels the pipe watcher even while the parent keeps
stdin open. A forced helper termination reports `cleanup_failed`; it cannot
prove that the server terminated. Parent HTTP probes retain the CLI's fixed
HTTP timeouts and disable auth and rate-limit retries.

HTTP client setup or cleanup errors produce a fixed failed HTTP check, without
copying exception text or environment values. Any already verified key identity
is retained, and independent executable and MCP checks still run. For example,
an invalid proxy or unavailable certificate file can fail the parent HTTP
checks while discovery succeeds in the isolated helper.

This lifecycle targets the controlled Solwyn server. The SDK can clean up an
active server and its process group, but an arbitrary descendant may survive if
its leader has already exited; no general descendant guarantee is made. Isolated
mode also does not make an environment-level `sitecustomize` or `.pth` tracing
bootstrap trustworthy. Use a controlled installed Python environment.

See the [quickstart](mcp-quickstart.md) for the tested clients and wheel acceptance
command. The wheel CI matrix executes this packaged doctor on Linux/macOS/Windows
with Python3.11; local observations so far cover macOS.

The local tests exercise macOS process behavior and Windows pipe-adapter branches.
The adapter uses documented Win32 anonymous-pipe APIs compatible with Python
3.11; branch tests do not prove Windows OS behavior. Actual Linux/Windows CI
results remain pending until the jobs run; particular desktop host versions have
not been exercised.
