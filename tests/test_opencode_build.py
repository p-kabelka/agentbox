"""Exercise the OpenCode download stage without Podman or GitHub."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


CONTAINERFILE = Path(__file__).resolve().parents[1] / "agent" / "Containerfile.opencode"
STUBS = r"""
curl() {
    case "$1" in
        -fsSL)
            [ "$#" -eq 6 ] && [ "$2 $3" = "--header @-" ] &&
                [ "$4 $5" = "--header X-GitHub-Api-Version: 2026-03-10" ] || return 2
            printf '%s <%s>\n' "$6" "$(cat)" >&2
            case "$6" in
                *page=1) printf '%s\n' '[{"tag_name":"v1.9.0","draft":false,"prerelease":false}]' ;;
                *page=2) printf '%s\n' '[{"tag_name":"v2.2.4","draft":false,"prerelease":false}]' ;;
                *) return 2 ;;
            esac
            ;;
        -fsSLo)
            [ "$#" -eq 3 ] && [ -z "${GH_TOKEN-}" ] || return 2
            printf '%s\n' "$3" >&2
            ;;
        *) return 2 ;;
    esac
}
gh() { printf '%s <%s>\n' "$*" "$GH_TOKEN" >&2; }
tar() { :; }
chmod() { :; }
"""


@unittest.skipUnless(shutil.which("jq"), "The OpenCode download stage requires jq")
class OpenCodeBuildTest(unittest.TestCase):
    def run_stage(self, token, skip, version="latest"):
        stage = CONTAINERFILE.read_text().split("RUN --mount=", 1)[1].split("\n", 1)[1]
        stage = stage.split("\n\nFROM ", 1)[0].replace("/run/secrets/gh_token", '"$TEST_SECRET_FILE"')
        with tempfile.TemporaryDirectory() as temp:
            secret_file = Path(temp) / "gh_token"
            if token is not None:
                secret_file.write_text(f"{token}\n")
            env = dict(os.environ, TEST_SECRET_FILE=str(secret_file),
                       AGENTBOX_BUILD_OPENCODE_VERSION=version, AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION=skip)
            env.pop("GH_TOKEN", None)
            return subprocess.run(["sh", "-c", STUBS + stage], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, env=env, timeout=5)

    def test_release_lookup_uses_build_secret_on_each_page(self):
        for skip, token in (("0", "test-token"), ("1", "test-token"), ("1", None)):
            with self.subTest(skip=skip, has_token=bool(token)):
                result = self.run_stage(token, skip)
                self.assertEqual(result.returncode, 0, result.stderr)
                header = f"Authorization: Bearer {token}" if token else ""
                expected = [
                    f"https://api.github.com/repos/anomalyco/opencode/releases?per_page=100&page={page} <{header}>"
                    for page in (1, 2)
                ]
                expected.append("https://github.com/anomalyco/opencode/releases/download/v2.2.4/opencode-linux-x64.tar.gz")
                if skip == "0":
                    expected.append(f"release verify-asset v2.2.4 /tmp/opencode.tar.gz --repo anomalyco/opencode <{token}>")
                self.assertEqual(result.stderr.splitlines(), expected)

    def test_pinned_release_does_not_call_release_api(self):
        result = self.run_stage(None, skip="1", version="2.2.4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr,
                         "https://github.com/anomalyco/opencode/releases/download/v2.2.4/opencode-linux-x64.tar.gz\n")

    def test_verification_requires_build_secret(self):
        result = self.run_stage(None, skip="0")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("gh_token build secret is required", result.stderr)
