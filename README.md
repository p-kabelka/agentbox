# agentbox

Isolated, credential-safe workspaces for AI coding agents. Each agentbox runs the agent in a microVM container with no real API keys, no direct internet access, and no write access to your original host git repository.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for design rationale and [SPEC.md](docs/SPEC.md) for the full technical specification.

---

## How it works

Each session has a **proxy** (holds real credentials, enforces an egress allowlist, logs all traffic) and one or more **agents** (hold no credentials, have no internet route). All agent network traffic transits mitmproxy. Your project is delivered to the agent as a read-only git bundle; output comes back through a bare repository with immutable hooks.

---

## Prerequisites

- Podman with `podman compose` (`podman-compose` 1.5.0 or newer; tmpfs syntax verified with 1.5.0)
- Python 3 with PyYAML (`pip install pyyaml`, `dnf install python3-pyyaml`)
- `krun` / `crun-vm` for VM-level isolation (recommended; agent container uses `runtime: krun`)

---

## Setup

```bash
git clone https://github.com/p-kabelka/agentbox.git ~/.local/share/agentbox

# add the project to your PATH
echo 'export PATH="$HOME/.local/share/agentbox/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
# or symlink the binary
ln -s -r ~/.local/share/agentbox/bin/agentbox ~/.local/bin

# Build container images (builds proxy, base agent, and all harness images)
agentbox build
# Or build specific images only:
agentbox build base proxy claude
```

### Zsh completion

The checked-in completion file covers all commands, nested subcommands, aliases,
and options. Session IDs (`--session` / `-s`) are read directly from session
directories across all projects, without invoking agentbox, Python, or Podman or
parsing Compose files. Session names (`--name` / `-n`) are scoped to the current
project. Presets, harnesses, providers, saved host rules, image tags, git
references, and paths are completed where applicable. New session/preset names
and other arbitrary values remain free-form. After `agentbox start --`, zsh
completes the command and its arguments using the host's installed completions
(the container may differ).

Add the completion directory to `fpath` **before** initializing completions in
`~/.zshrc`:

```zsh
fpath=("${AGENTBOX_HOME:-$HOME/.local/share/agentbox}/completions/zsh" $fpath)
autoload -Uz compinit
compinit
```

If a framework already runs `compinit`, put the `fpath` line before loading the
framework instead. Alternatively, copy `completions/zsh/_agentbox` into an
existing directory on your `fpath`. Restart zsh; if an existing completion cache
does not discover the new file, run `compinit -D` to rescan for the current shell.

If you symlink `bin/agentbox` under another name, such as `~/.local/bin/ax`,
register that name **after** `compinit` (or after loading your framework):

```zsh
compdef _agentbox ax
```

Keep the same `fpath` setup above. This enables all completions, including live
session IDs, for `ax` as well.

The script is packaged, not generated, so there is no `agentbox completions`
command. Live candidates are queried when you press Tab; nothing is built,
started, enabled, or reloaded. It uses Python 3 / PyYAML (already required by
agentbox), not `jq`. Keep agentbox on `PATH` and preserve any `AGENTBOX_HOME`,
`AGENTBOX_STATE`, or `XDG_STATE_HOME` overrides in your shell.

### Preset configuration

Create a configuration preset for an agentbox:

```bash
# Enable a provider in the default preset - set enabled: true
agentbox preset edit proxy
# or create a custom preset
agentbox preset copy default new-preset
agentbox preset edit proxy new-preset
agentbox preset edit agent new-preset
```

---

## Quickstart

```bash
cd ~/projects/my-app

# Initialise a session and launch tmux with the agent harness
agentbox init
agentbox start -- tmux -u new-session -s agent 'bash -l' ';' send-keys -t agent 'agent' Enter

# The agent has access to your current branch (read-only bundle).
# When it's done, push its work:
#   git push

# When the agent container exits, output is fetched automatically:
#   git fetch agentbox-<name>
#   git merge agentbox-<name>/my-feature
```

Some caveats: the start command is entirely customizable on the command line. By default if you don't provide a command to run it will launch the agent harness configured in the preset. The presets usually have some configuration in .bashrc that pre-configures the agent harness to be usable from the sandbox right way. Therefore it is almost always more preferable to use the tmux session start command echoed by the init command.

To keep the agent's workspace and home directory across starts, opt in at initialization:

