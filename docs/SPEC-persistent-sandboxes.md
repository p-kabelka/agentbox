# Persistent Sandboxes — Lifecycle and Verification

## 1. Goal and scope

`agentbox init --persist` preserves an agent container's writable filesystem across successive starts. On agent exit, the agent, proxy, and Git sidecar are **stopped, not removed**. A later start reuses the same agent. There is exactly one of each service per persistent session; parallel agents require separate sessions or ephemeral mode.

Without `--persist`, the implementation uses `compose run --rm --no-deps agent` and tears down the Compose project after the last concurrent agent exits (`bin/agentbox:_launch`, `_proxy_lifetime`).

Persistence applies to the **container**, not its processes: Git-backed `/workspace`, `/context/git`, and home survive stop/start, but running shells, tmux servers, and the agent process do not. Git data is isolated in the internal-only sidecar's named volume. Explicit `--no-git` workspaces use a writable host bind mount as before. See [SPEC-git-sidecar.md](SPEC-git-sidecar.md).

## 2. User contract

```bash
agentbox init --name feature --persist       # opt in; Git sharing starts the Git sidecar
agentbox start --name feature -- tmux -u new-session -s agent
# work, exit the agent process; the proxy stops and the agent remains as a stopped container
agentbox start --name feature                 # restart that container, same command and filesystem
agentbox stop --name feature                  # stop all services, retaining the containers
agentbox remove --name feature                # delete containers, volumes, session state, and git remote
```

`init --persist --start` is supported. `start -- CMD` selects the entrypoint's command **only on first creation**: save `CMD` as a YAML list in the persistent agent's Compose `command` before creating its service container. When the persistent agent already exists, `start -- CMD` fails before changing the proxy, even if `CMD` equals the original command: Compose restart uses the container's saved command. Without `CMD`, leave the service command unset so the preset's harness/default applies. `start` without `CMD` restarts the service and attaches an interactive terminal. Opening another shell while it runs is not another `start`: a second start fails with a clear *session already running* error. `stop` and `remove` use the same `--name`/`--session` resolution as today.

Host fetching is manual. `git fetch agentbox-SESSION_ID` can restart only the Git sidecar after shutdown, using its retained named volume. Unpushed/uncommitted work stays only in the persistent agent container; pushed but unfetched work stays in the sidecar. Ordinary exit/stop retains both; removal destroys both. Existing clones and `origin` remain intact on restart. New shares clone at the next start, or explicitly with `agentbox-clone NAME` inside a running sandbox; there is no watcher.

## 3. Session identity and lifecycle

- `init --persist` writes `x-metadata.persist: true` in the generated `compose.yaml` alongside `project-dir` and `name`. Missing or `false` means ephemeral, including sessions created before this feature. Reject malformed values rather than guessing. The flag is session-level; `start` has no separate `--persist` flag.
- Use **Compose service containers** for the agent, proxy, Git sidecar, network namespace holder, and firewall guard. All sessions use Compose project names containing a hash of the session ID, so projects with the same directory basename and session name remain separate. At `init --persist`, assign deterministic `container_name` values to `services.agent` and `services.proxy` (e.g. `agentbox-<first-20-hex-of-sha256(session-id)>-{agent,proxy}`). Git, the holder, and the guard always have fixed names. Compose supplies project labels; there are no extra session labels. Fixed names prevent Compose scaling the services. Find containers by Compose project label and identify services by their fixed names; Compose reports any fixed-name conflict during creation.
- After proxy synchronization, first `start` uses `compose up --no-start --no-deps --no-recreate agent` to create the **service** agent without starting it, then `podman start -ai <agent-container>` to start and attach. `podman-compose` 1.5.0 has no `create` subcommand; `up --no-start` provides the required creation step. Later starts use `podman start -ai` on that same container. Neither path uses `compose run` or `--rm`. `--no-deps` prevents an implicit proxy lifecycle action after secret synchronization, and `--no-recreate` prevents Compose from tearing down the running proxy after a first-start command override changes the YAML. The container retains its Compose project/service labels for `compose stop` and `down`.
- Start/reuse both sidecars with `compose up -d --no-recreate proxy git`. Without `--no-recreate`, Podman Compose can tear down the whole project after a user YAML edit. Git sharing updates a separate session registry, never Compose. On exit/stop use one `compose stop` with no service arguments and verify all containers are stopped. Never run `compose down` on ordinary persistent exit/stop.
- `agentbox remove` uses `compose down -v` to remove all service containers, networks, and volumes before session-directory/registered-remote cleanup, even while attached. The attached CLI skips cleanup after removal. Fetch wanted commits on the host first.

