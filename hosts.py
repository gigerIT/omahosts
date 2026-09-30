#!/usr/bin/python3 -I
"""Hosts file operations. The CLI has fixed paths; tests call the file API."""

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

HOSTS = Path("/etc/hosts")
BACKUPS = Path("/var/lib/omahosts/backups")
LOCK = Path("/run/lock/omahosts.lock")
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 65536
LOCALHOST = re.compile(r"^(localhost([46])?(\.localdomain[46]?)?|ip6-localhost|ip6-loopback)$")
LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class HostsError(Exception):
  def __init__(self, code, message):
    self.code = code
    super().__init__(message)


def fail(code, message):
  raise HostsError(code, message)


def hostname_key(name):
  return name.rstrip(".").lower()


def valid_hostname(name):
  bare = name[:-1] if name.endswith(".") else name
  return bool(bare) and len(bare) <= 253 and all(LABEL.fullmatch(label) for label in bare.split("."))


def protected(hosts):
  return any(LOCALHOST.fullmatch(hostname_key(name)) for name in hosts)


def revision(data):
  return hashlib.sha256(data).hexdigest()


def line_parts(line):
  if line.endswith(b"\r\n"):
    return line[:-2], b"\r\n"
  if line.endswith(b"\n"):
    return line[:-1], b"\n"
  return line, b""


def parse(data):
  entries = []
  for index, line in enumerate(data.splitlines(keepends=True)):
    body, _ = line_parts(line)
    content = body.decode("utf-8", errors="surrogateescape").lstrip(" \t")
    enabled = not content.startswith("#")
    if not enabled:
      content = content[1:].lstrip(" \t")
    mapping, separator, comment = content.partition("#")
    tokens = mapping.split()
    if len(tokens) < 2 or not all(valid_hostname(name) for name in tokens[1:]):
      continue
    try:
      address = ipaddress.ip_address(tokens[0])
      if "%" in tokens[0]:
        continue
    except ValueError:
      continue
    entries.append({
      "id": index, "address": tokens[0], "hosts": tokens[1:],
      "comment": comment.strip() if separator else "", "enabled": enabled,
      "protected": protected(tokens[1:]), "family": address.version,
      "duplicates": [],
    })
  owners = {}
  for entry in entries:
    if entry["enabled"]:
      for name in entry["hosts"]:
        owners.setdefault((hostname_key(name), entry["family"]), []).append(entry)
  for (name, _), matching in owners.items():
    if len(matching) > 1:
      for entry in matching:
        if name not in entry["duplicates"]:
          entry["duplicates"].append(name)
  return {"ok": True, "revision": revision(data), "entries": entries}


def read_file(path):
  fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
  with os.fdopen(fd, "rb") as stream:
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
      fail("unsupported", "The hosts file must be a regular file without links.")
    data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
      fail("unsupported", "The hosts file is too large to edit (maximum 8 MiB).")
    return data, metadata


def validate_fields(fields):
  if not isinstance(fields, dict):
    fail("validation", "Entry fields are missing.")
  address = fields.get("address")
  names = fields.get("hosts")
  comment = fields.get("comment", "")
  enabled = fields.get("enabled")
  if not isinstance(address, str) or "%" in address or any(ord(c) < 32 for c in address):
    fail("validation", "Enter an IPv4 or IPv6 address.")
  try:
    address = str(ipaddress.ip_address(address.strip()))
  except ValueError:
    fail("validation", "Enter an IPv4 or IPv6 address.")
  if not isinstance(names, list) or not names or len(names) > 100:
    fail("validation", "Enter between 1 and 100 hostnames, separated by spaces.")
  if any(not isinstance(name, str) or not valid_hostname(name) for name in names):
    fail("validation", "Hostnames can contain letters, numbers, dots, and hyphens. Use punycode for international names.")
  if len(set(map(hostname_key, names))) != len(names):
    fail("validation", "Each hostname should appear only once in an entry.")
  if protected(names):
    fail("protected", "Localhost mappings are read-only.")
  if not isinstance(comment, str) or len(comment) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in comment):
    fail("validation", "Use a single-line comment of at most 1024 characters.")
  if type(enabled) is not bool:
    fail("validation", "Enabled must be true or false.")
  return {"address": address, "hosts": names, "comment": comment.strip(), "enabled": enabled}


