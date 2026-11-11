# Session Resource Statistics — Feature Specification

**Status:** Implemented. This document defines the command's behavior and verification requirements.

## 1. Goal and scope

Add `agentbox stats` to report CPU, RAM, and disk usage for all session containers, grouped by session, with both per-container measurements and session totals. Include the proxy, every concurrent ephemeral agent, retained persistent agents, and any other containers belonging to the session's Compose project.

The following behavior reflects the agreed product choices:

- **One snapshot per invocation**, not a continuously refreshing display.
- **Disk means writable-layer space**, not cumulative block I/O or read/write throughput.
- **Stopped containers are included**, with their disk usage retained and unavailable live measurements explicitly marked.

The command is read-only. It must not start containers, synchronize credentials, execute code inside containers, or modify session state.

## 2. Command-line contract

```text
agentbox stats [--name NAME | --session SESSION_ID] [--json]
```

| Argument | Meaning |
|---|---|
| No selector | Report all sessions across all projects registered in the active `$AGENTBOX_STATE/sessions/` directory, regardless of the working directory. |
| `--name NAME`, `-n NAME` | Select exactly one session by name in the current project, using the existing project-root discovery and session-name resolution. |
| `--session SESSION_ID`, `-s SESSION_ID` | Select exactly one session by its full global ID, as shown by `agentbox list --all`; works from any directory. |
| `--json` | Print one structured JSON snapshot instead of the table. |

`--name` and `--session` are mutually exclusive. An unknown session is an error, not an empty result. Unlike existing single-session commands, omitting both selectors **does not** auto-select the current project's only session: it selects every registered session. The help text must make this distinction explicit.

There is no `--all`, streaming mode, refresh interval, or `--no-stream` flag in this feature. Both output formats collect a snapshot and exit. Repeated observation can use an external tool such as `watch agentbox stats`.

Examples:

```bash
agentbox stats
agentbox stats --name feature
agentbox stats --session <full-session-id>
agentbox stats --json
agentbox stats --name feature --json
```

## 3. Discovery and identity

1. Enumerate registered session directories under the active state directory, reading `x-metadata.name` and `x-metadata.project-dir` from each session's `compose.yaml`. The directory name is the session ID. Missing `x-metadata.persist` means ephemeral, as elsewhere in the CLI; a present value must be boolean.
2. Derive each session's Compose project identity using the existing `_project_name()` helper. Match containers by the exact `com.docker.compose.project` label, including non-running containers. Do not match by name substrings or assume that a session contains exactly two containers.
3. Record the container's full ID, name, state, and Compose service label. A missing service label is represented as `null` and does not exclude an otherwise matching container.
4. Deduplicate by full container ID before collecting or summing measurements. Never count a container twice. A conflicting session-to-project mapping is a discovery error, not permission to duplicate the container across groups.

Only containers associated with registered sessions in the active state directory are in scope. Ignore unrelated Compose projects and orphaned containers with no matching registered session. Do not silently combine separate agentbox installations or rootful/rootless Podman stores; use the current user's configured Podman connection without privilege escalation.

Sort sessions by `(project, name, session_id)` and containers by `(service or "", name, id)`, using deterministic string ordering in both output formats.

Include registered sessions that have no containers, including sessions initialized but never started. Show an empty group with zero totals; do not create placeholder agent or proxy containers. If there are no registered sessions, the table prints `No sessions.` and JSON contains an empty `sessions` array. Both are successful results.

Invalid or unreadable session metadata must not silently disappear from an all-session result: report a discovery error identifying the affected session directory and continue with valid sessions. Explicit selection of such a session fails before measurement.

## 4. Measurements

### 4.1 Per-container fields

| Field | Definition |
|---|---|
| `cpu_percent` | Podman-reported CPU utilization for the container, on the same scale as `podman stats`: approximately `100.0` means one fully occupied logical CPU. Values may exceed `100.0`. |
| `memory_bytes` | Current RAM usage reported by Podman's container statistics, in bytes, using the same accounting as `podman stats`; not configured RAM capacity or memory limit. |
| `disk_bytes` | Podman's writable-layer size (`SizeRw`, collected with size inspection or an equivalent structured interface), in bytes. Available for stopped as well as running containers. |

CPU is a sampled utilization measurement, not cumulative CPU time or a lifetime average. Collect two batched raw Podman statistics samples approximately one second apart and compute `100 * delta(CPUNano) / delta(SystemNano)`, matching Podman's interval CPU accounting. Podman's first one-shot CPU percentage is a lifetime average, so it is not used directly. Do not substitute the first counter value or an unavailable sample with a fabricated `0`.

