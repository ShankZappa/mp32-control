"""Suite 4 — shared notes as cards, and the lease that keeps two editors apart.

Public Notes used to be a single shared textarea synced as one metadata field. Last-write-
wins over one field means the whole document: two people typing at once, and the later
write erased everything the other had written. Each card is now its own metadata key
(`card:<id>`), so two people on different cards never collide and two on the same card lose
one card at worst.

Locking is politeness, not correctness. A lease (`lock:<id>`, 15 s, renewed every 5 s while
a field is focused) marks a card read-only for everyone else and names who holds it. Leases
carry their own expiry, so an editor whose machine dies never leaves a card stuck, and
nothing depends on the lease being delivered — per-card last-write-wins still bounds the
damage to that one card if it is lost.

Replication runs over real controller processes; the lease and tombstone rules are browser
code, extracted verbatim from the shipped UI and run under node.
"""
from __future__ import annotations

import sys
import time

import jsbridge
from harness import Checks, Cluster, hlc, report, stale_hlc, wait_for, wait_value

LEASE_PRELUDE = """
const LEASE_MS=15000, LEASE_RENEW_MS=5000;
let clientId='client-A', clientName='This computer';
let editingCard=null, leaseTimer=null;
function renderCards(){}
function toast(){}
function hlcNow(){ return Date.now()*65536; }
"""


def card(title="", body="", deleted=False):
    return {"title": title, "body": body, "created": 1, "author": "test", "deleted": deleted}


def replication_checks(ck: Checks):
    with Cluster() as cl:
        a, b = cl.start_all("node-A", "node-B")
        wait_for(lambda: len(a.peers()) >= 1, 6)

        ca, cb = cl.key("card:ca"), cl.key("card:cb")

        print("  two controllers create cards at the same time")
        a.push(ca, card("Setup", "Kick in 1"))
        b.push(cb, card("Notes", "Bass DI"))
        ck.ok(wait_value(b, ca, card("Setup", "Kick in 1")), "B receives A's card")
        ck.ok(wait_value(a, cb, card("Notes", "Bass DI")), "A receives B's card")
        ck.ok(a.value(ca) == card("Setup", "Kick in 1"),
              "A's card survives B creating one — the shared-textarea defect")
        ck.ok(b.value(cb) == card("Notes", "Bass DI"),
              "B's card survives A creating one")

        print("  editing one card must not touch the other")
        a.push(ca, card("Setup", "Kick in 1, snare in 2"))
        ck.ok(wait_value(b, ca, card("Setup", "Kick in 1, snare in 2")),
              "an edit to one card propagates")
        ck.ok(b.value(cb) == card("Notes", "Bass DI"),
              "editing one card leaves the other untouched")

        print("  a controller joining with an empty store must wipe no cards")
        c = cl.start("node-C")
        ck.ok(wait_value(c, ca, card("Setup", "Kick in 1, snare in 2")),
              "the newcomer bootstraps existing cards")
        time.sleep(1.5)
        ck.ok(a.value(cb) == card("Notes", "Bass DI"),
              "a newly joined controller wipes nothing")

        print("  deletion is a tombstone, and a stale edit cannot resurrect it")
        b.push(ca, card("Setup", "", deleted=True))
        ck.ok(wait_for(lambda: (a.value(ca) or {}).get("deleted") is True),
              "a card tombstone propagates")
        a.push(ca, card("Setup", "typed before the delete arrived"), ts=stale_hlc())
        time.sleep(1.0)
        ck.ok((b.value(ca) or {}).get("deleted") is True,
              "a stale edit cannot resurrect a deleted card")

        print("  a lease is ordinary metadata and replicates like any other key")
        lk = cl.key("lock:ca")
        until = int(time.time() * 1000) + 15_000
        a.push(lk, {"client": "A", "name": "Studio Mac", "until": until})
        ck.ok(wait_for(lambda: (b.value(lk) or {}).get("client") == "A"),
              "a lease taken on one controller is visible on the other")
        ck.ok((b.value(lk) or {}).get("name") == "Studio Mac",
              "the lease names the holder, so the other end can say who has it")
        a.push(lk, {"client": "A", "name": "Studio Mac", "until": 0})
        ck.ok(wait_for(lambda: (b.value(lk) or {}).get("until") == 0),
              "releasing a lease propagates")

        print("  simultaneous edits to the SAME card must lose one card at most")
        newer = hlc(bump_ms=5_000)
        a.push(ca, card("Same", "from A"), ts=newer)
        b.push(ca, card("Same", "from B"), ts=stale_hlc())
        time.sleep(1.0)
        ck.ok(a.value(ca) == card("Same", "from A"),
              "the newer write wins on a contested card")
        ck.ok(wait_value(b, ca, card("Same", "from A")),
              "the losing controller converges instead of diverging")
        ck.ok(b.value(cb) == card("Notes", "Bass DI"),
              "a contested card costs only that card, never the others")


