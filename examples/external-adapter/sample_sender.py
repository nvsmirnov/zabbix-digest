#!/usr/bin/env python3
"""Example external sender for the Zabbix Digest "external" transport.

Reads the structured message (JSON) from stdin and appends a one-line summary to a file.
Exit codes: 0 delivered, 75 temporary failure (Core retries), any other code permanent failure.

Core configuration:
  transports:
    file_log:
      type: external
      settings:
        command: ["/usr/bin/python3", "/opt/sender/sample_sender.py", "/tmp/digest.log"]
"""

import json
import sys

EX_TEMPFAIL = 75


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sample_sender.py OUTPUT_FILE", file=sys.stderr)
        return 2
    try:
        message = json.load(sys.stdin)
    except ValueError:
        print("invalid JSON on stdin", file=sys.stderr)
        return 2
    if message.get("schema_version") != 1:
        print("unsupported schema version", file=sys.stderr)
        return 2
    kind = message["type"]
    sub = message["subscription"]
    if kind == "error":
        summary = f"ERROR {message['error']['category']}: {message['error']['message']}"
    else:
        digest = message["digest"]
        summary = f"{digest['total']} problems, {digest['shown']} shown"
        if kind == "test":
            summary = "TEST " + summary
    line = f"{message['generated_at']} {message['recipient']['email']} #{sub['id']} {summary}\n"
    try:
        with open(sys.argv[1], "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"cannot write: {exc}", file=sys.stderr)
        return EX_TEMPFAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
