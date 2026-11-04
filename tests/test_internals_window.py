"""internals/window.sh control flow with stub ssh/rsync on PATH (nothing leaves the Mac).
Run: python3 tests/test_internals_window.py"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSH = r"""#!/usr/bin/env bash
while [[ $# -gt 0 && "$1" != *@* ]]; do shift; done; shift; cmd="$*"
echo "$cmd" >> "$STUB/calls"
case "$cmd" in
  *"sudo -n -l reboot"*) [[ -z "${NOSUDO:-}" ]];;
  *"sudo -n reboot"*) echo $(( $(cat "$STUB/boot") + 1 )) > "$STUB/boot"; exit 255;;
  *boot_id*) cat "$STUB/boot";;
  *"docker ps -q"*) ;;
  *"free -g | awk"*) echo "${FREE:-110 0}";;
  *"docker run"*) [[ -z "${FAIL_RUN:-}" ]];;
  *"v1/models"*) echo '{"data":[{"id":"Qwen/Qwen3.6-35B-A3B"}]}';;
  *"{{.Status}}"*) echo "Up 1 minute (healthy)";;
  *) echo "ok: $cmd";;
esac
"""


def window(**env):
    with tempfile.TemporaryDirectory() as td:
        stub = Path(td)
        (stub / "ssh").write_text(SSH)
        (stub / "rsync").write_text("#!/usr/bin/env bash\necho rsync \"$@\" >> \"$STUB/calls\"\n")
        for f in ("ssh", "rsync"):
            os.chmod(stub / f, 0o755)
        (stub / "boot").write_text("1\n")
        (stub / "calls").touch()
        e = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "STUB": str(stub), "POLL": "0",
             "LOCAL_OUT": str(stub / "out"), "GPU_HOST": "user@gpu",
             "GPU_VLLM_CONTAINER": "app-vllm", **env}
        p = subprocess.run(["bash", str(ROOT / "internals" / "window.sh")], env=e, capture_output=True, text=True,
                           timeout=120)
        return p.returncode, p.stdout + p.stderr, (stub / "calls").read_text().splitlines()


def idx(calls, needle):
    return [i for i, c in enumerate(calls) if needle in c]


class Window(unittest.TestCase):
    def test_success(self):
        rc, out, calls = window()
        self.assertEqual(rc, 0, out)
        self.assertIn("SUCCESS", out)
        reboots = idx(calls, "sudo -n reboot")
        self.assertEqual(len(reboots), 3)  # before, between smoke test and full run (pinned memory), after
        self.assertTrue(idx(calls, "--limit 2")[0] < reboots[1] < idx(calls, "--stage all")[0])
        self.assertEqual(len(idx(calls, "docker start app-vllm")), 1)
        self.assertLess(idx(calls, "docker stop")[0], idx(calls, "--limit 2")[0])
        self.assertLess(idx(calls, "--limit 2")[0], idx(calls, "--stage all")[0])
        self.assertTrue(any("chat/completions" in c for c in calls))

    def test_no_sudo_changes_nothing(self):
        rc, out, calls = window(NOSUDO="1")
        self.assertEqual(rc, 1)
        self.assertIn("passwordless sudo", out)
        self.assertFalse(idx(calls, "docker stop") or idx(calls, "docker start"))

    def test_gate_failure_restores(self):
        rc, out, calls = window(FREE="50 0")
        self.assertNotEqual(rc, 0)
        self.assertIn("gate FAILED", out)
        self.assertEqual(len(idx(calls, "sudo -n reboot")), 1)  # internals never ran: no second reboot
        self.assertEqual(len(idx(calls, "docker start app-vllm")), 1)
        self.assertFalse(idx(calls, "docker run"))

    def test_internals_failure_falls_back_then_restores(self):
        rc, out, calls = window(FAIL_RUN="1")
        self.assertNotEqual(rc, 0)
        runs = [c for c in calls if "docker run" in c]
        self.assertEqual(len(runs), 2)
        self.assertIn("--backend vllm", runs[0])
        self.assertIn("--backend hf", runs[1])
        self.assertTrue(idx(calls, "docker rm -f identity-internals"))
        self.assertEqual(len(idx(calls, "sudo -n reboot")), 2)  # reboot again before bringing vLLM back
        self.assertGreater(idx(calls, "docker start app-vllm")[0], idx(calls, "--backend hf")[0])
        self.assertIn("health OK", out)
        self.assertIn("FAILURE", out)


if __name__ == "__main__":
    unittest.main()
