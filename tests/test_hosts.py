import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hosts


FIXTURE = (
  b"# local hosts\r\n"
  b"127.0.0.1\tlocalhost\r\n"
  b"::1 localhost ip6-localhost\r\n"
  b"\r\n"
  b"  127.0.0.1  app.test api.app.test  # development\r\n"
  b"# 192.0.2.10 staging.test # disabled\r\n"
  b"2001:db8::1 app.test\r\n"
  b"# prose must stay put: \xff\r\n"
  b"unrecognized content\r\n"
  b"192.0.2.20 final.test"
)


def request(data, operation="toggle", target=4, **extra):
  return {"revision": hosts.revision(data), "operation": operation, "id": target, **extra}


def fields(**extra):
  return {"address": "127.0.0.2", "hosts": ["new.test", "api.new.test"], "comment": "test", "enabled": True, **extra}


def concurrent_save(path, backups, lock, change, start, results):
  start.wait()
  try:
    hosts.save_file(path, backups, lock, change)
    results.put("ok")
  except hosts.HostsError as error:
    results.put(error.code)


class ParsingTests(unittest.TestCase):
  def test_read_recognizes_only_mappings_and_protects_names_not_addresses(self):
    rows = hosts.parse(FIXTURE)["entries"]
    self.assertEqual([row["id"] for row in rows], [1, 2, 4, 5, 6, 9])
    self.assertEqual([row["protected"] for row in rows], [True, True, False, False, False, False])
    self.assertEqual(rows[2]["hosts"], ["app.test", "api.app.test"])
    self.assertFalse(rows[3]["enabled"])
    self.assertEqual(rows[3]["comment"], "disabled")
    self.assertFalse(rows[2]["duplicates"], "IPv4 and IPv6 for one name are valid together")

  def test_toggle_round_trip_preserves_bytes_aliases_comments_and_line_endings(self):
    disabled = hosts.transform(FIXTURE, request(FIXTURE, enabled=False))
    self.assertEqual(disabled, FIXTURE.replace(b"  127.0.0.1  app", b"  # 127.0.0.1  app"))
    enabled = hosts.transform(disabled, request(disabled, enabled=True))
    self.assertEqual(enabled, FIXTURE)
    self.assertEqual(hosts.transform(enabled, request(enabled, enabled=True)), enabled)

  def test_enable_existing_comment_and_keep_inline_comment(self):
    data = b"\t#192.0.2.1 first.test second.test # keep this\n"
    self.assertEqual(hosts.transform(data, request(data, target=0, enabled=True)), b"\t192.0.2.1 first.test second.test # keep this\n")

  def test_add_edit_delete_preserve_every_unrelated_line(self):
    added = hosts.transform(FIXTURE, request(FIXTURE, "add", fields=fields(enabled=False)))
    self.assertEqual(added, FIXTURE + b"\r\n# 127.0.0.2\tnew.test api.new.test  # test\r\n")
    edited = hosts.transform(FIXTURE, request(FIXTURE, "edit", fields=fields(address="2001:0db8::2", comment="IPv6")))
    self.assertEqual(edited, FIXTURE.replace(b"  127.0.0.1  app.test api.app.test  # development", b"2001:db8::2\tnew.test api.new.test  # IPv6"))
    deleted = hosts.transform(FIXTURE, request(FIXTURE, "delete"))
    self.assertEqual(deleted, FIXTURE.replace(b"  127.0.0.1  app.test api.app.test  # development\r\n", b""))

  def test_empty_file_and_last_line_without_newline(self):
    added = hosts.transform(b"", request(b"", "add", fields=fields(comment="")))
    self.assertEqual(added, b"127.0.0.2\tnew.test api.new.test\n")
    edited = hosts.transform(FIXTURE, request(FIXTURE, "edit", target=9, fields=fields(comment="")))
    self.assertTrue(edited.endswith(b"127.0.0.2\tnew.test api.new.test"))

  def test_duplicates_are_case_insensitive_and_ignore_disabled_and_other_family(self):
    data = b"127.0.0.1 App.test\n192.0.2.1 app.test.\n# 192.0.2.2 APP.test\n::1 app.test\n"
    self.assertEqual([e["duplicates"] for e in hosts.parse(data)["entries"]], [["app.test"], ["app.test"], [], []])

  def test_localhost_cannot_be_added_retargeted_deleted_or_disabled(self):
    for op in ("edit", "delete", "toggle"):
      with self.subTest(op=op), self.assertRaisesRegex(hosts.HostsError, "read-only"):
        hosts.transform(FIXTURE, request(FIXTURE, op, target=1, enabled=False, fields=fields()))
    for name in ("LOCALHOST.", "localhost.localdomain", "localhost6.localdomain6", "ip6-loopback"):
      with self.subTest(name=name), self.assertRaisesRegex(hosts.HostsError, "read-only"):
        hosts.transform(FIXTURE, request(FIXTURE, "add", fields=fields(hosts=[name])))

  def test_validation_rejects_bad_addresses_names_controls_and_types(self):
    invalid = [
      {"address": "999.1.1.1"}, {"address": "fe80::1%eth0"}, {"address": "127.0.0.1\n"},
      {"hosts": []}, {"hosts": ["-bad.test"]}, {"hosts": ["bad..test"]},
      {"hosts": ["x.test\n192.0.2.1 evil.test"]}, {"hosts": ["a.test", "A.test."]},
      {"hosts": ["*.test"]}, {"hosts": ["https://a.test"]}, {"hosts": ["a" * 64 + ".test"]},
      {"comment": "comment\n127.0.0.1 injected.test"}, {"comment": "\x00"},
      {"comment": "x" * 1025}, {"enabled": "false"},
    ]
    for values in invalid:
      with self.subTest(values=values), self.assertRaises(hosts.HostsError):
        hosts.transform(FIXTURE, request(FIXTURE, "add", fields=fields(**values)))

  def test_stale_and_invalid_row_requests_cannot_retarget(self):
    for change in (request(FIXTURE, enabled=False, revision="stale"), request(FIXTURE, target=8, enabled=False), request(FIXTURE, target=True, enabled=False)):
      with self.subTest(change=change), self.assertRaises(hosts.HostsError):
        hosts.transform(FIXTURE, change)


