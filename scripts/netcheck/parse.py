#!/usr/bin/env python3
"""parse.py — turn `iperf3 --bidir --json` output (on stdin, from the
netcheck-client pod's logs) into the NET_REFERENCE_MBPS value the
metrics-agent will use to normalize net_bw into net_pressure.

See docs/Технический план экспериментов.md §3.4. Deliberately stdlib-only
so it runs anywhere python3 does, no venv.

net_bw in the agent is rx+tx (pkg/cgroup/net.go TotalBytes), so the
reference must also be the both-directions aggregate: with --bidir that's
end.sum_sent (client->server) + end.sum_received (server->client). Falls
back to summing per-stream sender/receiver rates if the top-level sums are
absent (iperf3 JSON layout has drifted across 3.x versions)."""
from __future__ import annotations

import json
import sys


def _bits_per_second(report: dict) -> float:
    end = report.get("end", {})

    # Preferred: top-level aggregate sums (present in essentially all 3.x).
    sent = end.get("sum_sent", {}).get("bits_per_second")
    recv = end.get("sum_received", {}).get("bits_per_second")
    if sent is not None and recv is not None:
        return float(sent) + float(recv)

    # Fallback: sum every stream's sender+receiver rate (covers --bidir
    # layouts that only populate per-stream figures).
    total = 0.0
    found = False
    for stream in end.get("streams", []):
        for role in ("sender", "receiver"):
            bps = stream.get(role, {}).get("bits_per_second")
            if bps is not None:
                total += float(bps)
                found = True
    if found:
        return total

    raise ValueError(
        "no bits_per_second found in iperf3 JSON — was --json output "
        "captured intact? (check `kubectl logs pod/netcheck-client`)"
    )


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(
            "netcheck: empty input — the client pod may still be running "
            "(wait for STATUS Completed) or failed to reach the server.",
            file=sys.stderr,
        )
        return 1

    # `kubectl logs` merges the container's stdout AND stderr into one stream,
    # so the client pod's "server not ready, retry N" lines (emitted to stderr
    # while it waits for the iperf3 listener) land right before the JSON —
    # iperf3 itself can also print warnings first. Skip to the first '{' and
    # decode just that object, ignoring any leading noise or trailing output.
    brace = raw.find("{")
    if brace == -1:
        print("netcheck: no JSON object found in input.", file=sys.stderr)
        print("First 200 chars were:", file=sys.stderr)
        print(raw[:200], file=sys.stderr)
        return 1
    try:
        report, _ = json.JSONDecoder().raw_decode(raw[brace:])
    except json.JSONDecodeError as e:
        print(f"netcheck: could not decode iperf3 JSON ({e}).", file=sys.stderr)
        print("First 200 chars from the first brace were:", file=sys.stderr)
        print(raw[brace : brace + 200], file=sys.stderr)
        return 1

    if err := report.get("error"):
        print(f"netcheck: iperf3 reported an error: {err}", file=sys.stderr)
        return 1

    bps = _bits_per_second(report)
    mbps = bps / 1e6

    print(f"# cross-node realizable aggregate (rx+tx): {mbps:.0f} Mbit/s")
    print(f"# set this on the metrics-agent DaemonSet (see docs §3.4):")
    print(f"NET_REFERENCE_MBPS={mbps:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