| Persistent operation | Container management |
|---|---|
| First `start` | Start both sidecars without recreation; synchronize; create agent without starting/dependencies/recreation; `podman start -ai <agent>` |
| Later `start` | Start both sidecars without recreation; synchronize; `podman start -ai <agent>` |
| Agent exit / `agentbox stop` | `stop` (one invocation; all session services) |
| `agentbox remove` | `down -v` |

`podman start -ai` starts the agent and attaches the terminal in one blocking call. Container inspection is separate from lifecycle management.

| Persistent agent state before `start` | Required action |
|---|---|
| No container | Synchronize proxy; create exactly one named agent and attach. |
| Created/stopped/exited container | Synchronize proxy; start and attach that same container. |
| Running container or another `start` in progress | Fail before proxy changes; use `stop` to recover a running, unattached container. |

`list`, `status`, and `containers` should distinguish a retained stopped agent from a running agent. In particular, a stopped persistent pair must not be reported as running just because its containers still exist; a proxy-only running state should not be reported as an active agent. `containers` reports state for both session modes; `list --json` includes a documented `persistent` boolean.

## 4. Startup, serialization, and failures

Persistent `start` follows the existing `start` secret transaction, with the agent ownership check added **before** proxy changes:

1. Acquire `.secret-sync.lock`; acquire an **exclusive, nonblocking** ownership lock for this persistent agent (reuse `.lifetime.lock`, which ephemeral starts currently hold shared). Find the agent by Compose project label and fixed name; reject a running agent and disallow `-- CMD` on reuse. Hold ownership until the attached process and cleanup complete. Do not wait for the exclusive lock while holding `.secret-sync.lock`.
2. Run `_secret_preflight` against the current session `proxy-config/proxy.yaml`. Start/reuse the proxy, then call the current `_synchronize_proxy` transaction: one `compose exec -T proxy` stages credentials, waits for reload readiness, performs the provider reload, and verifies the applied fingerprints. Keep `.secret-sync.lock` across these steps, as today. **Do not launch or restart the agent until synchronization succeeds.** On first start, write any `-- CMD` override to `services.agent.command` (as a list) before creating that service container.
3. Still holding `.secret-sync.lock`, use `compose up --no-start --no-deps --no-recreate agent` only if the service container does not exist. Release the sync lock before `podman start -ai`, which blocks until the agent exits. A concurrent `stop` can stop the proxy between synchronization and agent startup, leaving the agent without proxy access; this race is accepted. Inspect the agent's exit status after `podman start -ai` if its return code does not reliably forward it.
4. On exit/interruption, reacquire `.secret-sync.lock` if the session still exists. Run `compose stop` once and verify all services stopped. Report cleanup failures without discarding data. If `remove` deleted the session, skip cleanup. A forcibly killed CLI releases its lock; the next start refuses any still-running agent until recovered with `stop`.

`stop`, `remove`, `proxy reload`, and `proxy restart` retain `.secret-sync.lock` for proxy lifecycle operations. `proxy provider` commands use the same lock to read or edit saved provider settings. `stop` can stop an active agent while its `start` CLI owns `.lifetime.lock`; the attached CLI then exits and performs idempotent cleanup. `remove` also stops an active agent and deletes its session while the CLI is attached. Existing-session `init` takes the sync lock while checking for a retained agent and editing Compose, so it cannot race with creation. This lock ordering prevents overlapping `start` calls from synchronizing or creating a second agent, and serializes teardown with secret reload/restart except for the acknowledged stop/start gap after synchronization.

Git sharing and host Git transfers never acquire the sync lock; `init` releases
it before the workspace push. Proxy reload and stop/remove do not wait for large
Git transfers. Teardown may interrupt a push, fetch, or share, leaving partial
sharing setup that must be retried or cleaned up.

Host preflight or provider-fingerprint errors leave an existing agent untouched and never launch it. A failed proxy sync leaves the agent stopped; clean up sidecars started solely for this failed attempt where possible. If agent creation/restart or cleanup fails, retain the existing container and report a nonzero result; the next `stop` or `start` can inspect/recover it. Published output remains available for manual fetching even if cleanup reports an error. Re-running `start` always performs a complete new secret transaction, including when it restarts a stopped agent. Raw Compose or automatic proxy restarts still do **not** synchronize file-backed credentials.

## 5. Configuration and restart semantics

