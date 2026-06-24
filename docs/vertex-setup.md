# Vertex setup

Whether you're configuring Claude Code, OpenCode or some other coding agent that you want to use with Google Vertex, you will need to create a custom preset with the correct GCP project and region.

First, log in with `gcloud` to initialize `~/.config/gcloud/application_default_credentials.json` file without your access token to Vertex.

```sh
gcloud auth application-default login
gcloud auth application-default set-quota-project <your quota project>
```

Create a custom preset by copying `claude-vertex` or `opencode-vertex` (or similar) and set the appropriate environment variables for your Vertex project and region.
