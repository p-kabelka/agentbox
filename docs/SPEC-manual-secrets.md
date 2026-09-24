# Manual Provider Secret Synchronization - Feature Specification

## 1. Overview

Provider credential bind mounts are replaced with a proxy-local tmpfs populated by explicit `agentbox` commands. Synchronization runs only during:

1. `agentbox start`
2. `agentbox proxy-reload`
3. `agentbox proxy-restart`

The current session's `proxy-config/proxy.yaml` is the only source of provider secret declarations. While a session is running, users apply provider configuration or credential-file changes with `proxy-reload` or `proxy-restart`. If no session is running, the next `start` applies them.

There is no watcher, polling loop, event listener, daemon, background retry, or automatic response to host file changes. The feature is limited to provider credentials selected by `api_key_file`; environment updates, OAuth discovery, and arbitrary file synchronization retain their existing behavior.

---

## 2. Secret Store and Source

### 2.1 Proxy Tmpfs

Newly generated Compose files must not contain host secret paths, provider credential bind mounts, generated target names, or credential values. Remove the broad `${AGENTBOX_HOME}/secrets:/run/secrets` mount and all per-provider file mounts.

The proxy receives an empty, root-owned writable tmpfs at `/run/secrets`:

```yaml
services:
  proxy:
    tmpfs:
      - /run/secrets:rw,noexec,nosuid,nodev,mode=0700
```

The exact syntax must be verified against the supported `podman-compose` version. The tmpfs has no feature-specific size limit; normal host and container memory limits apply.

`/run/secrets` is managed by Compose for synchronized provider credentials. The CLI and secret helper use that directory directly; mount type, permissions, and volume destinations are not independently checked.

The current mount logic is localized in `compose-base.yaml:20-22` and `generate_compose()` at `bin/agentbox:243-284`. Replace those mounts in place rather than adding a second Compose-generation path.

### 2.2 Configuration Authority

Every synchronization reads:

```text
<session-dir>/proxy-config/proxy.yaml
```

Secret references must not be derived from the initialization preset, Compose volumes, `${AGENTBOX_HOME}/secrets` contents, previous synchronization metadata, or installed tmpfs files.

Compose metadata may provide `x-metadata.project-dir` for relative path resolution, but it cannot declare a secret.

Session config creation, the `/config` mount, project metadata, and proxy-side reads already exist at `bin/agentbox:234-245,449-465` and `proxy/addons/addon.py:13,85-87`. Synchronization must reuse those paths but read the current session file, not the initialization preset currently passed to Compose generation.

---

## 3. Discovery and Naming

### 3.1 Credential References

For every enabled provider in the configuration snapshot:

1. If the provider has no `injection_policy`, treat the provider object as one top-level injection policy.
2. Inspect every provider-level `injection_policy` entry.
3. Inspect every `injection_policy` entry under every `request_policy` rule.
4. Include each non-empty `api_key_file` value.
5. Deduplicate repeated exact configured strings.

Disabled providers and `api_key_env` values are not synchronized. Discovery is implemented once in a host helper shared by synchronization, validation, and tests; Compose generation must not use it to create mounts.

`generate_compose()` already implements the required provider and policy traversal at `bin/agentbox:261-282`. Extract that code into the shared helper instead of implementing another traversal. The resulting host reference contains configured source, resolved host path, target name, and provider name.

The proxy remains authoritative about rule validity. A source referenced only by a request rule that is rejected during compilation does not create a runtime unavailable policy.

### 3.2 Target Names

The target name is:

```text
<first 12 lowercase hex characters of SHA-256(configured api_key_file)>-<basename>
```

The resulting path is `/run/secrets/<target-name>`. The hash input is the exact YAML scalar before path expansion.

The basename must match `[A-Za-z0-9._-]+`, be neither `.` nor `..`, and contain no slash or traversal component. The complete generated target must fit the target filesystem's `NAME_MAX`; for Linux tmpfs with a 255-byte limit, the 13-byte hash prefix leaves 242 bytes for the ASCII basename. No additional arbitrary margin is imposed. A source that cannot produce a valid target is rejected before tmpfs mutation. Distinct configured strings mapping to the same generated target are a fatal collision. Host and proxy implementations use identical contract-test vectors.

This generation formula is already implemented by `injection_secret_name()` in `bin/agentbox:199-202` and `_secret_path()` in `proxy/addons/resolvers.py:16-21`. Preserve their valid output and add shared validation and collision checks around the existing contract.