class FileTests(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.directory = Path(self.temporary.name)
    self.path = self.directory / "hosts"
    self.path.write_bytes(FIXTURE)
    self.path.chmod(0o640)
    self.backups = self.directory / "state" / "backups"
    self.lock = self.directory / "hosts.lock"

  def save(self, change):
    return hosts.save_file(self.path, self.backups, self.lock, change)

  def test_atomic_save_preserves_permissions_and_backups_original(self):
    old_info = self.path.stat()
    result = self.save(request(FIXTURE, enabled=False))
    info = self.path.stat()
    self.assertNotEqual(info.st_ino, old_info.st_ino)
    self.assertEqual((info.st_uid, info.st_gid, info.st_mode), (old_info.st_uid, old_info.st_gid, old_info.st_mode))
    self.assertEqual(result, hosts.parse(self.path.read_bytes()))
    backup = list(self.backups.iterdir())[0]
    self.assertEqual(backup.read_bytes(), FIXTURE)
    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

  def test_noop_does_not_replace_file_or_create_backup(self):
    inode = self.path.stat().st_ino
    self.save(request(FIXTURE, enabled=True))
    self.assertEqual(self.path.stat().st_ino, inode)
    self.assertFalse(self.backups.exists())

  def test_stale_snapshot_leaves_file_and_backups_untouched(self):
    self.path.write_bytes(FIXTURE + b"\n# external edit\n")
    with self.assertRaisesRegex(hosts.HostsError, "changed"):
      self.save(request(FIXTURE, enabled=False))
    self.assertEqual(self.path.read_bytes(), FIXTURE + b"\n# external edit\n")
    self.assertFalse(self.backups.exists())

  def test_external_edit_during_save_is_preserved(self):
    read_file = hosts.read_file
    calls = 0

    def external_edit(path):
      nonlocal calls
      calls += 1
      if calls == 2:
        self.path.write_bytes(FIXTURE + b"\n# changed while saving\n")
      return read_file(path)

    with patch.object(hosts, "read_file", side_effect=external_edit), self.assertRaises(hosts.HostsError):
      self.save(request(FIXTURE, enabled=False))
    self.assertTrue(self.path.read_bytes().endswith(b"# changed while saving\n"))
    self.assertFalse(list(self.directory.glob(".omahosts-*")))

  def test_backup_failure_leaves_original_unchanged(self):
    with patch.object(hosts.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
      self.save(request(FIXTURE, enabled=False))
    self.assertEqual(self.path.read_bytes(), FIXTURE)
    self.assertFalse(list(self.backups.iterdir()))

  def test_replace_failure_leaves_original_and_valid_backup(self):
    with patch.object(hosts.os, "replace", side_effect=PermissionError("read-only")), self.assertRaises(OSError):
      self.save(request(FIXTURE, enabled=False))
    self.assertEqual(self.path.read_bytes(), FIXTURE)
    self.assertEqual(list(self.backups.iterdir())[0].read_bytes(), FIXTURE)
    self.assertFalse(list(self.directory.glob(".omahosts-*")))

  def test_cleanup_failure_reports_saved_state_with_warning(self):
    original = hosts.sync_directory

    def sync(path):
      if path == self.directory:
        raise OSError("sync failed")
      original(path)

    with patch.object(hosts, "sync_directory", side_effect=sync):
      result = self.save(request(FIXTURE, enabled=False))
    self.assertTrue(result["ok"])
    self.assertIn("warning", result)
    self.assertNotEqual(self.path.read_bytes(), FIXTURE)

  def test_only_twenty_backups_are_retained(self):
    for i in range(23):
      data = self.path.read_bytes()
      self.save(request(data, enabled=bool(i % 2)))
    self.assertEqual(len(list(self.backups.iterdir())), 20)

  def test_symlink_hardlink_and_unsafe_backup_directory_are_refused(self):
    link = self.directory / "link"
    link.symlink_to(self.path)
    with self.assertRaises(OSError):
      hosts.read_file(link)
    link.unlink()
    os.link(self.path, link)
    with self.assertRaises(hosts.HostsError):
      hosts.read_file(self.path)
    link.unlink()
    self.backups.mkdir(parents=True)
    self.backups.chmod(0o777)
    with self.assertRaises(hosts.HostsError):
      self.save(request(FIXTURE, enabled=False))
    self.assertEqual(self.path.read_bytes(), FIXTURE)

  def test_concurrent_saves_have_one_winner_and_one_conflict(self):
    context = multiprocessing.get_context("fork")
    start, results = context.Event(), context.Queue()
    changes = [request(FIXTURE, enabled=False), request(FIXTURE, "delete", target=9)]
    processes = [context.Process(target=concurrent_save, args=(self.path, self.backups, self.lock, change, start, results)) for change in changes]
    for process in processes:
      process.start()
    start.set()
    for process in processes:
      process.join(10)
      self.assertEqual(process.exitcode, 0)
    self.assertEqual(sorted([results.get(timeout=1), results.get(timeout=1)]), ["conflict", "ok"])
    self.assertIn(self.path.read_bytes(), [hosts.transform(FIXTURE, change) for change in changes])


class CliTests(unittest.TestCase):
  def test_cli_rejects_path_override_and_malformed_input(self):
    for arguments, data in [(["read", "/tmp/hosts"], ""), (["check"], "not json"), (["check"], "[]")]:
      with self.subTest(arguments=arguments, data=data):
        result = subprocess.run([sys.executable, "-I", "-B", hosts.__file__, *arguments], input=data, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
  unittest.main()