def browser_checks(ck: Checks):
    if not jsbridge.node_available():
        print("  SKIPPED: node is not on PATH, so the lease logic was not run")
        ck.ok(False, "browser-side lease logic could not be tested (node missing)")
        return

    fns = ["cardKey", "lockKey", "lockHolder", "heldByOther", "pushCard", "pushLock",
           "acquireLease", "releaseLease"]

    ck.equal(jsbridge.run(fns, "const result = { k: cardKey('x'), l: lockKey('x') };",
                          LEASE_PRELUDE),
             {"k": "card:x", "l": "lock:x"},
             "cards and locks use one key per card, never a shared key")

    r = jsbridge.run(fns, """
      meta.locks = {};
      const got = acquireLease('c1');
      const holder = lockHolder('c1');
      const result = { got, holderClient: holder && holder.client,
                       blockedForOther: !!heldByOther('c1'),
                       pushedKeys: apiKeys() };
    """, LEASE_PRELUDE)
    ck.ok(r["got"], "an uncontested lease is granted")
    ck.equal(r["holderClient"], "client-A", "the lease records who holds it")
    ck.ok(not r["blockedForOther"], "our own lease does not block ourselves")
    ck.equal(r["pushedKeys"], ["lock:c1"], "taking a lease pushes only that card's lock key")

    r = jsbridge.run(fns, """
      meta.locks = { c1:{ client:'client-B', name:'Phone', until: Date.now()+15000 } };
      const result = { blocked: !!heldByOther('c1'),
                       acquired: acquireLease('c1'),
                       name: heldByOther('c1') && heldByOther('c1').name };
    """, LEASE_PRELUDE)
    ck.ok(r["blocked"], "a card held by another controller is read-only here")
    ck.ok(not r["acquired"], "a lease held by someone else cannot be taken")
    ck.equal(r["name"], "Phone", "the UI can name who holds a contested card")

    r = jsbridge.run(fns, """
      // An editor whose browser or machine died: the lease is still in the store but its
      // expiry has passed. It must not keep the card locked for everyone else forever.
      meta.locks = { c1:{ client:'client-B', name:'Dead laptop', until: Date.now()-1 } };
      const result = { stillHeld: !!heldByOther('c1'),
                       holder: lockHolder('c1'),
                       reacquired: acquireLease('c1') };
    """, LEASE_PRELUDE)
    ck.ok(not r["stillHeld"], "an expired lease stops blocking, so a dead editor never sticks")
    ck.ok(r["holder"] is None, "an expired lease has no holder")
    ck.ok(r["reacquired"], "a card whose lease expired can be picked up again")

    r = jsbridge.run(fns, """
      meta.cards = { c1:{ title:'T', body:'B', deleted:false } };
      pushCard('c1', { ...meta.cards.c1, deleted:true, body:'' });
      const result = { keyPresent: 'c1' in meta.cards,
                       deleted: meta.cards.c1.deleted,
                       pushedKeys: apiKeys() };
    """, LEASE_PRELUDE)
    ck.ok(r["keyPresent"],
          "a deleted card keeps its key as a tombstone — absence must not mean deleted")
    ck.ok(r["deleted"], "the tombstone marks the card deleted")
    ck.equal(r["pushedKeys"], ["card:c1"], "deleting pushes only that card's key")

    # Guard the rules that live inside DOM-bound functions and cannot be lifted out.
    ck.ok(jsbridge.source_contains(r"const LEASE_MS=15000, LEASE_RENEW_MS=5000, CARD_SAVE_MS=800"),
          "the lease is still renewed well inside its own expiry (5 s renew, 15 s lease)")
    ck.ok(jsbridge.source_contains(r"if\(heldByOther\(id\)\)\{ stopEditing\(id,false\)"),
          "the loser of a simultaneous grab stops editing rather than fighting")
    ck.ok(jsbridge.source_contains(r"const id='migrated-public-notes'"),
          "the old shared textarea migrates under one deterministic id, so browsers converge "
          "instead of each adding a duplicate")
    ck.ok(jsbridge.source_contains(r"if\(cardsDirty && !editingCard"),
          "the card list is never rebuilt under a focused field")


def main() -> int:
    ck = Checks("shared notes cards")
    replication_checks(ck)
    browser_checks(ck)
    return report(ck)


if __name__ == "__main__":
    sys.exit(main())
