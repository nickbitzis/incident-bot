import pytest

from incidentbot.commands import CommandError, normalize_user, parse


def test_declare_with_quoted_title_and_options():
    c = parse('/incident declare sev2 "checkout errors" service=checkout ic=@ana')
    assert c.verb == "declare"
    assert c.args == ["sev2", "checkout errors"]
    assert c.options == {"service": "checkout", "ic": "@ana"}


def test_prefix_is_optional():
    assert parse("state identified").verb == "state"


def test_flags_are_collected():
    c = parse("runbook rollback-deploy service=checkout --dry-run --force")
    assert c.flags == {"dry-run", "force"}
    assert c.options == {"service": "checkout"}


def test_aliases_map_to_verbs():
    assert parse("severity sev1").verb == "sev"
    assert parse("rb flush-cache").verb == "runbook"


def test_empty_text_is_help():
    assert parse("").verb == "help"


def test_unknown_verb_rejected():
    with pytest.raises(CommandError, match="unknown command"):
        parse("explode now")


def test_unbalanced_quotes_rejected():
    with pytest.raises(CommandError, match="could not parse"):
        parse('update "never closed')


def test_missing_argument_message_includes_usage():
    c = parse("role")
    with pytest.raises(CommandError, match="Usage: /incident role"):
        c.arg(0, "role")


def test_quoted_text_with_equals_stays_positional():
    c = parse('update "error rate = 12% on checkout"')
    assert c.args == ["error rate = 12% on checkout"]
    assert c.options == {}


@pytest.mark.parametrize("raw,user", [("@Ana", "ana"), ("<@kofi>", "kofi"),
                                      ("<@U123|grace>", "u123"), ("ben", "ben")])
def test_normalize_user(raw, user):
    assert normalize_user(raw) == user