```bash
agentbox init --name feature --persist
agentbox start --name feature -- bash     # first start chooses the saved command
# exit bash, then restart the same container and its filesystem
agentbox start --name feature
agentbox stop --name feature              # retain the stopped agent and proxy
agentbox remove --name feature            # delete the container and unpublished work
```

Only one `start` can own a persistent session at a time. Processes, including tmux, do not survive stopping the container. Push commits to the output remote before removing the session; auto-fetch runs after each attached exit unless `remove` has deleted the session while it was running.
After the first start, edits to `services.agent` in `compose.yaml` do not reconfigure the retained agent on the next start. Create a new session for a different agent image, environment, mounts, or command.

The default environment does not include any agent harness, so it can be used as a temporary sandbox.

Currently, for all provided agents you need to create your own preset (or modify the generated compose file) to configure them. Checkout [docs](docs/) for the specific agent harness setup guide.

---

## Usage

### Session lifecycle

```bash
agentbox init   [--name NAME] [--preset NAME] [--branch BRANCH] [--no-git] [--persist] \
                [--ro-mount SRC[:DST]] [--rw-mount SRC[:DST]] [--start]
agentbox start  [--name NAME] [-- CMD]   # launch agent harness (or CMD, e.g. -- bash, -- tmux)
agentbox stop   [--name NAME]            # stop containers
agentbox remove [--name NAME]            # stop, delete session, output repo, and git remote
```

`--name` defaults to a timestamp if not specified. If a project has exactly one session, it is auto-detected. By default, `agentbox start` can run independent agent containers concurrently; the proxy stays up until the last one exits, then `compose down` tears the project down. With `init --persist`, the agent is one retained Compose service container; `start -- CMD` is only valid before its first creation, later starts use its saved command, and a second simultaneous start fails. `stop` retains both persistent service containers; `remove` deletes them. Because agents run in krun microVMs, `podman exec` cannot reach a running container; use another ephemeral start or a separate session for parallel shells.

Everywhere where `--name` can be used, the parameter `--session` can also be used when you provide the session global ID found in `agentbox list --all`.

### Monitoring

```bash
agentbox logs [--name NAME] [--tail LAST_N_LINES]   # tail structured JSON access log from the proxy
agentbox web  [--name NAME]                         # print the mitmweb traffic-monitor URL
agentbox list [--all]                               # list sessions (optionally across all projects)
agentbox containers [--name NAME] [--json]          # list session containers and their states
agentbox status                                     # list all running agentbox containers
```

### Egress control

```bash
agentbox allow pypi.org [--name NAME]   # add host to allowlist (hot-reloads proxy config)
agentbox deny  pypi.org [--name NAME]   # remove host from allowlist
agentbox allow -r '.*\.example\.com'   # add a host regex verbatim (--regex also works)
agentbox deny  -r '.*\.example\.com'   # remove that exact regex rule
```

Hosts are escaped by default. Use `-r` / `--regex` to preserve the host regex
verbatim. Both modes support a `:port` suffix (default: 443).

To force HTTP/1.1 upstream for a destination, add the optional top-level
`force_http1_hosts` list, separate from request policies:

```yaml
force_http1_hosts:
  - host: 'api\.example\.com'
    port: 8443
```

The list defaults to `[]` (normal negotiation). Each entry requires a hostname
regex (full match) and accepts an optional integer or regex `port`, defaulting to
443. It applies to **all paths on the matching host and port**, whether allowed by
provider rules or `extra_request_policy`. It does not allow any requests or inject
credentials by itself: existing host/path/method policies are unchanged.

The addon uses mitmproxy's public upstream ALPN offers; it does not patch mitmproxy
internals or disable HTTP/2 globally. Client-facing HTTP/2 and TLS certificate
verification remain unchanged. Native gRPC or HTTP/2-only services must not be forced.

### Proxy management

```bash
agentbox proxy reload  [--name NAME]   # synchronize keys and reload providers in place
agentbox proxy restart [--name NAME]   # restart proxy, repopulate keys, and reload
agentbox proxy provider list [--name NAME] [--json]
agentbox proxy provider enable  PROVIDER [PROVIDER ...] [--name NAME]
agentbox proxy provider disable PROVIDER [PROVIDER ...] [--name NAME]
```

Provider commands manage the current session's configuration, not its preset.
`provider list` shows saved enabled/disabled settings, not live runtime status.
`provider enable` and `provider disable` accept one or more provider names and save
the settings without restarting the proxy or synchronizing credentials. All names
are checked before saving, so an unknown name leaves the configuration unchanged.
Run `agentbox proxy reload` for that session to apply
the edit immediately, or let the next `start` apply it. These commands also work
while the session is stopped. The old `proxy-reload` and `proxy-restart` commands
are replaced by `proxy reload` and `proxy restart`.

