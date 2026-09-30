"""Exercise the fixed-path root CLI in a disposable Linux user namespace."""

from pathlib import Path
import shutil
import subprocess
import unittest


class PrivilegedCliTests(unittest.TestCase):
  @unittest.skipUnless(shutil.which("bwrap"), "Optional root CLI test requires bubblewrap")
  def test_full_cli_round_trip_with_isolated_system_paths(self):
    repo = Path(__file__).resolve().parents[1]
    sandbox = [
      "bwrap", "--unshare-all", "--die-with-parent", "--uid", "0", "--gid", "0",
      "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
      "--symlink", "usr/lib", "/lib64", "--symlink", "usr/bin", "/bin",
      "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/etc",
      "--tmpfs", "/run", "--dir", "/run/lock", "--tmpfs", "/var",
      "--dir", "/var/lib", "--ro-bind", str(repo), "/app", "--",
    ]
    probe = subprocess.run([*sandbox, "/usr/bin/true"], capture_output=True, text=True, timeout=10)
    if probe.returncode:
      self.skipTest("Unprivileged user namespaces unavailable: " + probe.stderr.strip())
    driver = r'''
import json, os, pathlib, subprocess
assert os.geteuid() == 0
path = pathlib.Path("/etc/hosts")
original = b"# isolated fixture\n127.0.0.1 localhost\n::1 localhost\n"
path.write_bytes(original)
path.chmod(0o644)

def call(action, request=None, success=True):
  result = subprocess.run(
    ["/usr/bin/python3", "-I", "-B", "/app/hosts.py", action],
    input=json.dumps(request) + "\n" if request is not None else "",
    text=True, capture_output=True, timeout=10,
  )
  data = json.loads(result.stdout)
  assert data["ok"] == success, data
  assert (result.returncode == 0) == success, result.stderr
  return data

initial = call("read")
request = {"operation": "add", "revision": initial["revision"], "fields": {
  "address": "127.0.0.2", "hosts": ["app.test", "api.app.test"], "comment": "fixture", "enabled": True,
}}
call("check", request)
assert path.read_bytes() == original
added = call("apply", request)
assert added["entries"][-1]["hosts"] == ["app.test", "api.app.test"]
assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o644
backups = pathlib.Path("/var/lib/omahosts/backups")
assert next(backups.iterdir()).read_bytes() == original
assert call("apply", request, success=False)["code"] == "conflict"
disabled = call("apply", {"operation": "toggle", "revision": added["revision"], "id": 3, "enabled": False})
assert not disabled["entries"][-1]["enabled"]
assert b"# 127.0.0.2\tapp.test api.app.test" in path.read_bytes()
edited = call("apply", {"operation": "edit", "revision": disabled["revision"], "id": 3, "fields": {
  "address": "2001:db8::1", "hosts": ["new.test"], "comment": "", "enabled": True,
}})
assert edited["entries"][-1]["address"] == "2001:db8::1"
deleted = call("apply", {"operation": "delete", "revision": edited["revision"], "id": 3})
assert path.read_bytes() == original
assert call("apply", {"operation": "delete", "revision": deleted["revision"], "id": 1}, success=False)["code"] == "protected"
assert path.read_bytes() == original
print("Privileged CLI: add, toggle, edit, delete, backups, permissions, and conflicts passed")
'''
    result = subprocess.run([*sandbox, "/usr/bin/python3", "-I", "-B", "-c", driver], capture_output=True, text=True, timeout=30)
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
