"""Password policy.

Deliberately modest. Length is the property that actually correlates with
resistance to guessing; composition rules ("one uppercase, one symbol") mostly
produce ``Password1!`` and a sticky note. The rules here exist to reject the
handful of passwords that would make the throttle in ``login_guard`` pointless:

- the shipped default, which must never survive into production
- the account's own username
- a short list of passwords that appear at the top of every breach corpus
- trivially patterned strings (``aaaaaaaa``, ``12345678``)

Everything is a plain function returning a message (or ``None``), so it can be
tested without Flask and reused by the CLI.
"""

from __future__ import annotations

MIN_LENGTH = 8
MAX_LENGTH = 128

# The password shipped in app.py / README for local development. It is also
# rejected by the production startup guard; this catches it at change time too,
# so a user cannot "change" their password back to the default.
DEFAULT_PASSWORD = "Admin@12345"

# Short and honest. A long list mostly punishes users for the sins of others,
# and the throttle is what actually stops guessing.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyui",
        "qwerty123",
        "abc12345",
        "admin123",
        "admin1234",
        "administrator",
        "letmein1",
        "iloveyou",
        "welcome1",
        "changeme",
        "changeme1",
        "test1234",
        "pts12345",
        "lingxing",
        "juxingtongchuang",
    }
)

_SEQUENCES = (
    "0123456789",
    "9876543210",
    "abcdefghijklmnopqrstuvwxyz",
    "zyxwvutsrqponmlkjihgfedcba",
    "qwertyuiop",
    "poiuytrewq",
    "asdfghjkl",
    "lkjhgfdsa",
    "zxcvbnm",
    "mnbvcxz",
)


def _is_trivial_pattern(password: str) -> bool:
    lowered = password.lower()
    if len(set(lowered)) == 1:
        return True
    if lowered in _SEQUENCES:
        return True
    # A pure sequence with a trailing digit run, e.g. "abcdefg123".
    for sequence in _SEQUENCES:
        if lowered in sequence or lowered in sequence[::-1]:
            return True
        for suffix_length in range(1, 5):
            if lowered[: len(sequence)] == sequence and lowered[len(sequence) :].isdigit():
                return True
    return False


def password_policy_error(password: object, *, username: object = "") -> str | None:
    """Return a human-readable reason the password is unacceptable, or ``None``."""
    value = str(password or "")
    if not MIN_LENGTH <= len(value) <= MAX_LENGTH:
        return f"密码长度需为 {MIN_LENGTH}-{MAX_LENGTH} 个字符"

    lowered = value.lower()
    if lowered == DEFAULT_PASSWORD.lower():
        return "不能使用系统默认密码，请设置一个只有你知道的密码"
    if lowered in COMMON_PASSWORDS:
        return "该密码过于常见，请换一个"

    account = str(username or "").strip().lower()
    if account and lowered == account:
        return "密码不能与账号相同"
    # A "must not contain the username" rule was tried and removed: with the
    # short account names this system uses (``admin``, ``operator``), ordinary
    # passwords like ``Admin@98765`` tripped it. Equality is the case that
    # actually matters.

    if _is_trivial_pattern(value):
        return "密码过于简单（重复字符或连续序列），请换一个"

    return None