Provider files are synchronized **only** by `start`, `proxy reload`, and `proxy restart`.
Edit the current session's `proxy-config/proxy.yaml` or replace a host key file, then run
`proxy reload` to apply the change without interrupting active requests. Every `start`
synchronizes, including when it shares an existing proxy. One Compose exec transfers all
provider secrets and applies the reload. There is no file watcher.

Changes to `force_http1_hosts` destinations require `agentbox proxy restart --name NAME`:
existing pooled TLS connections cannot renegotiate. A reload detects such changes,
keeps the previous runtime configuration, and reports that a restart is required.
Path/method changes or credential rotation that leave those destinations unchanged
still reload normally. A restart interrupts active connections.

**Direct `podman compose up` and automatic container restarts do not synchronize keys.**
Their tmpfs starts empty. Requests requiring an unavailable credential receive HTTP 503
until a synchronization command runs; healthy providers and ordinary allowlist rules
continue working. A non-empty `api_key_env` is used as a fallback when configured.

`allow` and `deny` save the edit and reload only non-provider settings. If provider changes
are pending, the CLI reports that the saved edit is not active: run `proxy reload` once to
apply both changes. You do not need to repeat `allow` or `deny`.

### Reference mounts

Mount additional projects at `/context/<name>` when initializing a session:

```bash
agentbox init --ro-mount ~/libs/shared-lib:shared-lib --rw-mount ~/data/scratch:scratch
```

For a persistent session, set mounts during `init`. Existing containers keep their original mounts; create a new session to change them.

### Retrieving output

Output is fetched automatically when the session ends. To fetch mid-session:

```bash
git fetch agentbox-<name>
git log agentbox-<name>/my-feature
git diff HEAD agentbox-<name>/my-feature
git merge agentbox-<name>/my-feature
```

### Maintenance

```bash
agentbox remote-cleanup   # remove stale agentbox-* git remotes with no matching session
```

---

## Configuration

### Image tags

`AGENTBOX_IMAGE_TAG` selects the proxy, base agent, and built-in harness image tags.
It defaults to `latest` when unset or empty:

```bash
export AGENTBOX_IMAGE_TAG=stage
agentbox build
agentbox init --name testing --preset claude-vertex
agentbox start --name testing
```

Both `build` and `update` use this setting by default; an explicit `--tag TAG` (or
`-t TAG`) overrides it for that build, including the base image used by harnesses.
Build `base` for a new tag before building individual harnesses with that tag.
`--tag` does not change the tag used by a later `init`.

`init` uses `podman compose config` to resolve image references using the Compose
provider's native variable interpolation, then saves concrete references in the
session's `compose.yaml`. Only image fields are passed to this command; other
session settings retain their placeholders. Image interpolation uses exported
environment variables, not a local `.env` file, matching `build` and `update`.
Starting an existing session from another shell keeps
those references; re-running `init` regenerates them using the current setting
(subject to the persistent-session restrictions above).

Presets still override the agent image through `agent_image`. Custom presets can
inherit the selected tag using `my-registry/my-agent:${AGENTBOX_IMAGE_TAG:-latest}`.
Other image variables and interpolation forms are handled by the Compose provider.
Explicit image references, including pinned tags and
digests, are left unchanged. Update existing custom presets to use the placeholder
if they should follow this setting.

#### Separate stage installation

With a `stage` branch, create a separate worktree:

```bash
git -C ~/.local/share/agentbox worktree add ~/.local/share/agentbox-stage stage
```

Save this wrapper as `~/.local/bin/agentbox-stage` and make it executable:

```bash
#!/usr/bin/env bash
export AGENTBOX_HOME="$HOME/.local/share/agentbox-stage"
export AGENTBOX_STATE="$HOME/.local/state/agentbox-stage"
export AGENTBOX_IMAGE_TAG=stage
exec "$AGENTBOX_HOME/bin/agentbox" "$@"
```

Use `agentbox-stage build`, `agentbox-stage init --name testing`, and
`agentbox-stage start --name testing` to build and test independently. Merge
`stage` into `main` normally, then run `agentbox build` with the default `latest`
tag for the main installation. The wrapper selects the tag and separate session
state; no branch-specific image-tag edits are needed.

### Providers

