#!/usr/bin/env python3
"""Pull one Docker image and show aggregate layer download progress on one line."""
import os
import re
import subprocess
import sys
import time

LAYER = re.compile(r"^\s*([0-9a-f]{12,64}):\s+Downloading\b.*?([\d.]+)\s*([kMGT]?B)\s*/\s*([\d.]+)\s*([kMGT]?B)", re.I)
COMPLETE = re.compile(r"^\s*([0-9a-f]{12,64}):\s+Download complete\b", re.I)
UNIT = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def bytes_value(number, unit):
    return float(number) * UNIT[unit.upper()]


def main():
    if len(sys.argv) != 2:
        return 2
    proc = subprocess.Popen(["docker", "pull", sys.argv[1]], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)
    layers = {}
    pending = bytearray()
    last_display = 0.0
    last_text = ""
    errors = []

    def consume(raw):
        nonlocal last_display, last_text
        line = raw.decode("utf-8", "replace").strip()
        match = LAYER.search(line)
        if match:
            layer, current, current_unit, total, total_unit = match.groups()
            layers[layer] = (min(bytes_value(current, current_unit), bytes_value(total, total_unit)),
                             bytes_value(total, total_unit))
            now = time.monotonic()
            downloaded = sum(item[0] for item in layers.values()) / 1000**2
            total_mb = sum(item[1] for item in layers.values()) / 1000**2
            display = f"   Downloading {downloaded:.1f} MB / {total_mb:.1f} MB"
            if display != last_text and now - last_display >= 0.25:
                print("\r\x1b[2K" + display, end="", flush=True)
                last_display, last_text = now, display
        elif (completed := COMPLETE.search(line)) and completed.group(1) in layers:
            _, total = layers[completed.group(1)]
            layers[completed.group(1)] = (total, total)
        elif "error" in line.lower() or "denied" in line.lower():
            errors.append(line)

    while True:
        chunk = os.read(proc.stdout.fileno(), 4096)
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
