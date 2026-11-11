"""Integration tests for the vault index (run inside the container image).

    docker run --rm -v "$PWD/tests:/opt/tests" -v wdb-models:/models \
        --entrypoint python <image> /opt/tests/test_index.py

Uses the real models (downloaded to /models on first run) and a small German
test vault in a temporary directory - German on purpose, it exercises the
stemming full-text search. Prints OK or FAIL per test; exit code 1 on failures.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

os.environ.setdefault("STATUS_INTERVAL", "0")
os.environ.setdefault("CHECKPOINT_INTERVAL", "0")  # checkpoint after every block
os.environ.setdefault("FTS_LANGUAGE", "German")
sys.path.insert(0, "/opt/app")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from access import NoteMeta, compile_policy  # noqa: E402
from index import Config, NoteNotFound, NotReady, VaultIndex  # noqa: E402
from progress import BuildCancelled, progress  # noqa: E402

NOTES = {
    "Prozesse/PA - Wareneingang.md": """---
tags:
  - prozess
status: aktiv
---
# PA - Wareneingang

## Ablauf

Die Lieferung wird mit dem Lieferschein verglichen. Beschädigte Verpackungen werden fotografiert und auf dem Lieferschein vermerkt. Siehe [[AA - Sichtprüfung]] und [[Sperrlager]].

## Zuständigkeit

Der Lagerleiter ist verantwortlich.
""",
    "Anweisungen/AA - Sichtprüfung.md": """---
status: entwurf
---
# AA - Sichtprüfung

## Schritte

1. Verpackung auf Schäden prüfen.
2. Bei Schaden: Foto, Vermerk, Fahrer gegenzeichnen lassen.

Gehört zu [[PA - Wareneingang|Wareneingang]].
""",
    "Glossar/Sperrlager.md": """# Sperrlager

Gesperrte Ware wird im Sperrlager an der Ostwand gelagert, bis die Qualitätssicherung entscheidet. #qualität
""",
    "Personal/Urlaubsantrag.md": """---
tags: [personal/abwesenheit]
---
# Urlaubsantrag

## Schritte

Den Antrag beim Vorgesetzten stellen. Die Personalabteilung trägt den Urlaub ein.
""",
    "Glossar/Formiergas.md": """# Formiergas