### 3.3 Host Paths

On every synchronization, the host expands `~` and resolves the current source pathname. Absolute and home-relative paths have their normal meanings; relative paths resolve against `x-metadata.project-dir`, never the command's working directory.

The configured string determines the target name. The resolved host pathname is neither written to Compose nor sent to the proxy. Reopening the current pathname on each command handles atomic replacement and symlink retargeting without relying on a bind-mounted inode.

The current host code already calls `expanduser().resolve()` at `bin/agentbox:273-276`, but resolves relative paths against the invoking process. Reuse the existing project metadata to supply the required base instead.

---

## 4. Synchronization Protocol

### 4.1 One-Shot Transaction

With the synchronization lock held as defined in Section 6, each invocation performs a complete transaction, including credentials whose contents may be unchanged:

1. Parse one host `proxy.yaml` snapshot.
2. Compute full-configuration and provider fingerprints.
3. Discover and validate the desired references, detect target collisions, and attempt every host source read.
4. Start, reuse, or restart the proxy as required by the invoking command, then invoke one `compose exec -T` for the transaction.
5. Inside the exec, wait for the loopback reload interface before changing the store.
6. Clean private temporary files and reset the managed target set.
7. Install every transferable source.
8. Call `/reload/providers` exactly once with the expected full-configuration fingerprint.
9. Verify the applied fingerprint and report unavailable policies.

Steps 1-3 are host preflight and occur before a command starts or restarts a proxy. Invalid YAML, invalid target names, and target collisions therefore cannot destroy a working proxy; structural checks performed only by the proxy can still fail after `proxy-restart`, as defined in Section 7.

There is no delta detection or persistent synchronization state. An interrupted transaction is recovered by rerunning any synchronization command, which resets and transfers the complete set.

No watcher, polling thread, generation tracker, or persistent secret-sync state currently exists; this absence is intentional and must be preserved.

### 4.2 Host Reads

Sources are expected to be regular credential files. A source is transferable when it can be opened and read as UTF-8 text and is non-empty after resolver whitespace semantics are applied.

Each source is opened and read once with `Path.read_bytes()` before the first tmpfs mutation. The bytes read are retained in a short-lived host buffer, so replacement of the source pathname during the read still produces one complete payload.

A source that is missing, unreadable, invalid UTF-8, or empty is not transferable. Section 5.3 defines its runtime availability semantics.

### 4.3 Secret Manager

A fixed Python helper in the proxy provides inventory, managed-target reset, private temporary-file cleanup, installation, and loopback reload. One `compose exec -T proxy python3 /app/manage_secrets.py sync` carries the complete transaction through stdin: `v1 <full-fingerprint> <transfer-count>\n`, followed by each validated target name and length/digest/payload frame. No credential contents or digests travel in command arguments.

An installation frame contains payload length, digest, and payload. The helper:

1. Validates the target name and frame header.
2. Creates a unique temporary file under `/run/secrets` with `umask 077`.
3. Reads exactly the declared payload while computing its digest.
4. Rejects premature EOF, trailing bytes, and digest mismatch.
5. Sets mode `0400` and atomically renames the file to its final target.
6. Removes its temporary file on every failure.

Managed final targets match `^[0-9a-f]{12}-[A-Za-z0-9._-]+$`. After readiness and before installation, the helper removes every managed final target and every file bearing its private temporary prefix. An unrelated entry is a fatal error and is not removed. Once all frames and end-of-stream are verified, the helper calls the existing `/reload/providers` endpoint once and returns its sanitized status/body to the host for fingerprint verification.

Existing resolvers keep credentials in memory, so resetting files does not alter active requests.

### 4.4 Fingerprints

The host computes SHA-256 over canonical JSON encodings of:

- The complete parsed configuration, used to bind `/reload/providers` to the exact staged snapshot.
- The parsed `providers` value, used to prevent fast reload from applying provider changes without synchronization.

The proxy recomputes fingerprints from the single configuration snapshot used for its operation. A mismatch leaves runtime state unchanged and is reported as a concurrent configuration change that requires the user to rerun the command. Files already staged in tmpfs remain inactive until the next complete transaction resets them.

The current reload handler only compares parsed provider lists at `proxy/addons/addon.py:175-189`; no canonical fingerprints or expected-fingerprint validation exist yet.

---

## 5. Proxy Reload and Request Behavior

### 5.1 Reload Operations

The loopback reload server exposes:

| Endpoint | Caller | Behavior |
|----------|--------|----------|
| `/reload` | `agentbox allow` / `deny` | Apply non-provider changes only when the snapshot provider fingerprint equals the active fingerprint. Never rebuild providers. |
| `/reload/providers` | Synchronization commands | Validate the expected full fingerprint and always rebuild enabled providers and resolvers. |

Both operations share one addon-level asynchronous lock. The server remains bound to `127.0.0.1` inside the proxy and is not exposed through the agent-facing address.

Fast `/reload` compares the provider fingerprint before rebuilding rules or changing runtime state. On mismatch it returns a conflict without performing a partial reload.

`/reload/providers` reads `proxy.yaml` once, constructs providers in an executor, validates resolver types and provider structure, and builds providers, rules, logging settings, and credential availability as one candidate. It atomically assigns the candidate only after structural validation succeeds, then returns the applied fingerprint and sanitized unavailable-policy summaries.

The loopback server and executor-based provider construction already exist at `proxy/addons/addon.py:167-198`. Extend that server with routing, locking, fingerprints, and atomic candidate construction; do not create a second reload service.

Candidate construction must not mutate any live runtime field. Structural failure retains the complete previous runtime state.

### 5.2 Flow Continuity

Provider reconstruction is an in-process object swap. It must not restart mitmproxy, replace the addon, close connections or tunnels, or cancel flows.

Credential injection finishes in the request-header hook before forwarding. An active flow no longer depends on its resolver: existing requests retain their injected credential, including streaming requests, while requests entering injection after the swap use the new resolver set. Static resolvers continue serving credentials from memory without request-path file I/O; rebuilt Cursor resolvers receive the new API key and start with an empty exchanged-token cache.

The current request-header injection, `_Config` assignment, and streaming hooks already provide this lifecycle at `proxy/addons/addon.py:187-189,213-261`. Preserve those hooks rather than adding flow ownership or connection management.

### 5.3 Per-Policy Availability

Each valid injection policy independently records whether its resolver has a usable credential. A file policy may use its synchronized file or a non-empty configured environment fallback; the host never reads fallback values.

Resolvers use namespaced lookup, environment fallback, and Static/Cursor caching. Policy construction, invalid-rule skipping, original-header matching, and injection ordering already exist at `proxy/addons/provider.py:49-214`. Extend those implementations with availability status and resolve-then-commit; do not add request-path file reads or a parallel policy engine.

For each allowed request, the proxy evaluates policy applicability against the original headers and resolves all applicable credentials before changing any header. A `replace_token` mismatch makes that policy non-applicable. If any applicable configured policy is unavailable, the proxy returns HTTP 503 without contacting upstream or partially injecting headers.

Unavailable policies do not block healthy providers or ordinary `extra_request_policy` rules. Allowlist denials remain HTTP 403. A missing, unreadable, invalid UTF-8, or empty source has no installed target; its policy uses a non-empty configured environment fallback when available and is otherwise marked unavailable. This produces a sanitized warning rather than structural reload failure.

---

## 6. Command and Lifecycle Behavior

| Operation | Required behavior |
|-----------|-------------------|
| `start` | After host preflight, start or reuse the proxy, then enter one exec that waits for the reload endpoint. Report unavailable-policy warnings, release the lock after verification, then launch the agent. Do not launch on transaction failure. |
| `proxy-reload` | Require a running proxy, perform one complete transaction, and rebuild providers even when YAML is unchanged. |
| `proxy-restart` | Complete host preflight before restarting the proxy, then hold the lock through endpoint readiness, tmpfs repopulation, reload, and verification. |
| `allow` / `deny` | Save the allowlist edit, then use fast `/reload`; do not synchronize or rebuild providers. If provider changes are pending, report that the edit is saved but not active and instruct the user to run `proxy-reload`, which applies both changes. The user does not rerun `allow` or `deny`. |
| Automatic proxy restart | File policies without usable environment fallbacks are unavailable until the next synchronization command; only matching requests receive HTTP 503. |
| Raw `podman compose up` | Does not synchronize. File-backed policies without usable environment fallbacks are unavailable until an `agentbox` synchronization command runs. |

Every `start` invocation synchronizes, including when agents share an existing proxy. The final process leaving the proxy lifetime context must reacquire the synchronization lock before `compose down` so teardown cannot race with reload or restart.

The exclusive lock is:

```text
<session-dir>/.secret-sync.lock
```