Disk usage excludes image/read-only layers, named volumes, bind-mounted directories, and host session files such as `input.bundle`, `output.git`, logs, and configuration. In particular, a `--no-git` host-mounted workspace is excluded. This is a container writable-layer measurement, **not** total project storage, uniquely allocated physical disk blocks, free filesystem space, or a promise of reclaimable space. Do not scan mount contents with `du` or add shared image sizes.

The initial command does not report RAM limits, RAM percentages, disk limits, network traffic, PIDs, or block I/O. These are not needed to satisfy the resource-usage contract and must not be confused with disk space.

### 4.2 Container state and unavailable values

- Running containers have CPU, RAM, and disk measurements where supported.
- Paused containers retain RAM and disk usage. Use runtime-supported CPU accounting; a confirmed zero is valid, but pausing is not a reason to discard their RAM from totals.
- Created, stopped, and exited containers have `cpu_percent: null` and `memory_bytes: null`, because live statistics do not apply. Their `disk_bytes` is still collected. These expected nulls are not errors.
- An applicable measurement that cannot be obtained is also `null`, but must have an associated error. Never represent unknown or unsupported data as zero, reuse stale measurements, or print non-finite JSON numbers.
- Preserve Podman's state string. Unknown or transitional states must not be assumed to have zero RAM/CPU; if applicability cannot be established, mark the live measurements unavailable and report an error.

Measurements are **host/runtime accounting**, not telemetry collected inside the agent's krun microVM. Guest-visible CPU, RAM, or filesystem usage can differ from what Podman reports. If a runtime, storage driver, permissions setting, or cgroup configuration cannot expose a metric, report it as unavailable; do not claim full guest coverage or bypass isolation to obtain it.

### 4.3 Session totals

Compute totals from unrounded container measurements in the same snapshot:

```text
totals.cpu_percent = sum(cpu_percent for live containers)
totals.memory_bytes = sum(memory_bytes for live containers)
totals.disk_bytes = sum(disk_bytes for all containers)
```

Here, live containers include running and paused containers. Known non-live containers contribute zero to the session's CPU and RAM totals even though their own live fields are `null`. Thus a fully stopped session has `0.0` CPU, `0` RAM, and its retained writable-layer disk usage. An empty session has zero for all three totals.

If any contributing measurement is unavailable, the corresponding **session total is `null`**, rather than a misleading partial sum. Other totals remain available. Unknown/transitional state or disappearance of a container must conservatively invalidate any total whose membership or measurement cannot be established.

CPU percentages are summed without clamping or dividing by host CPU count. RAM and disk totals sum Podman-reported usage, not limits or percentages. These totals are accounting sums, not measurements of exclusively owned host resources; shared-page and storage accounting retain Podman's limitations. No cross-session grand total is required.

## 5. Human-readable output

Print a single table-like report, visually grouping container rows under a session header. Each group must identify the session name, project directory, and full session ID so identical names in different projects cannot be confused. End each group with a clearly labeled `TOTAL` row.

Illustrative output (IDs shortened here):

```text
SESSION feature    PROJECT /home/user/projects/app
SESSION ID feature-<full-project-hash>
CONTAINER ID  NAME                 STATE    CPU %  RAM         DISK (RW)
b2c3d4e5f6a1  app-feature-agent-1  running  38.00  512.00 MiB  512.00 MiB
a1b2c3d4e5f6  app-feature-proxy    running   2.00   32.00 MiB    8.00 MiB
              TOTAL                        40.00  544.00 MiB  520.00 MiB

SESSION retained    PROJECT /home/user/projects/other
SESSION ID retained-<full-project-hash>
CONTAINER ID  NAME                 STATE    CPU %  RAM         DISK (RW)
c3d4e5f6a1b2  other-agent          exited       -          -  256.00 MiB
              TOTAL                         0.00        0 B  256.00 MiB
```

- Display CPU percentages with two decimal places and byte values in IEC units (`B`, `KiB`, `MiB`, `GiB`, etc.). Round only for display, after summation.
- Render unavailable or inapplicable fields as `-`, never as a measured zero. Errors on stderr distinguish measurement failure from expected stopped-container nulls.
- Container IDs may be shortened for display, but must remain unambiguous within the report. Preserve container names and the session identity; do not rely on color or terminal cursor control to convey grouping.
- An empty session group prints `(no containers)` and a zero `TOTAL` row.
- Output must remain readable when redirected; do not clear the terminal, rewrite rows, or emit ANSI controls. Escape control characters in names and project paths before rendering them.

## 6. JSON output

`--json` writes **exactly one JSON object and a trailing newline** to stdout. No table, progress output, diagnostic messages, or newline-delimited stream may be mixed into stdout. Pretty-printing is permitted.

The object has the following fields:

