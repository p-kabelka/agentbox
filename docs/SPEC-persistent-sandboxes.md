# Persistent Sandboxes — Proposed Feature Specification and Implementation Plan

## 1. Goal and scope

Add an opt-in `agentbox init --persist` mode that preserves an agent container's writable filesystem across successive `agentbox start` calls. On agent exit, the agent and its proxy sidecar are **stopped, not removed**. A later `start` starts the same agent container if it exists, or creates it if this is the first start. There is exactly **one agent container and one proxy container per persistent session**; parallel agents remain possible only with separate sessions (or the existing ephemeral mode).

This is a proposed feature. The current implementation uses `compose run --rm --no-deps agent` and tears down the Compose project after the last concurrent agent exits (`bin/agentbox:_launch`, `_proxy_lifetime`). Without `--persist`, retain that behavior, including concurrent starts and final `compose down`.

Persistence applies to the **container**, not its processes: the writable layer (including `/workspace` for git-backed projects and the agent's home directory) survives stop/start, but running shells, tmux servers, and the agent process do not. Existing git-bundle/output-repo isolation, proxy-only credentials, and internal networking remain in force. In `--no-git` mode, `/workspace` is still the existing host bind mount.

## 2. User contract

```bash
agentbox init --name feature --persist       # opt in; no container is created yet
agentbox start --name feature -- tmux -u new-session -s agent
# work, exit the agent process; the proxy stops and the agent remains as a stopped container
agentbox start --name feature                 # restart that container, same command and filesystem
agentbox stop --name feature                  # stop both, retaining the agent and proxy containers
agentbox remove --name feature                # delete containers, volumes, session state, and git remote
```

`init --persist --start` is supported. `start -- CMD` selects the entrypoint's command **only on first creation**: save `CMD` as a YAML list in the persistent agent's Compose `command` before creating its service container. When the persistent agent already exists, `start -- CMD` fails before changing the proxy, even if `CMD` equals the original command: Compose restart uses the container's saved command. Without `CMD`, leave the service command unset so the preset's harness/default applies. `start` without `CMD` restarts the service and attaches an interactive terminal. Opening another shell while it runs is not another `start`: a second start fails with a clear *session already running* error. `stop` and `remove` use the same `--name`/`--session` resolution as today.

Auto-fetch from `output.git` still runs after each attached agent exit (success or failure), with hooks disabled as in `_launch`. The agent still has to push to the output repo to publish commits; unpushed commits and uncommitted files remain only in the container until pushed. `remove` destroys that unpublished work. A persisted agent does **not** re-clone a new bundle on restart; its existing `/workspace/.git` and `origin` remain intact.

## 3. Session identity and lifecycle

- `init --persist` writes `x-metadata.persist: true` in the generated `compose.yaml` alongside `project-dir` and `name`. Missing or `false` means ephemeral, including sessions created before this feature. Reject malformed values rather than guessing. The flag is session-level; `start` has no separate `--persist` flag.
- Use **Compose service containers** for both members of a persistent session. At `init --persist`, assign deterministic `container_name` values to `services.agent` and `services.proxy` (e.g. `agentbox-<first-20-hex-of-sha256(session-id)>-{agent,proxy}`) and add session-identity labels. Fixed names prevent Compose scaling the services. Inspect the exact service names/labels and validate ownership; refuse foreign, duplicate, or unexpected containers rather than creating a second pair.
- After proxy synchronization, first `start` uses `compose create --no-deps agent` to create the **service** agent without starting it, then `compose start agent`. Later starts use `compose start agent` to resume that same container without recreating it. Neither path uses `compose run` or `--rm`. `--no-deps` on creation prevents an implicit proxy lifecycle action after secret synchronization. Verify that `compose start agent` leaves the already-running proxy unchanged. Use `podman attach <agent-container>` only to connect the terminal to the running service; it does not create, start, stop, or remove a container. If the agent exits before attach, inspect its exit status and proceed to cleanup.
- Start/reuse the proxy with the existing `compose up -d proxy` path, retaining one Compose proxy service container for the session. On agent exit and on `agentbox stop`, use **one** `podman compose stop` invocation with no service arguments (via the existing `compose()` wrapper) and verify both containers are stopped. The wrapper supplies the session's Compose file and project name, so the command stops the session's services without affecting other sessions. Report a nonzero result if either remains running; do not create another agent on the next `start` until the state is resolved. Stopping is idempotent if a service has already exited. Never run `compose down` on ordinary persistent exit/stop.
- `agentbox remove`, after ensuring no active `start` owns the persistent session, uses `compose down -v` to remove **both service containers**, networks, and volumes before session-directory/git-remote cleanup. A running or attaching agent causes `remove` to fail with instructions to `stop` and wait for `start` to return; never delete its filesystem underneath an attached CLI. No separate `podman rm` is required for a Compose service container.

| Persistent operation | Container management via `podman compose` |
|---|---|
| First `start` | `up -d proxy`; synchronize; `create --no-deps agent`; `start agent` |
| Later `start` | `up -d proxy`; synchronize; `start agent` |
| Agent exit / `agentbox stop` | `stop` (one invocation; all session services) |
| `agentbox remove` | `down -v` |

Terminal attachment (`podman attach`) and inspection are separate from container lifecycle management.

| Persistent agent state before `start` | Required action |
|---|---|
| No container | Synchronize proxy; create exactly one named agent and attach. |
| Created/stopped/exited container | Synchronize proxy; start and attach that same container. |
| Running container or another `start` in progress | Fail before proxy changes; use `stop` to recover a running, unattached container. |
| Unexpected/multiple/mismatched containers | Fail closed with an actionable diagnostic; do not create or adopt another. |

`list`, `status`, and `containers` should distinguish a retained stopped agent from a running agent. In particular, a stopped persistent pair must not be reported as running just because its containers still exist; a proxy-only running state should not be reported as an active agent. Preserve existing ephemeral output semantics; adding a `persistent` boolean to `list --json` is acceptable if documented.

## 4. Startup, serialization, and failures

Persistent `start` follows the existing `start` secret transaction, with the agent ownership check added **before** proxy changes:

1. Acquire `.secret-sync.lock`; acquire an **exclusive, nonblocking** ownership lock for this persistent agent (reuse `.lifetime.lock`, which ephemeral starts currently hold shared). Inspect the named container and project labels; reject an active, foreign, or duplicate agent and disallow `-- CMD` on reuse. Hold ownership until the attached process, cleanup, and auto-fetch complete. Do not wait for the exclusive lock while holding `.secret-sync.lock`.
2. Run `_secret_preflight` against the current session `proxy-config/proxy.yaml`. Start/reuse the proxy, then call the current `_synchronize_proxy` transaction: one `compose exec -T proxy` stages credentials, waits for reload readiness, performs the provider reload, and verifies the applied fingerprints. Keep `.secret-sync.lock` across these steps, as today. **Do not launch or restart the agent until synchronization succeeds.** On first start, write any `-- CMD` override to `services.agent.command` (as a list) before creating that service container, and include it in the immutable-configuration check.
3. Still holding `.secret-sync.lock`, use `compose create --no-deps agent` only if the service container does not exist, then `compose start agent`. Wait until the service container is **running** or has already exited; a merely *created* container is not sufficient, because `stop` must not miss a pending start. Use a bounded readiness wait and clean up a stalled startup under the lock. Then release the sync lock and attach the terminal with `podman attach` outside it. This closes the gap in which `stop` could stop the proxy just before `start` starts the agent. Read the agent's actual exit status from the stopped container if attach does not reliably forward it.
4. On exit or an interrupted attach, reacquire `.secret-sync.lock` for cleanup. Run `compose stop` once, then verify both are stopped; do not remove either. Report a cleanup failure if either remains running. Auto-fetch published output before releasing ownership, even if cleanup reports an error. If the process is forcibly killed, its OS lock releases: a subsequent `start` must inspect and refuse a still-running agent; `stop` can recover it.

`stop`, `remove`, `proxy-reload`, and `proxy-restart` retain `.secret-sync.lock` for proxy lifecycle operations. `stop` is allowed to stop an active agent while its `start` CLI owns `.lifetime.lock`: do not wait for that lock while holding `.secret-sync.lock`. The attached CLI then exits and performs idempotent cleanup. `remove` checks for active ownership without waiting under `.secret-sync.lock`. Existing-session `init` and `mount add`/`mount remove` also take the sync lock while checking for a retained agent and editing Compose, so they cannot race with its creation. This lock ordering prevents overlapping `start` calls from synchronizing or creating a second agent, and prevents teardown racing with secret reload/restart.

Host preflight or fingerprint errors leave an existing agent untouched and never launch it. A failed proxy sync leaves the agent stopped; clean up a proxy started solely for this failed attempt where possible. If agent creation/restart or cleanup fails, retain the existing container and report a nonzero result; the next `stop` or `start` can inspect/recover it. Fetch published output after an agent exit even if proxy cleanup reports an error. Re-running `start` always performs a complete new secret transaction, including when it restarts a stopped agent. Raw Compose or automatic proxy restarts still do **not** synchronize file-backed credentials.

## 5. Configuration and restart semantics

The retained container's image, runtime, mounts, networks, environment, and command are fixed when it is created. They cannot be retroactively changed by editing `compose.yaml` or rebuilding an image. Give a clear error on `start` if immutable agent configuration differs from what created the retained container: record a non-secret configuration fingerprint and session identity as Compose service labels at creation, and compare to a canonical fingerprint of `services.agent` (excluding its fingerprint label) and referenced network/volume declarations before proxy startup. Validate that the Compose provider supports service `create --no-deps`, `start`/`stop` without recreation, fixed container names, and interactive `podman attach` under `krun`. Proxy provider YAML edits still take effect through `proxy-reload` or the next `start`; environment/mount changes requiring proxy recreation retain their existing limitations.

- If `init` targets an existing session, reject changing its persistence mode **before** modifying the session, bundle, or output repo. Existing ephemeral sessions remain ephemeral; create a new named persistent session instead of implicitly converting one.
- Once a persistent agent container exists, `init` must not regenerate its bundle or agent Compose definition. Reject re-initialization of that session before side effects, with guidance to use the existing container or create a new session. Before first start, repeat `init --persist` may retain the current re-init behavior.
- `mount add`/`mount remove` may edit a persistent session before its first agent creation. With a retained agent, reject agent-mount edits rather than claiming they apply on `start`. Direct changes to immutable agent Compose settings cause the fingerprint check above to fail. Create a new session for changed agent configuration; use `remove` only after publishing work that should be kept.
- Make `agent/start.sh` restart-safe: preserve the existing guard that skips cloning when `/workspace/.git` exists, avoid appending the same proxy CA to the trust bundle on every boot, and seed preset dotfiles only on first creation (or when absent), so restarting does not overwrite edits in the agent's home directory. No extra host mounts, agent credentials, or second proxy are needed.

Persisting the writable layer also persists any files the agent placed there. The existing security boundary must hold on every restart: no host `.git` mount or real credentials inside the agent, agent attached only to the internal network, protected output-repo hooks/config mounts retained, and proxy credentials still limited to proxy memory/tmpfs and explicitly configured proxy-side sources. `stop` does not erase agent files; `remove` does.

## 6. Implementation plan

1. **Mode and identity:** Add `--persist` to `init`, serialize/read `x-metadata.persist`, guard existing-session re-initialization and mode changes, and define fixed Compose service names plus ownership/configuration labels in `bin/agentbox`/`generate_compose`.
2. **Lifecycle:** Split `_launch` into the current ephemeral path and a persistent path sharing the current `_secret_preflight`/`_synchronize_proxy` transaction. Add a nonblocking exclusive ownership lifetime, service-container inspection, first `compose create --no-deps agent`, creation/reuse with `compose start agent`, terminal `podman attach`, and exit/interrupt cleanup with `compose stop`. Preserve exit status and hook-disabled auto-fetch.
3. **Management:** Branch `cmd_stop` and `cmd_remove` by session mode; stop all session Compose services with one `compose stop` and remove both containers with `compose down -v`. Reject agent-mount edits and re-init of a populated persistent session; update session-listing status for retained containers. Make `agent/start.sh` idempotent for CA/dotfiles.
4. **Docs:** Once implemented, update `README.md` quickstart/lifecycle, `docs/SPEC.md` command reference and `start`/`stop`/`remove` contracts, `docs/ARCHITECTURE.md` orchestration description, and the mount instructions. Keep the default-ephemeral concurrency contract explicit.
5. **Verification:** Add focused CLI lifecycle tests alongside `tests/test_manual_secrets.py` with mocked Compose/Podman calls and real file locks; then exercise the real Compose provider, Podman, and `krun` in an integration environment. Podman and podman-compose are not installed in this workspace, so their CLI interaction must be validated there rather than assumed.

## 7. Acceptance tests

- `init --persist`, `init --persist --start`, old sessions lacking the flag, conflicting re-init modes, and re-init of a populated persistent session.
- First `start` creates one Compose `agent` service container and one Compose `proxy` service container; two consecutive starts report the **same container ID**, retain edits under `/workspace` and `~`, and perform a fresh provider sync before each attach. The second start reuses the original command. `start -- CMD` on reuse and simultaneous `start` fail before creating containers or touching the proxy.
- Exit (including nonzero exit), Ctrl-C/failed attach, explicit `stop` while attached, and recovery after a killed CLI leave no unintended running agent; `agentbox stop` issues **one** Compose `stop` without service arguments and both containers remain stopped. A subsequent `start` resumes once. `remove` uses Compose `down -v` to delete both containers, session resources, and remote.
- Synchronization errors, duplicate/foreign container names, stale running agents without an owner, changed immutable Compose settings, `mount add/remove` after creation, and cleanup failures fail predictably without a second agent. Reload/restart and stop operations serialize with start/teardown.
- Auto-fetch after each agent exit remains hook-disabled; first-boot clone, output remote, CA installation, and dotfile seeding do not overwrite the resumed workspace/home. No-git mode and persistent source/output mounts retain their original isolation properties.
- Real Podman/Compose compatibility: deterministic `container_name` values, service labels, `compose create --no-deps agent`, `compose start agent`, one Compose `stop` stopping both services, `compose down -v` removing both, interactive `podman attach` and process exit codes, and retained writable layer under `krun`.