It is held from before any CLI-managed proxy lifecycle action through configuration snapshot, target reset, transfer, and reload. It contains no secret data.

`compose()`, `_proxy_lifetime()`, `_launch()`, and the relevant CLI commands already provide the lifecycle structure at `bin/agentbox:122-171,417-423,564-659`. Add one-shot synchronization only to `_launch()`, `proxy-reload`, and `proxy-restart`; keep `allow` and `deny` on fast reload. Add the synchronization lock to those operations, final lifetime teardown, `stop`, and `remove` rather than creating a second lifecycle manager.

---

## 7. Failure Semantics

| Failure | Required behavior |
|---------|-------------------|
| Invalid YAML | Do not mutate tmpfs or runtime state; report the error and require the user to rerun the command. |
| Managed-target reset failure | Abort before installing any target or invoking reload. |
| Malformed, incomplete, mismatched, or unwritable transfer | Remove its temporary file, skip reload, and retain the old active resolver set when one exists. |
| Process exit during synchronization | Leave any old in-memory resolver set active; the next command resets partial staging and retries the complete set. |
| `proxy-restart` transfer or structural failure | Exit nonzero; the old process is gone, so providers have the availability established by the new process's last successful configuration load. |

---

## 8. Security

Synchronized credential bytes may exist only in the configured host file, short-lived host buffers and stdin pipe, proxy tmpfs, resolver memory, and host swap used for those memory pages. Tmpfs avoids container-layer persistence but does not protect secrets from the trusted host or proxy container.

Credential contents and content digests must not appear in Compose, synchronization locks, persistent metadata, process arguments, environment variables, logs, or command output. Separately configured environment credentials retain their existing behavior. Configuration fingerprints may be transmitted but are not logged as credential identifiers.

Logs may include provider names, configured source paths, generated target names, and sanitized errors.

---

## 9. Configuration and Dependencies

Compose provides the secret tmpfs. Synchronization uses it directly, and resolvers read only the generated namespaced targets.

`${AGENTBOX_HOME}/secrets` is no longer automatically visible. It is usable as a provider source only when explicitly referenced by `api_key_file`; non-provider consumers require `proxy_volumes` outside `/run/secrets`.

Environment-only providers are not synchronized, and changing their container environment still requires recreation. The provider YAML schema does not change: `api_key_file` becomes a synchronization source instead of a mount declaration.

No new host runtime package is introduced. The implementation uses the Python standard library, existing YAML parser, and existing Podman and Compose commands. The proxy secret manager uses only the Python standard library. No agent image or runtime behavior changes.

Update `README.md`, `docs/SPEC.md`, `docs/ARCHITECTURE.md`, credential comments in `compose-base.yaml`, and affected presets such as `presets/default/proxy.yaml`. Remove broad-mount guidance, document the direct-Compose limitation prominently, and move non-provider OAuth examples outside `/run/secrets`.

---

## 10. Verification

Automated coverage must verify:

Target-contract coverage in `tests/test_secret_paths.py`, policy scope and ordering coverage in `tests/test_provider.py`, and basic reload coverage in `tests/test_addon.py` must be preserved rather than duplicated.

- Discovery across top-level, provider-level, and request-rule policies, including exclusions, deduplication, invalid rules, target vectors, collisions, and deterministic path resolution.
- Compose output has no provider secret mounts or host secret paths.
- `${AGENTBOX_HOME}/secrets` receives no mountpoint side effects, and synchronization creates no persistent state file.
- Stable reads across atomic replacement and symlink retargeting; malformed transfer rejection; target reset; and recovery from partial staging.
- Exact fingerprint matching, fast/full reload separation, structural rollback, and serialization of overlapping start/reload/restart/teardown operations.
- Static and Cursor rotation remains unapplied before an explicit command and applies afterward.
- A streaming request survives provider reload while a later request uses the new credential.
- One unavailable policy returns HTTP 503 only for requests requiring it; healthy providers continue, invalid skipped rules do not gate, and multi-policy injection never partially mutates headers.
- Start synchronizes before agent launch, restart repopulates tmpfs, and automatic or raw-Compose startup leaves only unsynchronized file policies unavailable.
- Credential values never appear in Compose, arguments, metadata, logs, or command output.
- One exec per synchronization handles all transferable sources, including zero sources, and calls `/reload/providers` exactly once after successful installation.
- Environment-only providers and `proxy_volumes` work with the generated Compose configuration.