def transform(data, request):
  if not isinstance(request, dict):
    fail("validation", "Invalid request.")
  if request.get("revision") != revision(data):
    fail("conflict", "The hosts file changed. Reload before trying again.")
  operation = request.get("operation")
  if operation not in ("add", "edit", "delete", "toggle"):
    fail("validation", "Unknown operation.")
  lines = data.splitlines(keepends=True)
  entry = None
  if operation != "add":
    target = request.get("id")
    if type(target) is not int:
      fail("validation", "Select an entry.")
    entry = next((row for row in parse(data)["entries"] if row["id"] == target), None)
    if entry is None:
      fail("conflict", "This entry is no longer available. Reload before trying again.")
    if entry["protected"]:
      fail("protected", "Localhost mappings are read-only.")
  if operation in ("add", "edit"):
    fields = validate_fields(request.get("fields"))
    text = ("" if fields["enabled"] else "# ") + fields["address"] + "\t" + " ".join(fields["hosts"])
    if fields["comment"]:
      text += "  # " + fields["comment"]
    encoded = text.encode("utf-8")
    if operation == "add":
      newline = b"\r\n" if b"\r\n" in data else b"\n"
      if lines and not lines[-1].endswith(b"\n"):
        lines[-1] += newline
      lines.append(encoded + newline)
    else:
      _, newline = line_parts(lines[entry["id"]])
      lines[entry["id"]] = encoded + newline
  elif operation == "delete":
    del lines[entry["id"]]
  else:
    enabled = request.get("enabled")
    if type(enabled) is not bool:
      fail("validation", "Enabled must be true or false.")
    if enabled != entry["enabled"]:
      body, newline = line_parts(lines[entry["id"]])
      indent = body[:len(body) - len(body.lstrip(b" \t"))]
      content = body[len(indent):]
      if enabled:
        content = content[1:]
        if content.startswith(b" "):
          content = content[1:]
      else:
        content = b"# " + content
      lines[entry["id"]] = indent + content + newline
  result = b"".join(lines)
  if len(result) > MAX_FILE_BYTES:
    fail("validation", "This change would exceed the 8 MiB file limit.")
  return result


def ensure_private_directory(path):
  path.mkdir(mode=0o700, parents=True, exist_ok=True)
  for directory in (path.parent, path):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
      fail("permissions", "The backup directory must be owned by the current user and not writable by others.")


def save_file(path, backups, lock_path, request):
  """Fixed paths at the CLI boundary; explicit paths here permit isolated tests."""
  lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
  with os.fdopen(lock_fd, "rb") as lock:
    lock_info = os.fstat(lock.fileno())
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or lock_info.st_nlink != 1:
      fail("permissions", "The hosts lock has unsafe ownership or links.")
    fcntl.flock(lock, fcntl.LOCK_EX)
    data, metadata = read_file(path)
    updated = transform(data, request)
    if updated == data:
      return parse(data)
    ensure_private_directory(backups)
    backup_fd, backup_name = tempfile.mkstemp(prefix=f"hosts-{time.time_ns()}-", dir=backups)
    try:
      with os.fdopen(backup_fd, "wb") as backup:
        backup.write(data)
        backup.flush()
        os.fsync(backup.fileno())
    except OSError:
      os.unlink(backup_name)
      raise
    sync_directory(backups)
    temporary = None
    try:
      fd, temporary = tempfile.mkstemp(prefix=".omahosts-", dir=path.parent)
      with os.fdopen(fd, "wb") as output:
        output.write(updated)
        output.flush()
        os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
        os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
        for name in os.listxattr(path, follow_symlinks=False):
          os.setxattr(output.fileno(), name, os.getxattr(path, name, follow_symlinks=False))
        os.fsync(output.fileno())
      # A stable sidecar lock serializes our writers across atomic replacements.
      # Other editors need not honor it, so compare again immediately before rename.
      current, current_metadata = read_file(path)
      if current != data or (current_metadata.st_dev, current_metadata.st_ino, current_metadata.st_mtime_ns, current_metadata.st_ctime_ns) != (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns):
        fail("conflict", "The hosts file changed. Reload before trying again.")
      os.replace(temporary, path)
      temporary = None
    finally:
      if temporary is not None:
        os.unlink(temporary)
    result = parse(updated)
    try:
      sync_directory(path.parent)
      for old in sorted(backups.glob("hosts-*"), key=lambda item: item.name, reverse=True)[20:]:
        old.unlink()
    except OSError:
      # The rename already committed: never report an unapplied change here.
      result["warning"] = "Saved, but backup cleanup or directory synchronization failed."
    return result


def sync_directory(path):
  fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def main():
  try:
    if len(sys.argv) != 2 or sys.argv[1] not in ("read", "check", "apply"):
      fail("validation", "Usage: hosts.py read|check|apply")
    action = sys.argv[1]
    if action == "read":
      result = parse(read_file(HOSTS)[0])
    else:
      raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
      if len(raw) > MAX_REQUEST_BYTES:
        fail("validation", "Request is too large.")
      request = json.loads(raw)
      if action == "check":
        result = parse(transform(read_file(HOSTS)[0], request))
      else:
        if os.geteuid() != 0:
          fail("permissions", "Administrator authentication is required to save.")
        result = save_file(HOSTS, BACKUPS, LOCK, request)
    print(json.dumps(result))
    return 0
  except HostsError as error:
    result = {"ok": False, "code": error.code, "error": str(error)}
  except (ValueError, UnicodeError, TypeError):
    result = {"ok": False, "code": "validation", "error": "Invalid request data."}
  except OSError as error:
    result = {"ok": False, "code": "io", "error": f"Could not access the hosts file or its backup: {error.strerror}."}
  print(json.dumps(result))
  return 1


if __name__ == "__main__":
  sys.exit(main())