Formiergas schützt die Wurzelseite einer Schweißnaht vor Oxidation und Anlauffarben. Siehe [[WIG-Schweißen]].
""",
}


def write_vault(root: Path, notes: dict[str, str]) -> dict[str, Path]:
    for rel, text in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*.md"))}


def top_note(idx: VaultIndex, q: str) -> str | None:
    res = idx.search(q, top_k=3)
    return res[0]["file_path"] if res else None


RESULTS: list[tuple[str, bool, str]] = []


def test(name):
    def deco(fn):
        def run(*a):
            t0 = time.monotonic()
            try:
                fn(*a)
                RESULTS.append((name, True, f"{time.monotonic() - t0:.1f} s"))
                print(f"OK      {name} ({time.monotonic() - t0:.1f} s)", flush=True)
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, False, repr(exc)))
                print(f"FAIL    {name}: {exc!r}", flush=True)
                traceback.print_exc()
        return run
    return deco


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    vault, index_dir = tmp / "vault", tmp / "index"
    files = write_vault(vault, NOTES)
    idx = VaultIndex(index_dir, Config())

    @test("Before the first build: search raises NotReady")
    def t0():
        try:
            idx.search("Urlaub")
        except NotReady:
            return
        raise AssertionError("no NotReady")
    t0()

    @test("First build")
    def t1():
        assert idx.update("c1", files) is True
        st = idx.state["stats"]
        assert st["notes"] == 5, st
        assert idx.state["changes"]["full"] is True
        assert idx.state["embeddings"]["computed"] > 0
    t1()

    @test("Search finds the right note, links and backlinks")
    def t2():
        res = idx.search("Wie beantrage ich Urlaub?", top_k=3)
        assert res[0]["file_path"] == "Personal/Urlaubsantrag.md", res[0]
        res = idx.search("Was tun bei beschädigter Lieferung?", top_k=5)
        notes = [r["file_path"] for r in res]
        assert "Prozesse/PA - Wareneingang.md" in notes[:2], notes
        we = next(r for r in res if r["file_path"] == "Prozesse/PA - Wareneingang.md")
        assert "Anweisungen/AA - Sichtprüfung.md" in we["links"], we
        assert "Anweisungen/AA - Sichtprüfung.md" in we["backlinks"], we  # alias link [[..|..]]
        assert "Glossar/Sperrlager.md" in we["links"]
        info = idx.note_info("Glossar/Formiergas.md")
        assert info["links"] == [], info  # [[WIG-Schweißen]] does not exist
        assert len({r["file_path"] for r in res}) == len(res), "duplicate notes"
    t2()

    @test("Full text with German stemming")
    def t3():
        res = idx.search("Lieferungen", mode="fulltext", rerank=False, top_k=3)
        assert res and res[0]["file_path"] in ("Prozesse/PA - Wareneingang.md", "Anweisungen/AA - Sichtprüfung.md"), res
    t3()

    @test("No change: no update")
    def t4():
        v = idx.state["version"]
        assert idx.update("c2", files) is False
        assert idx.state["version"] == v and idx.state["commit"] == "c2"
    t4()

    @test("Incremental: change, add, delete")
    def t5():
        (vault / "Personal/Urlaubsantrag.md").write_text(
            NOTES["Personal/Urlaubsantrag.md"] + "\n## Fristen\n\nUrlaub für Juli muss bis zum 31. März beantragt werden.\n",
            encoding="utf-8")
        (vault / "Glossar/Formiergas.md").unlink()
        new = {"Glossar/Gasflaschen.md": "# Gasflaschen\n\nArgon-Flaschen stehen im Gaslager an der Nordwand und werden mit Kette gesichert.\n"}
        f2 = write_vault(vault, new)
        assert idx.update("c3", f2) is True
        ch = idx.state["changes"]
        assert (ch["new"], ch["changed"], ch["deleted"], ch["full"]) == (1, 1, 1, False), ch
        emb = idx.state["embeddings"]
        # Urlaubsantrag: 2 sections unchanged (cache), 1 new; Gasflaschen 1 new
        assert emb["computed"] == 2 and emb["reused"] >= 1, emb
        assert top_note(idx, "Wo stehen die Argon-Flaschen?") == "Glossar/Gasflaschen.md"
        assert top_note(idx, "Bis wann muss ich Urlaub für Juli beantragen?") == "Personal/Urlaubsantrag.md"
        notes = {r["file_path"] for r in idx.search("Formiergas Wurzelschutz", top_k=8)}
        assert "Glossar/Formiergas.md" not in notes, notes
        assert idx.state["stats"]["notes"] == 5
    t5()

    @test("Search during an update returns the previous state")
    def t6():
        orig = idx.models.encode

        def slow(texts):
            if threading.current_thread().name == "upd":
                time.sleep(3)
            return orig(texts)
        idx.models.encode = slow
        try:
            write_vault(vault, {"Glossar/Laser.md": "# Laser\n\nDer Faserlaser schneidet Edelstahl bis 20 mm.\n"})
            f3 = {p.relative_to(vault).as_posix(): p for p in sorted(vault.rglob("*.md"))}
            th = threading.Thread(target=idx.update, args=("c4", f3), name="upd")
            th.start()
            time.sleep(1.0)
            assert idx.building is not None, "update is not running"
            t0 = time.monotonic()
            res = idx.search("Faserlaser schneidet Edelstahl", top_k=8)
            dt = time.monotonic() - t0
            assert "Glossar/Laser.md" not in {r["file_path"] for r in res}, "new state visible too early"
            assert dt < 2.5, f"search blocked ({dt:.1f} s)"
            th.join()
        finally:
            idx.models.encode = orig
        assert top_note(idx, "Faserlaser schneidet Edelstahl") == "Glossar/Laser.md"
    t6()

    @test("Cancel: previous state stays, embeddings cached, resume uses the cache")
    def t7():
        v_before = idx.state["version"]
        big = {f"Massen/Notiz {i:03d}.md": f"# Notiz {i}\n\nInhalt Nummer {i} über Bauteil {i * 7} und Prüfung {i * 3}.\n"
               for i in range(200)}
        f4 = write_vault(vault, big)
        orig = idx.models.encode
        calls = {"n": 0}
        old_batch = idx.cfg.embed_batch
        idx.cfg.embed_batch = 4  # blocks of 64 sections, so there are several cancellation points

        def counting(texts):
            calls["n"] += 1
            if calls["n"] == 2:
                progress.cancel_requested.set()
            return orig(texts)
        idx.models.encode = counting
        try:
            idx.update("c5", f4)
            raise AssertionError("not cancelled")
        except BuildCancelled:
            pass
        finally:
            idx.models.encode = orig
            idx.cfg.embed_batch = old_batch
        assert idx.state["version"] == v_before, "state changed"
        assert idx.state["commit"] == "c4"
        assert top_note(idx, "Faserlaser schneidet Edelstahl") == "Glossar/Laser.md"
        assert idx.update("c5", f4) is True
        emb = idx.state["embeddings"]
        assert emb["reused"] >= 64, emb  # at least the blocks computed before cancelling
        assert idx.state["stats"]["notes"] == 206, idx.state["stats"]
    t7()

    @test("Error while writing: fall back to the previous state")
    def t8():
        commit = idx.state["commit"]
        (vault / "Glossar/Laser.md").write_text("# Laser\n\nGeänderter Text zum Laser.\n", encoding="utf-8")
        f5 = {p.relative_to(vault).as_posix(): p for p in sorted(vault.rglob("*.md"))}
        orig = idx._ensure_indices

        def boom(*a, **k):
            raise RuntimeError("test error")
        idx._ensure_indices = boom
        try:
            idx.update("c6", f5)
            raise AssertionError("no error")
        except RuntimeError:
            pass
        finally:
            idx._ensure_indices = orig
        assert idx.state["commit"] == commit
        tbl = idx.db.open_table(idx.state["table"])
        assert tbl.version == idx.state["version"], (tbl.version, idx.state["version"])
        assert top_note(idx, "Faserlaser schneidet Edelstahl bis 20 mm") == "Glossar/Laser.md"
        assert idx.update("c6", f5) is True
    t8()

    @test("Restart after a crash while writing: roll back to the active state")
    def t9():
        tbl = idx.db.open_table(idx.state["table"])
        tbl.delete("note = 'Personal/Urlaubsantrag.md'")  # simulated half-finished write
        idx2 = VaultIndex(index_dir, Config())
        idx2.open()
        assert idx2.db.open_table(idx2.state["table"]).version == idx2.state["version"]
        assert top_note(idx2, "Wie beantrage ich Urlaub?") == "Personal/Urlaubsantrag.md"
        idx.open()
    t9()

    @test("Full rebuild with cache computes nothing, search keeps working")
    def t10():
        old_table = idx.state["table"]
        f6 = {p.relative_to(vault).as_posix(): p for p in sorted(vault.rglob("*.md"))}
        assert idx.update("c7", f6, force=True) is True
        emb = idx.state["embeddings"]
        assert emb["computed"] == 0, emb
        assert idx.state["table"] != old_table
        assert old_table not in idx._tables()
        assert top_note(idx, "Wie beantrage ich Urlaub?") == "Personal/Urlaubsantrag.md"
    t10()

    @test("Related notes (depth 2)")
    def t11():
        r = idx.related("Glossar/Sperrlager.md", depth=2)
        assert [x["file_path"] for x in r["backlinks"]] == ["Prozesse/PA - Wareneingang.md"], r
        assert any(x["file_path"] == "Anweisungen/AA - Sichtprüfung.md" for x in r["second_degree"]), r
    t11()

    @test("Prune cache")
    def t12():
        r = idx.prune_cache()
        assert r["after"] <= r["before"] and r["after"] == idx.state["stats"]["chunks"] or r["after"] > 0, r
    t12()

    # -- access control ----------------------------------------------------
    policy = {
        "roles": {"no-hr": {"deny": {"tags": ["personal"]}},
                  "no-drafts": {"deny": {"fields": {"status": ["entwurf"]}}}},
        "accounts": {
            "staff": {"allow": "all", "roles": ["no-hr", "no-drafts"]},
            "hr": {"allow": {"tags": ["personal"]}},
            "warehouse": {"allow": {"folders": ["Prozesse", "Glossar"]},
                          "deny": {"paths": ["Glossar/Gas*"]}},
            "nobody": {"allow": {"tags": ["does-not-exist"]}},
            "off": {"allow": "all", "disabled": True},
        },
    }
    accounts, _ = compile_policy(policy)

    @test("Access: SQL prefilter and Python predicate agree for every account and note")
    def ta1():
        snap = idx.snapshot()
        for name, acc in accounts.items():
            by_rule = {p for p, m in snap.notes.items() if acc.visible(NoteMeta.from_note(p, m))}
            flt = acc.sql_filter()
            if flt == "false":
                by_sql = set()
            else:
                q = snap.table.search()
                if flt:
                    q = q.where(flt)
                by_sql = set(q.select(["note"]).limit(100000).to_arrow().column("note").to_pylist())
            assert by_rule == by_sql, (name, sorted(by_rule ^ by_sql))
            assert set(idx.manifest(acc)) == by_rule, name

    @test("Access: deny tag (hierarchical) and field hide notes from search")
    def ta2():
        staff = accounts["staff"]
        for q in ("Wie beantrage ich Urlaub?", "Urlaub Antrag Vorgesetzter"):
            for mode in ("hybrid", "semantic", "fulltext"):
                notes = {r["file_path"] for r in idx.search(q, top_k=50, mode=mode, access=staff)}
                assert "Personal/Urlaubsantrag.md" not in notes, (q, mode, notes)
        res = idx.search("Was tun bei beschädigter Lieferung?", top_k=50, access=staff)
        notes = {r["file_path"] for r in res}
        assert "Anweisungen/AA - Sichtprüfung.md" not in notes, notes  # status: entwurf
        we = next(r for r in res if r["file_path"] == "Prozesse/PA - Wareneingang.md")
        assert "Anweisungen/AA - Sichtprüfung.md" not in we["links"] + we["backlinks"], we
        assert "Glossar/Sperrlager.md" in we["links"], we

    @test("Access: allow-only account sees only its notes")
    def ta3():
        hr = accounts["hr"]
        assert idx.manifest(hr) == ["Personal/Urlaubsantrag.md"], idx.manifest(hr)
        res = idx.search("Wo stehen die Argon-Flaschen?", top_k=10, access=hr)
        assert {r["file_path"] for r in res} <= {"Personal/Urlaubsantrag.md"}, res
        res = idx.search("Wie beantrage ich Urlaub?", top_k=3, access=hr)
        assert res and res[0]["file_path"] == "Personal/Urlaubsantrag.md", res

    @test("Access: folders plus deny path; read/related/explain respect the rules")
    def ta4():
        wh = accounts["warehouse"]
        vis = set(idx.manifest(wh))
        assert "Prozesse/PA - Wareneingang.md" in vis and "Glossar/Sperrlager.md" in vis, vis
        assert "Glossar/Gasflaschen.md" not in vis and "Personal/Urlaubsantrag.md" not in vis, vis
        try:
            idx.note_info("Glossar/Gasflaschen.md", wh)
            raise AssertionError("hidden note readable")
        except NoteNotFound:
            pass
        rel = idx.related("Glossar/Sperrlager.md", depth=2, access=wh)
        allp = [x["file_path"] for k in ("links", "backlinks", "second_degree") for x in rel.get(k, [])]
        assert "Anweisungen/AA - Sichtprüfung.md" not in allp, allp
        ex = idx.explain(wh, "Glossar/Gasflaschen.md")
        assert not ex["visible"] and "path" in ex["reason"], ex
        ex = idx.explain(accounts["staff"], "Personal/Urlaubsantrag.md")
        assert not ex["visible"] and "personal" in ex["reason"], ex

    @test("Access: empty and disabled accounts get nothing")
    def ta5():
        for name in ("nobody", "off"):
            assert idx.search("Urlaub", access=accounts[name]) == [], name
            assert idx.manifest(accounts[name]) == [], name

    @test("Access: filter overhead per search is small")
    def ta6():
        staff = accounts["staff"]
        for _ in range(2):
            idx.search("Lieferung prüfen", top_k=8, access=staff)
        t0 = time.monotonic()
        for _ in range(5):
            idx.search("Lieferung prüfen", top_k=8)
        base = (time.monotonic() - t0) / 5
        t0 = time.monotonic()
        for _ in range(5):
            idx.search("Lieferung prüfen", top_k=8, access=staff)
        filt = (time.monotonic() - t0) / 5
        print(f"        search {base * 1000:.0f} ms, with access filter {filt * 1000:.0f} ms")
        assert filt < base * 1.5 + 0.1, (base, filt)

    for t in (ta1, ta2, ta3, ta4, ta5, ta6):
        t()

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
