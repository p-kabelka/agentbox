# Cursor CLI setup

To get started with Cursor for the first time or to refresh access token, first create a temporary session where you will authenticate with Cursor to generate an access token:

```sh
mkdir tmp-cursor-refresh
cd tmp-cursor-refresh
agentbox init --preset cursor --name cursor-refresh --no-git
agentbox start --name cursor-refresh -- bash -c 'env CURSOR_REFRESH_TOKEN=1 /root/.local/bin/agent login && jq -r ".accessToken" ~/.config/cursor/auth.json'
agentbox remove
```

The `cursor` preset expects the token in `~/.keys/cursor-token` by default, if you want to change the path you need to create a custom preset.

Paste the token you retrieved from the temporary session to the file configured in `api_key_file` field in in your preset.

Now you can initialize and start the session normally:

```sh
agentbox init --preset cursor
agentbox start -- tmux -u new-session -s agent 'bash -l' ';' send-keys -t agent 'agent' Enter
```