| Field | Type / meaning |
|---|---|
| `schema_version` | Integer `1`. Incompatible schema or unit changes require a new version. |
| `collected_at` | UTC RFC 3339 timestamp when collection finished. The snapshot is not atomic. |
| `complete` | Boolean: no discovery or applicable measurement failures occurred. Expected stopped-container nulls do not make it false. |
| `sessions` | Array of session objects, each with `session_id`, `name`, `project`, `persistent`, `containers`, and `totals`. |
| `errors` | Array of structured discovery/measurement errors; empty on success. |

Session identity fields are strings, `project` is an absolute project-directory path, and `persistent` is boolean. Each container object has `id` (full container ID), `name`, `service` (string or `null`), `state`, `cpu_percent`, `memory_bytes`, and `disk_bytes`. Each `totals` object has the same three metric keys. Byte measurements are non-negative integers or `null`; CPU percentages are non-negative JSON numbers or `null`. No units or percent signs are embedded in numeric values.

For a session with one proxy and one agent:

```json
{
  "schema_version": 1,
  "collected_at": "2026-11-11T12:00:00Z",
  "complete": true,
  "sessions": [
    {
      "session_id": "feature-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "name": "feature",
      "project": "/home/user/projects/app",
      "persistent": false,
      "containers": [
        {
          "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "name": "app-feature-agent-1",
          "service": "agent",
          "state": "running",
          "cpu_percent": 38.0,
          "memory_bytes": 536870912,
          "disk_bytes": 536870912
        },
        {
          "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "name": "app-feature-proxy",
          "service": "proxy",
          "state": "running",
          "cpu_percent": 2.0,
          "memory_bytes": 33554432,
          "disk_bytes": 8388608
        }
      ],
      "totals": {
        "cpu_percent": 40.0,
        "memory_bytes": 570425344,
        "disk_bytes": 545259520
      }
    }
  ],
  "errors": []
}
```

Every error has `code`, `message`, `session_id`, `container_id`, and `metric`. Use `null` for an identity or metric not applicable to the error. `metric`, when present, is one of the three metric keys; a container-level error affecting multiple metrics can use `null`. Codes identify failure categories, such as `session_metadata_unreadable`, `container_discovery_failed`, `container_disappeared`, or `metric_unavailable`; consumers must tolerate new codes and additional fields.

For example, an unavailable disk measurement leaves the container and its other metrics in place, sets its `disk_bytes` and the session's `totals.disk_bytes` to `null`, sets `complete` to `false`, and adds:

```json
{
  "code": "metric_unavailable",
  "message": "Writable-layer size is unavailable for this container.",
  "session_id": "feature-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "container_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "metric": "disk_bytes"
}
```

## 7. Collection, consistency, and failures

- Reuse existing session identity/resolution helpers, but separate global enumeration from the current `_list_sessions()` status probing. With explicit selectors, discover and measure only that session's containers.
- Collect structured Podman output, not the human-readable `podman stats` table. Use one-shot statistics for live containers and size inspection for all matching containers; normalize to the numeric JSON contract above. Prefer raw numeric fields and preserve source precision. If a supported interface exposes unit-bearing values, normalize documented units explicitly, distinguishing SI and IEC units; do not infer missing values.
- Batch discovery, statistics, and inspection where supported, avoiding a separate stats sampling delay per container. Do not request sizes for unrelated containers. Writable-layer inspection can be expensive, which is one reason this command is a single snapshot rather than a polling loop.
- Never invoke unqualified `podman stats` with an empty target list: it can select unrelated containers. An empty selection requires no stats sampling. A missing/empty session registry should succeed without requiring a Podman call.
- Treat collection as a best-effort, non-atomic observation. Record discovery state, then measure that set of IDs. Containers or sessions created after discovery appear on the next invocation. Do not acquire lifecycle or secret synchronization locks, and do not block `start`, `stop`, or `remove` while collecting statistics.
- If a discovered container exits during collection, obtain its current state and disk usage if possible; confirmed non-live state uses the stopped-container rules. If it disappears, retain its discovered identity and last observed state, mark affected measurements/totals unavailable, and report `container_disappeared`. Do not relabel a removed container as a measured zero or silently omit it from totals.
- An error obtaining one container or metric must not discard successful measurements from other containers or sessions. A failed container-discovery operation makes that session's totals unknown, not zero. Malformed session metadata may prevent producing a session object, but must still produce an error identifying the session ID.
- Once selection succeeds, collection failures still render the available report. JSON uses `complete: false` and structured errors; table output shows unavailable fields. Also print concise diagnostics to stderr. Fatal argument/session-resolution errors occur before collection and leave stdout empty.
- Error messages must not expose container environments, credentials, or complete raw inspect payloads. The stats path requires no provider configuration or secret reads.

Exit statuses:

| Status | Meaning |
|---|---|
| `0` | Complete snapshot, including no sessions, no containers, or fully stopped sessions. |
| `1` | Session resolution, discovery, Podman, or applicable measurement failure; a partial report may be present. |
| `2` | Invalid command-line syntax, including simultaneous selectors. |
| `130` | Interrupted by the user, consistent with the existing CLI. |

## 8. Implementation and documentation outline

1. Add the `stats` parser and `cmd_stats` handler in `bin/agentbox`, with mutually exclusive selectors and `--json`. Reuse session enumeration and projected container discovery for existing commands too: avoid per-container state inspection and name-substring activity detection. `containers` includes the Compose `service` label and numeric PID alongside full IDs.
2. Factor discovery and measurement into helpers returning the structured snapshot. Keep aggregation separate from rendering so table and JSON use identical data and totals. Avoid importing current per-container inspection overhead into a new per-container sampling loop.
3. Add focused tests in `tests/test_stats.py`, following the existing `unittest`, `runpy`, temporary state-directory, and mocked subprocess patterns.
4. When implementing the feature, document it in the monitoring section of `README.md` and the observation/CLI sections of `docs/SPEC.md`. Explicitly call out global default selection, snapshot-only output, stopped-container handling, and disk-space exclusions.
5. Verify the actual structured stats fields, CPU sampling behavior, size-inspection support and precision, and cgroup/krun accounting on supported Podman installations. Opt-in real-runtime tests run with `AGENTBOX_TEST_PODMAN=1 python3 -m unittest discover -s tests -p test_stats.py -k StatsPodmanTest -v`. Set `AGENTBOX_TEST_IMAGE` to an existing local image with `/bin/sh` (default: `localhost/agentbox-stats-test:latest`); tests do not pull images. Rootless and krun coverage depend on the test host's runtime support.

Implementation-workspace verification used Podman 5.8.9, podman-compose 1.6.0, rootful crun, and cgroup v2. Real tests cover CLI table/JSON output, exact service labels, concurrent agents, session selection, paused RAM, retained disk after stop, container removal, and Compose stop/restart with the same container IDs. Rootless operation and krun accounting were not exercised; this host has neither `/dev/kvm` nor a krun/crun-vm runtime.

## 9. Acceptance tests

- **Global selection:** With sessions in two projects, plain `stats` includes both, even when invoked inside one project or outside a repository. Identical session names remain separate. A custom `AGENTBOX_STATE` restricts discovery to that registry.
- **Explicit selection:** `--name`/`-n` selects only the current project's matching session; `--session`/`-s` works from any directory. Other sessions are not measured. Unknown selectors, simultaneous selectors, and invalid metadata fail as specified. One local session does not alter the global default.
- **Container membership:** Include a proxy, multiple concurrent ephemeral agents, a persistent pair, stopped containers, and additional Compose services. Exclude unrelated projects and orphaned containers. Missing service labels are tolerated; duplicate IDs are not counted twice.
- **Aggregation:** Verify exact CPU/RAM/disk sums, CPU above `100%`, and summation before rounding. Missing disk or live statistics null only the affected totals. A paused container retains RAM in totals. Fully stopped and empty sessions have zero CPU/RAM totals, with stopped disk preserved.
- **Disk semantics:** Use only writable-layer size. Do not sum `SizeRootFs`, shared images, bind mounts, volumes, or host session files. Verify a stopped persistent agent still reports its retained writable-layer usage and that `--no-git` does not trigger host-directory scans.
- **Table:** Verify deterministic grouping/order, duplicate-name disambiguation, per-container rows, one `TOTAL` row per session, IEC units, unavailable markers, and readable redirected output without terminal control sequences.
- **JSON:** Parse stdout as exactly one JSON object, validate types/full IDs/timestamp/schema version, and verify totals match the table's underlying data. Expected stopped-container nulls are successful; genuine unknowns produce `complete: false`, structured errors, and exit `1`. No diagnostics appear on stdout.
- **Empty cases:** No state directory, an empty registry, and initialized sessions with no containers succeed. Never call stats with an empty ID list or accidentally collect unrelated containers.
- **Failures and races:** Cover Podman absent or inaccessible, malformed backend output, unsupported stats or writable-layer size, permission/cgroup failures, invalid session metadata, partial discovery failure, a container exiting, and a container/session being removed during collection. Preserve valid results and do not substitute unknown values with zero.
- **Read-only operation:** No lifecycle commands, Compose mutations, container exec, filesystem scans of mounts, credential synchronization, or lifecycle locks. Collection does not interfere with concurrent lifecycle commands.
- **Real-runtime verification:** On rootless Podman with the supported Compose provider, exercise ephemeral concurrency and running/stopped persistent sessions under krun. Compare CPU/RAM with Podman's one-shot stats and disk with writable-layer inspection, allowing for collection timing; confirm documented unavailable-metric behavior where accounting is unsupported.