Each session has its own `$AGENTBOX_STATE/sessions/<session-id>/proxy-config/proxy.yaml`, copied from the preset on `agentbox init`. The default state directory is `~/.local/state/agentbox`. Edit the session file to enable providers:

```yaml
providers:
  - name: anthropic
    enabled: true
    credential_type: static        # "static" (API key) or "oauth" (Google OAuth)
    api_key_env: ANTHROPIC_API_KEY # env var on the proxy side
    # api_key_file: ~/secrets/key  # host source synchronized by explicit commands
    inject_header: x-api-key
    inject_prefix: ""
    request_policy:
      - host: 'api\.anthropic\.com'
        paths: ['/v1/messages(/.*)?$', '/v1/complete$', '/v1/models(/.*)?$']
```

The real API key is read from the proxy container's environment or synchronized tmpfs — never
from the agent. `api_key_file` takes precedence. Host `~` expansion is supported; relative paths
resolve against the session's project directory, even with `--session` from another directory.
Replacing a file atomically or retargeting a symlink takes effect on the next synchronization.
Keys and their content digests travel through stdin, never command arguments or Compose.
Injected headers are redacted in structured access logs. Environment changes still require
container recreation.

### Vertex AI

```yaml
providers:
  - name: vertex
    enabled: true
    credential_type: oauth
    metadata_server: true
    inject_header: Authorization
    inject_prefix: "Bearer "
    replace_token: "dummy-replaced-by-proxy"
    request_policy:
      - host: '(.*-)?aiplatform\.googleapis\.com'
        paths: ['/v1/projects/${VERTEX_PROJECT_ID}/locations/${VERTEX_REGION}/publishers/.*']

proxy_volumes:
  - src: ~/.config/gcloud
    dst: /root/.config/gcloud

environment:
  GOOGLE_CLOUD_PROJECT: my-gcp-project
  VERTEX_PROJECT_ID: my-gcp-project
  VERTEX_REGION: global
```

The example mounts local gcloud ADC read-only. Alternatively, explicitly mount a service-account
file at `/oauth/credentials.json` using `proxy_volumes` and set
`GOOGLE_APPLICATION_CREDENTIALS: /oauth/credentials.json` in the preset's `environment`.
`proxy_volumes` use shared SELinux labels (`:ro,z`) so multiple sessions can read the same credentials.
OAuth discovery retains its existing behavior and is separate from `api_key_file` synchronization.

### Presets

Presets live at `$AGENTBOX_HOME/presets/<name>/` (built-in) or `$AGENTBOX_HOME/custom/presets/<name>/` (user-defined, takes precedence). Each contains `proxy.yaml` (provider config) and optionally `agent.yaml` (agent image, environment overrides, and dotfiles).

```bash
agentbox preset list
agentbox preset copy default mypreset
agentbox preset edit proxy mypreset    # edit provider config
agentbox preset edit agent mypreset    # edit agent env / dotfiles
agentbox init --preset mypreset --start
```

### Agent harness

The agent harness is configured via the preset's `agent.yaml`. Each harness has a dedicated container image with the harness binary installed. Built-in presets are available for `claude-vertex`, `opencode-vertex`, and the `default` (base image, no harness).

Set the harness in `agent.yaml`:

```yaml
agent_image: localhost/agentbox-agent-claude:${AGENTBOX_IMAGE_TAG:-latest}

environment:
  AGENT_HARNESS: claude
  AGENT_HARNESS_ARGS: --dangerously-skip-permissions
```

To add a harness not already supported, create a `Containerfile.<name>` in the `agent/` directory:

```dockerfile
ARG AGENTBOX_IMAGE_TAG=latest
FROM localhost/agentbox-agent-base:${AGENTBOX_IMAGE_TAG}
RUN npm install -g my-agent
```

Then `agentbox build <name>` or `agentbox build` to build all images.

---

## Updating

```bash
git -C ~/.local/share/agentbox pull
agentbox update              # rebuild all images without cache
agentbox update claude       # rebuild only the claude harness image
```

## Verification

Run `python3 -m unittest discover -s tests -v` with PyYAML installed. Installing
`zsh` enables real interactive Tab-completion tests and checks that every CLI
command, option, alias, and positional argument has a completion definition.
Installing `podman-compose==1.5.0` enables native image-interpolation and Compose argument checks; installing the proxy's
mitmproxy dependency enables the real TLS/ALPN and HTTP streaming/reload regressions.
TLS negotiation runs in memory without sockets. The streaming regression requires
working local TCP listeners and reports a skip when the environment cannot provide them.