The retained container's image, runtime, mounts, networks, environment, and command are fixed when it is created. Editing `services.agent` in `compose.yaml` or rebuilding an image does not update that container on the next `start`; direct Compose edits may also disrupt service management. To apply agent configuration changes, create a new session. Validate that the Compose provider supports service `up --no-start --no-deps --no-recreate`, `stop` without recreation, fixed container names, and interactive `podman start -ai` under `krun`. Proxy provider YAML edits still take effect through `proxy reload` or the next `start`; environment/mount changes requiring proxy recreation retain their existing limitations.

- Reject changing an existing session's persistence mode before modifying configuration or shared repositories.
- Once a persistent agent exists, reject re-initialization before side effects. New Git references use `agentbox mount`, not re-initialization.
- `--ro-mount`/`--rw-mount` on `init` set agent mounts before first creation. Direct changes to agent Compose settings do not reconfigure the retained container. Create a new session for changed agent configuration; use `remove` only after publishing work that should be kept.
- Make `agent/start.sh` restart-safe: preserve the existing guard that skips cloning when `/workspace/.git` exists, avoid appending the same proxy CA to the trust bundle on every boot, and copy preset dotfiles over image defaults on the first boot only, so restarting does not overwrite edits in the agent's home directory. No extra host mounts, agent credentials, or second proxy are needed.

Persisting the writable layer also persists any files the agent placed there. Every restart retains the security boundary: no implicit writable host mounts, no real credentials inside the agent, internal-only agent/Git networking, and hook-disabled Git transport. Read-only dotfiles/config mounts remain; `--no-git` is an explicit writable-mount exception. `stop` does not erase agent files; `remove` does.

## 6. Implementation outline

1. **Mode and identity:** Add `--persist` to `init`, serialize/read `x-metadata.persist`, guard existing-session re-initialization and mode changes, use session-hashed Compose project names for both modes, and define fixed persistent service container names in `bin/agentbox`/`generate_compose`.
2. **Lifecycle:** Split `_launch` into the current ephemeral path and a persistent path sharing the current `_secret_preflight`/`_synchronize_proxy` transaction. Add a nonblocking exclusive ownership lifetime, service-container inspection, first `compose up --no-start --no-deps --no-recreate agent`, start/attach with `podman start -ai`, and exit/interrupt cleanup with `compose stop`. Preserve exit status and the Git volume for manual fetching.
3. **Management:** Stop all persistent services with one `compose stop`; remove them with `compose down -v`. Reject re-init of populated persistent sessions. Make CA/dotfile seeding and one-shot Git cloning restart-safe; provide `agentbox-clone` for explicit cloning of later shares.
4. **Docs:** Describe the persistent lifecycle in `README.md`, `docs/SPEC.md`, and `docs/ARCHITECTURE.md`. Keep the default-ephemeral concurrency contract explicit.
5. **Verification:** Add focused CLI lifecycle tests alongside `tests/test_manual_secrets.py` with mocked Compose/Podman calls and real file locks; then exercise the real Compose provider, Podman, and `krun` in an integration environment. See the Git sidecar spec for integration prerequisites and the KVM-independent host transport check.

## 7. Acceptance tests

- `init --persist`, `init --persist --start`, old sessions lacking the flag, conflicting re-init modes, and re-init of a populated persistent session.
- First `start` creates one Compose `agent` service container and one Compose `proxy` service container; two consecutive starts report the **same container ID**, retain edits under `/workspace` and `~`, and perform a fresh provider sync before each attach. The second start reuses the original command. `start -- CMD` on reuse and simultaneous `start` fail before creating containers or touching the proxy.
- Exit (including nonzero exit), Ctrl-C/failed attach, explicit stop, and killed-CLI recovery leave no unintended running agent. One `compose stop` retains all services; `remove` uses `down -v` and removes matching host remotes. Attached CLIs do not fetch after session removal.
- Synchronization errors, fixed-name conflicts, stale running agents without an owner, and cleanup failures fail predictably without a second agent. Direct agent Compose edits do not update the retained container. Reload/restart and stop operations serialize with start/teardown.
- Exit never automatically fetches. Manual fetching after stop starts only Git and preserves its volume. Startup cloning, CA installation, and dotfile seeding never overwrite existing work. Live `mount` does not automatically clone anything; `agentbox-clone NAME` inside the sandbox obtains the share without recreation or Compose edits, including under krun. Explicit no-git workspaces retain their host-mount semantics.
- Real Podman/Compose compatibility: deterministic names, service labels, agent creation without start/dependencies/recreation, interactive attachment, one stop retaining all services, volume deletion on removal, exit codes, and retained writable layer under krun.
