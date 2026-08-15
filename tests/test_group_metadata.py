"""Suite 3 — per-group metadata sync (regression cover for a defect that shipped).

The defect: every group and stereo link lived in **one shared metadata key**, `groups`,
holding the whole object — while names and colours use one key per channel. Last-write-wins
on a whole-object key does not merge, it replaces, so whichever controller wrote last
silently discarded every group the other had. Two people grouping channels on two machines
was precisely the case that could not work.

The fix: each group is its own key, `group:<gid>`. Deletion writes a tombstone rather than
removing the key, because absence must not mean deleted — a controller joining with an
empty store would otherwise wipe everyone's groups.

Both halves are covered. The replication half runs over real controller processes and real
multicast; the tombstone and `groupOf` half is browser code, extracted verbatim from the
shipped UI and run under node, because that is where the defect actually lived.
"""
from __future__ import annotations

import sys
import time

import jsbridge
from harness import Checks, Cluster, hlc, report, stale_hlc, wait_for, wait_value


def group(members, mode="offset", deleted=False):
    g = {"members": list(members), "mode": mode}
    if deleted:
        g["members"], g["deleted"] = [], True
    return g


def replication_checks(ck: Checks):
    with Cluster() as cl:
        a, b = cl.start_all("node-A", "node-B")
        wait_for(lambda: len(a.peers()) >= 1, 6)

        ga, gb = cl.key("group:ga"), cl.key("group:gb")

        print("  two controllers group different channels at the same time")
        a.push(ga, group([0, 1]))
        b.push(gb, group([4, 5], mode="link"))
        ck.ok(wait_value(b, ga, group([0, 1])), "B receives the group A created")
        ck.ok(wait_value(a, gb, group([4, 5], mode="link")),
              "A receives the group B created")
        ck.ok(a.value(ga) == group([0, 1]),
              "A's own group survives B creating one — the original defect")
        ck.ok(b.value(gb) == group([4, 5], mode="link"),
              "B's own group survives A creating one")

        print("  editing one group must leave the other untouched")
        a.push(ga, group([0, 1, 2]))
        ck.ok(wait_value(b, ga, group([0, 1, 2])), "an edit to A's group propagates")
        ck.ok(b.value(gb) == group([4, 5], mode="link"),
              "editing one group does not disturb the other")

        print("  a controller joining with an empty store must wipe nothing")
        c = cl.start("node-C")
        ck.ok(wait_value(c, ga, group([0, 1, 2])), "the newcomer bootstraps existing groups")
        ck.ok(wait_value(c, gb, group([4, 5], mode="link")),
              "the newcomer bootstraps every existing group, not just one")
        time.sleep(1.5)   # let the newcomer announce its own (empty) state at least once
        ck.ok(a.value(ga) == group([0, 1, 2]),
              "a newly joined controller wipes nothing it has never heard of")
        ck.ok(a.value(gb) == group([4, 5], mode="link"),
              "the newcomer's empty store does not erase the second group either")

        print("  tombstones must propagate, and a stale edit must not resurrect one")
        b.push(ga, group([], deleted=True))
        ck.ok(wait_for(lambda: (a.value(ga) or {}).get("deleted") is True),
              "a tombstone written on one controller reaches the others")
        ck.ok(wait_for(lambda: (c.value(ga) or {}).get("deleted") is True),
              "the tombstone reaches every controller")
        a.push(ga, group([0, 1, 2]), ts=stale_hlc())
        time.sleep(1.0)
        ck.ok((b.value(ga) or {}).get("deleted") is True,
              "a stale edit cannot resurrect a deleted group")
        ck.ok((a.value(ga) or {}).get("deleted") is True,
              "the resurrecting controller keeps the tombstone itself")

        print("  a whole-object 'groups' event from an un-updated controller must not wipe")
        c.push(cl.key("groups"), {})
        time.sleep(1.0)
        ck.ok(a.value(gb) == group([4, 5], mode="link"),
              "an empty legacy whole-object event wipes no per-group key")
        ck.ok(b.value(gb) == group([4, 5], mode="link"),
              "the legacy event wipes nothing on the other controller either")

        print("  per-field last-write-wins: the newer timestamp must win, in either order")
        gc = cl.key("group:gc")
        newer = hlc(bump_ms=5_000)
        a.push(gc, group([8, 9]), ts=newer)
        b.push(gc, group([10, 11]), ts=stale_hlc())
        time.sleep(1.0)
        ck.ok(a.value(gc) == group([8, 9]), "an older write loses to a newer one")
        ck.ok(wait_value(b, gc, group([8, 9])),
              "the controller that wrote stale converges to the newer value")


