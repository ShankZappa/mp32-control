"""Suite 7 — metadata survives a controller restart.

Metadata used to live only in `PeerService.fields`, in memory. Restarting a controller
emptied it, and the first browser to reconnect re-seeded whatever it still held —
including values that had been deleted while it was closed. Observed on 2026-08-19:
channel colours deleted through the API came back verbatim after a restart, re-seeded by a
browser tab that had never seen the deletion.

Persisting the field set fixes it at the root. The network's current state outlives the
process, so a stale copy arriving afterwards loses the timestamp comparison instead of
winning by default.

Real controller processes, each with its own state directory so a run can never read or
write the real user's metadata.
"""
from __future__ import annotations

import json
import os
import sys
import time

from harness import Checks, Cluster, hlc, report, stale_hlc, wait_for, wait_value


def main() -> int:
    ck = Checks("metadata persistence")
    with Cluster() as cl:
        state = cl.state_dir("node-A")
        name, colour, card = cl.key("name:4"), cl.key("color:4"), cl.key("card:c1")

        print("  write metadata, then stop the controller")
        a = cl.start("node-A", state_dir=state)
        a.push(name, "Snare Top")
        a.push(colour, "#ff5252")
        a.push(card, {"title": "Session", "body": "notes", "deleted": False})
        ck.equal(a.value(name), "Snare Top", "metadata is accepted")
        a.stop()

        path = os.path.join(state, "metadata.json")
        ck.ok(os.path.exists(path), "the field set is written to disk on shutdown")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        ck.ok(isinstance(saved.get("fields"), dict), "the file holds a field map")
        ck.equal(saved["fields"].get(name, {}).get("value"), "Snare Top",
                 "the saved value is the one that was written")
        ck.ok(isinstance(saved["fields"].get(name, {}).get("ts"), int),
              "each saved field keeps its timestamp, without which it cannot be compared")

        print("  restart onto the same state directory")
        b = cl.start("node-A", state_dir=state)
        ck.equal(b.value(name), "Snare Top", "metadata is restored after a restart")
        ck.equal(b.value(colour), "#ff5252", "every key is restored, not just one")
        ck.equal((b.value(card) or {}).get("title"), "Session",
                 "structured values survive the round trip")

        print("  a stale copy arriving afterwards must lose, not resurrect")
        b.push(name, "OLD NAME", ts=stale_hlc())
        time.sleep(0.5)
        ck.equal(b.value(name), "Snare Top",
                 "a stale write is rejected because the restored timestamp still stands")

        print("  a deletion must survive a restart too — this is the reported defect")
        b.push(colour, "")
        ck.equal(b.value(colour), "", "the deletion is accepted")
        b.stop()
        c = cl.start("node-A", state_dir=state)
        ck.equal(c.value(colour), "", "the deletion is still in force after a restart")
        c.push(colour, "#ff5252", ts=stale_hlc())
        time.sleep(0.5)
        ck.equal(c.value(colour), "",
                 "a browser re-seeding its old colour cannot undo the deletion")

        print("  a newer write must still win")
        c.push(colour, "#00c853", ts=hlc(bump_ms=5_000))
        ck.equal(c.value(colour), "#00c853", "a genuinely newer value is accepted")

        print("  a controller with no stored state starts empty and inherits from the peer")
        fresh = cl.start("node-B", state_dir=cl.state_dir("node-B"))
        ck.ok(wait_value(fresh, name, "Snare Top"),
              "a controller with an empty store bootstraps from the network")
        ck.ok(wait_value(fresh, colour, "#00c853"),
              "it inherits the current value, not a resurrected one")

        print("  flushing happens while running, not only at shutdown")
        c.push(cl.key("name:9"), "Hat")
        ck.ok(wait_for(lambda: os.path.exists(path) and
                       json.load(open(path, encoding="utf-8"))["fields"]
                       .get(cl.key("name:9"), {}).get("value") == "Hat", 8),
              "a write reaches disk without waiting for the process to exit")

        print("  a corrupt or truncated file must not take the controller down")
        d_state = cl.state_dir("node-C")
        with open(os.path.join(d_state, "metadata.json"), "w", encoding="utf-8") as fh:
            fh.write('{"fields": {"broken": ')      # truncated mid-write
        d = cl.start("node-C", state_dir=d_state)
        ck.ok(isinstance(d.fields(), dict),
              "a controller starts normally despite an unreadable state file")
        d.push(cl.key("name:1"), "Recovered")
        ck.equal(d.value(cl.key("name:1")), "Recovered",
                 "and it can write again, replacing the bad file")

    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
