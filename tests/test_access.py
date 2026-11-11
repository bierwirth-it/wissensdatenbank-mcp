"""Unit tests for the access policy (no models needed).

    pip install pyyaml pytest && pytest tests/test_access.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from access import (  # noqa: E402
    FULL_ACCESS, Criteria, NoteMeta, Policy, PolicyEditor, PolicyError, compile_policy, expand_tags,
    field_entries, token_hash,
)


def meta(path: str, tags=(), fields=None) -> NoteMeta:
    return NoteMeta.from_note(path, {"acl_tags": expand_tags(list(tags)), "acl_fields": field_entries(fields or {})})


POLICY = {
    "roles": {
        "no-hr": {"deny": {"tags": ["HR"]}},
        "no-drafts": {"deny": {"fields": {"status": ["draft"]}}},
        "production": {"allow": {"folders": ["20 Work instructions"], "tags": ["area/production"]}},
    },
    "accounts": {
        "admin": {"allow": "all", "permissions": ["admin"]},
        "staff": {"allow": "all", "roles": ["no-hr", "no-drafts"]},
        "hr": {"allow": {"tags": ["hr"], "folders": ["30 HR"]}},
        "workshop": {"roles": ["production", "no-drafts"], "deny": {"paths": ["20 Work instructions/WI - CNC*"]}},
        "blocked": {"allow": "all", "disabled": True},
    },
}


@pytest.fixture(scope="module")
def accounts():
    return compile_policy(POLICY)[0]


def test_tag_helpers():
    assert expand_tags(["#Area/Production/Welding"]) == ["area", "area/production", "area/production/welding"]
    assert field_entries({"status": "Draft", "tags": ["x"], "owner": ["A", "B"]}) == \
        ["status=draft", "owner=a", "owner=b"]


def test_allow_all_except(accounts):
    staff = accounts["staff"]
    assert staff.visible(meta("10 Processes/Goods receipt.md", ["process"], {"status": "active"}))
    assert not staff.visible(meta("30 HR/Leave.md", ["hr/absence"]))           # hierarchical tag
    assert not staff.visible(meta("10 Processes/New.md", [], {"status": "draft"}))
    assert staff.sql_filter().startswith("NOT (")


def test_allow_only(accounts):
    hr = accounts["hr"]
    assert hr.visible(meta("30 HR/Sub/Leave.md"))                               # folder includes subfolders
    assert hr.visible(meta("Misc/Note.md", ["HR"]))
    assert not hr.visible(meta("30 HRX/Note.md"))                               # folder is not a prefix match
    assert not hr.visible(meta("10 Processes/Goods receipt.md", ["process"]))


def test_roles_merge_and_deny_wins(accounts):
    ws = accounts["workshop"]
    assert ws.visible(meta("20 Work instructions/WI - Welding.md"))
    assert ws.visible(meta("Elsewhere/Note.md", ["area/production/welding"]))
    assert not ws.visible(meta("20 Work instructions/WI - CNC milling.md"))     # deny path wins
    assert not ws.visible(meta("20 Work instructions/WI - Draft.md", [], {"status": "draft"}))
    assert not ws.visible(meta("10 Processes/Goods receipt.md"))
    ok, why = ws.explain(meta("20 Work instructions/WI - CNC milling.md"))
    assert not ok and "deny" in why and "path" in why


def test_full_and_disabled(accounts):
    assert accounts["admin"].unrestricted and accounts["admin"].sql_filter() is None
    assert accounts["admin"].can("admin") and not accounts["staff"].can("admin")
    assert accounts["staff"].can("search") and accounts["staff"].can("read")
    blocked = accounts["blocked"]
    assert blocked.sql_filter() == "false" and not blocked.visible(meta("a.md"))
    assert FULL_ACCESS.unrestricted


def test_sql_escaping():
    c = Criteria(tags={"it's"}, paths={"100%_done/*"})
    sql = c.sql()
    assert "'it''s'" in sql and "100\\%\\_done/%" in sql


@pytest.mark.parametrize("bad", [
    {"accounts": {"a": {"allow": {"tag": ["x"]}}}},                 # unknown criterion
    {"accounts": {"a": {"roles": ["missing"]}}},                    # unknown role
    {"accounts": {"a": {"permissions": ["root"]}}},                 # unknown permission
    {"accounts": {"a": {"allow": "everything"}}},                   # allow must be 'all' or mapping
    {"accounts": {"a": {"tokens": [{"sha256": "x"}]}, "b": {"tokens": [{"sha256": "x"}]}}},
    {"roles": {"r": {"roles": ["x"]}}},                             # nested roles
])
def test_invalid_policies(bad):
    with pytest.raises(PolicyError):
        compile_policy(bad)


def test_policy_file_tokens_and_reload(tmp_path):
    path = tmp_path / "access.yaml"
    pol = Policy(path, legacy_token="")
    assert pol.open_access and pol.authenticate(None) is FULL_ACCESS   # no policy, no token: open

    ed = PolicyEditor(path)
    ed.add("account", "staff", allow_all=True)
    ed.rule("account", "staff", "deny", "tag", "hr")
    ed.rule("account", "staff", "deny", "field", "status=draft")
    tid, token = ed.create_token("staff")
    ed.save()
    assert token not in path.read_text() and token_hash(token) in path.read_text()

    import os
    os.utime(path, (1, 1))   # make sure the mtime changes for the reload check
    acc = pol.authenticate(token)
    assert acc is not None and acc.name == "staff"
    assert pol.authenticate("wrong") is None and pol.authenticate(None) is None

    ed = PolicyEditor(path)
    ed.revoke_token("staff", tid)
    ed.save()
    os.utime(path, (2, 2))
    assert pol.authenticate(token) is None

    # an invalid file keeps the previous policy
    ed = PolicyEditor(path)
    _, token2 = ed.create_token("staff")
    ed.save()
    os.utime(path, (3, 3))
    assert pol.authenticate(token2) is not None
    path.write_text("accounts: {x: {allow: nope}}")
    os.utime(path, (4, 4))
    assert pol.authenticate(token2) is not None and pol.error


def test_legacy_token(tmp_path):
    pol = Policy(tmp_path / "missing.yaml", legacy_token="secret")
    assert pol.authenticate("secret") is FULL_ACCESS
    assert pol.authenticate("other") is None and pol.authenticate(None) is None


def test_editor_rules_roundtrip(tmp_path):
    path = tmp_path / "access.yaml"
    ed = PolicyEditor(path)
    ed.add("role", "no-drafts")
    ed.rule("role", "no-drafts", "deny", "field", "status=draft")
    ed.add("account", "ws")
    ed.rule("account", "ws", "allow", "folder", "20 Work instructions")
    ed.set_roles("ws", "no-drafts")
    with pytest.raises(PolicyError):
        ed.remove("role", "no-drafts")          # still in use
    ed.save()
    raw = yaml.safe_load(path.read_text())
    assert raw["accounts"]["ws"]["allow"] == {"folders": ["20 Work instructions"]}
    acc = compile_policy(raw)[0]["ws"]
    assert acc.visible(meta("20 Work instructions/A.md"))
    assert not acc.visible(meta("20 Work instructions/B.md", [], {"status": "draft"}))
    ed.rule("account", "ws", "allow", "folder", "20 Work instructions", remove=True)
    ed.save()
    assert compile_policy(yaml.safe_load(path.read_text()))[0]["ws"].sql_filter() == "false"