def browser_checks(ck: Checks):
    """The tombstone and groupOf half, extracted verbatim from the shipped UI."""
    if not jsbridge.node_available():
        print("  SKIPPED: node is not on PATH, so the browser-side group logic was not run")
        ck.ok(False, "browser-side group logic could not be tested (node missing)")
        return

    fns = ["liveGroupIds", "pushGroup", "tombstoneGroup", "groupOf", "removeFromGroup"]

    r = jsbridge.run(fns, """
      meta.groups = { g1:{members:[0,1],mode:'offset'}, g2:{members:[4,5],mode:'link'} };
      tombstoneGroup('g1');
      const result = {
        live: liveGroupIds(),
        groupOf0: groupOf(0),
        groupOf4: groupOf(4) ? groupOf(4).gid : null,
        pushedKeys: __pushed.map(p => p.key),
        pushedValue: __pushed[0].value,
        keyStillPresent: 'g1' in meta.groups,
      };
    """)
    ck.equal(r["live"], ["g2"], "a tombstoned group drops out of the live group list")
    ck.ok(r["groupOf0"] is None, "groupOf() skips tombstoned groups")
    ck.equal(r["groupOf4"], "g2", "groupOf() still finds live groups")
    ck.equal(r["pushedKeys"], ["group:g1"],
             "deletion pushes the per-group key, never the whole object")
    ck.ok(r["pushedValue"].get("deleted") is True and r["pushedValue"]["members"] == [],
          "the tombstone carries deleted:true and empty members")
    ck.ok(r["keyStillPresent"],
          "the key is kept as a tombstone, not removed — absence must not mean deleted")

    r = jsbridge.run(fns, """
      meta.groups = { g1:{members:[0,1,2],mode:'offset'} };
      removeFromGroup(2);
      const afterOne = JSON.parse(JSON.stringify(meta.groups.g1));
      removeFromGroup(1);
      const result = {
        afterOne, afterTwo: meta.groups.g1,
        pushedKeys: __pushed.map(p => p.key),
      };
    """)
    ck.equal(r["afterOne"]["members"], [0, 1],
             "removing a member from a group of three leaves the group alive")
    ck.ok(r["afterTwo"].get("deleted") is True,
          "a group falling below two members is tombstoned, not left as a group of one")
    ck.ok(all(k == "group:g1" for k in r["pushedKeys"]),
          "every member change pushes only that group's own key")

    r = jsbridge.run(fns, """
      meta.groups = { g1:{members:[0,1],mode:'offset'} };
      tombstoneGroup('g1');
      const pushes = __pushed.length;
      tombstoneGroup('g1');
      const result = { pushes, pushesAfterRepeat: __pushed.length };
    """)
    ck.equal(r["pushesAfterRepeat"], r["pushes"],
             "tombstoning an already-deleted group is a no-op, so it cannot loop on the network")

    # The whole-object legacy branch lives inside syncMeta(), which is bound to fetch and the
    # DOM and cannot be lifted out as a standalone function. Guard its shape instead, so a
    # future edit that turns the merge back into a replace fails here.
    ck.ok(jsbridge.source_contains(
        r"key==='groups'.{0,400}?Object\.keys\(incoming\)\.forEach\(gid=>\{\s*if\(!meta\.groups\[gid\]\)"),
        "the legacy whole-object 'groups' branch still merges rather than replaces")
    ck.ok(not jsbridge.source_contains(r"pushMeta\('groups'"),
          "nothing in the shipped UI pushes the whole-object 'groups' key any more")


def main() -> int:
    ck = Checks("per-group metadata")
    replication_checks(ck)
    browser_checks(ck)
    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
