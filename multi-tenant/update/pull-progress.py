#!/usr/bin/env python3
"""Pull one Docker image and show aggregate layer download progress on one line."""
import errno
import os
import pty
import re
import subprocess
import sys
import time

LAYER_ID = re.compile(r"\b[0-9a-f]{8,64}\b", re.I)
AMOUNTS = re.compile(r"([\d.]+)\s*([kMGT]?i?B)\s*/\s*([\d.]+)\s*([kMGT]?i?B)", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
UNIT = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
        "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def bytes_value(number, unit):
    return float(number) * UNIT[unit.upper()]


def main():
    if len(sys.argv) != 2:
        return 2
    # Docker draws byte progress only when it sees a terminal on some versions.
    # Give it a private pty, then render one aggregate line in the panel log.
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(["docker", "pull", sys.argv[1]], stdout=slave,
                                stderr=slave, stdin=subprocess.DEVNULL)
    finally:
        os.close(slave)
    layers = {}
    pending = bytearray()
    last_display = 0.0
    last_text = ""
    errors = []

    def consume(raw):
        nonlocal last_display, last_text
        line = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
        layer_match = LAYER_ID.search(line)
        amounts = AMOUNTS.search(line) if "Downloading" in line else None
        if layer_match and amounts:
            layer = layer_match.group()
            current, current_unit, total, total_unit = amounts.groups()
            layers[layer] = (min(bytes_value(current, current_unit), bytes_value(total, total_unit)),
                             bytes_value(total, total_unit))
            now = time.monotonic()
            downloaded = sum(item[0] for item in layers.values()) / 1000**2
            total_mb = sum(item[1] for item in layers.values()) / 1000**2
            display = f"   Downloading {downloaded:.1f} MB / {total_mb:.1f} MB"
            if display != last_text and now - last_display >= 0.25:
                print("\r\x1b[2K" + display, end="", flush=True)
                last_display, last_text = now, display
        elif layer_match and "Download complete" in line and layer_match.group() in layers:
            _, total = layers[layer_match.group()]
            layers[layer_match.group()] = (total, total)
        elif "error" in line.lower() or "denied" in line.lower():
            errors.append(line)

    while True:
        try:
            chunk = os.read(master, 4096)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        for byte in chunk:
            if byte in (10, 13):
                if pending:
                    consume(pending)
                    pending.clear()
            else:
                pending.append(byte)
    if pending:
        consume(pending)
    os.close(master)
    rc = proc.wait()
    if layers:
        downloaded = sum(item[0] for item in layers.values()) / 1000**2
        total_mb = sum(item[1] for item in layers.values()) / 1000**2
        print(f"\r\x1b[2K   Downloaded {downloaded:.1f} MB / {total_mb:.1f} MB")
    elif rc == 0:
        print("   Pull completed; Docker did not report byte totals.")
    if rc:
        for error in errors[-3:]:
            print(error, file=sys.stderr)
        print(f"Docker pull failed for {sys.argv[1]} (exit {rc}).", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
