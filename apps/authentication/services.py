from .models import PasswordResetToken

INVITE_TTL_MINUTES = 60 * 24 * 7  # 7 days — invites live longer than a forgot-password reset


def issue_invite(user):
    """Locks the account (no usable password) and issues a long-lived
    PasswordResetToken for it. The user can't log in via /v1/auth/login/
    until they complete the invite by POSTing the token + a chosen password
    to /v1/auth/reset-password/, which also logs them straight in — the
    same mechanism as a forgot-password reset, just a longer expiry and a
    must_change_password flag cleared on completion instead of set.
    """
    user.set_unusable_password()
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    return PasswordResetToken.issue(user, ttl_minutes=INVITE_TTL_MINUTES, kind="invite")
