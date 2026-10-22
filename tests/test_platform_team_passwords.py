"""Super Admin team members get a temporary password (2026-10-13, per
Karwin): POST /platform/team/ needs no password - the server generates one,
returns it once and flags must_change_password; verify-2fa reports the flag;
POST /platform/auth/change-password/ lets any Super Admin set their own."""
import pytest
from rest_framework.test import APIClient

from apps.platform.models import PlatformActivityLog, PlatformAdmin
from apps.platform.serializers import issue_platform_access_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def creator():
    admin = PlatformAdmin.objects.create_user(email="creator@team.test", password="Test@1234", name="Creator")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(admin)}")
    return admin, client


def _sign_in(email, password):
    """Both steps of the Super Admin login; returns (status, verify-2fa body)."""
    client = APIClient()
    step1 = client.post("/platform/auth/login/", {"email": email, "password": password}, format="json")
    if step1.status_code != 200:
        return step1.status_code, step1.data
    step2 = client.post("/platform/auth/verify-2fa/", {"email": email, "code": step1.data["code"]}, format="json")
    return step2.status_code, step2.data


def _as(body):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    return client


def test_new_member_gets_a_temporary_password_once(creator):
    _, client = creator

    resp = client.post("/platform/team/", {"email": "new@team.test", "name": "New", "access_level": "SUPPORT_READONLY"}, format="json")

    assert resp.status_code == 201, resp.data
    temp = resp.data["temp_password"]
    assert isinstance(temp, str) and len(temp) >= 12
    assert resp.data["must_change_password"] is True and "password" not in resp.data
    assert resp.data["access_level"] == "SUPPORT_READONLY"
    status, body = _sign_in("new@team.test", temp)
    assert status == 200 and body["must_change_password"] is True and body["access"] and body["refresh"]
    # never shown again
    listed = client.get("/platform/team/").data
    rows = listed["results"] if isinstance(listed, dict) else listed
    member = next(r for r in rows if r["email"] == "new@team.test")
    assert "temp_password" not in member and member["must_change_password"] is True
    assert "temp_password" not in client.get(f"/platform/team/{member['id']}/").data
    assert PlatformActivityLog.objects.filter(action="TEAM_MEMBER_ADDED", description__contains="new@team.test").exists()


def test_a_password_still_sent_is_the_temporary_one(creator):
    """An older app still sends password: accepted, but the member must
    still set their own at first sign-in, and nothing is echoed back."""
    _, client = creator

    resp = client.post("/platform/team/", {"email": "legacy@team.test", "name": "Legacy", "password": "Typed@1234"}, format="json")

    assert resp.status_code == 201 and "temp_password" not in resp.data and resp.data["must_change_password"] is True
    status, body = _sign_in("legacy@team.test", "Typed@1234")
    assert status == 200 and body["must_change_password"] is True


def test_a_short_password_is_still_refused(creator):
    _, client = creator
    resp = client.post("/platform/team/", {"email": "short@team.test", "password": "short"}, format="json")
    assert resp.status_code == 400 and "password" in resp.data


def test_existing_admins_sign_in_without_the_flag(creator):
    status, body = _sign_in("creator@team.test", "Test@1234")
    assert status == 200 and body["must_change_password"] is False


def test_change_password_clears_the_flag(creator):
    _, client = creator
    temp = client.post("/platform/team/", {"email": "first@team.test", "name": "First"}, format="json").data["temp_password"]
    _, body = _sign_in("first@team.test", temp)
    member = _as(body)

    resp = member.post("/platform/auth/change-password/", {"current_password": temp, "new_password": "Mine@12345"}, format="json")

    assert resp.status_code == 200 and resp.data["detail"] == "Password changed." and resp.data["must_change_password"] is False
    assert _sign_in("first@team.test", temp)[0] == 400  # the temporary one is gone
    status, body = _sign_in("first@team.test", "Mine@12345")
    assert status == 200 and body["must_change_password"] is False


def test_change_password_ends_the_other_sessions_and_keeps_this_one(creator):
    """A temporary password is handed over by chat - whoever else signed in
    with it is signed out when the member sets their own."""
    import time

    _, client = creator
    temp = client.post("/platform/team/", {"email": "leak@team.test"}, format="json").data["temp_password"]
    _, other = _sign_in("leak@team.test", temp)  # someone else who saw it
    _, mine = _sign_in("leak@team.test", temp)
    time.sleep(1.1)  # token times are whole seconds

    changed = _as(mine).post("/platform/auth/change-password/", {"current_password": temp, "new_password": "Mine@12345"}, format="json")

    assert changed.status_code == 200 and changed.data["access"] and changed.data["refresh"]
    assert _as(other).get("/platform/team/").status_code == 401
    assert APIClient().post("/platform/auth/refresh/", {"refresh": other["refresh"]}, format="json").status_code == 400
    assert _as(changed.data).get("/platform/team/").status_code == 200  # the new pair works
    refreshed = APIClient().post("/platform/auth/refresh/", {"refresh": changed.data["refresh"]}, format="json")
    assert refreshed.status_code == 200 and refreshed.data["must_change_password"] is False


def test_refresh_reports_must_change_password(creator):
    _, client = creator
    temp = client.post("/platform/team/", {"email": "reopen@team.test"}, format="json").data["temp_password"]
    _, body = _sign_in("reopen@team.test", temp)

    refreshed = APIClient().post("/platform/auth/refresh/", {"refresh": body["refresh"]}, format="json")

    assert refreshed.status_code == 200 and refreshed.data["must_change_password"] is True and refreshed.data["access"]


def test_change_password_voluntarily_with_patch_too(creator):
    admin, client = creator
    resp = client.patch("/platform/auth/change-password/", {"current_password": "Test@1234", "new_password": "Newer@12345"}, format="json")
    assert resp.status_code == 200
    admin.refresh_from_db()
    assert admin.check_password("Newer@12345") and admin.must_change_password is False


@pytest.mark.parametrize("body, field", [
    ({"current_password": "Wrong@1234", "new_password": "Mine@12345"}, "current_password"),
    ({"current_password": "Test@1234", "new_password": "short"}, "new_password"),
    ({"current_password": "Test@1234", "new_password": "Test@1234"}, "new_password"),
    ({"current_password": "Test@1234", "new_password": "12345678"}, "new_password"),  # all digits / too common
    ({"current_password": "Test@1234", "new_password": "password"}, "new_password"),  # too common
    ({"new_password": "Mine@12345"}, "current_password"),
    ({"current_password": "Test@1234"}, "new_password"),
])
def test_change_password_errors(creator, body, field):
    admin, client = creator
    resp = client.post("/platform/auth/change-password/", body, format="json")
    assert resp.status_code == 400 and field in resp.data, resp.data
    admin.refresh_from_db()
    assert admin.check_password("Test@1234")


def test_change_password_needs_a_super_admin_token(api_client):
    assert api_client.post("/platform/auth/change-password/", {"current_password": "a", "new_password": "Mine@12345"}, format="json").status_code in (401, 403)


def test_a_read_only_member_can_change_their_own_password(creator):
    _, client = creator
    temp = client.post("/platform/team/", {"email": "ro@team.test", "access_level": "SUPPORT_READONLY"}, format="json").data["temp_password"]
    _, body = _sign_in("ro@team.test", temp)
    assert _as(body).post("/platform/auth/change-password/", {"current_password": temp, "new_password": "Mine@12345"}, format="json").status_code == 200


def test_regenerate_while_still_on_the_temporary_password(creator):
    _, client = creator
    created = client.post("/platform/team/", {"email": "lost@team.test"}, format="json").data

    resp = client.post(f"/platform/team/{created['id']}/regenerate-temp-password/")

    assert resp.status_code == 200 and resp.data["temp_password"] != created["temp_password"]
    assert resp.data["must_change_password"] is True
    assert _sign_in("lost@team.test", created["temp_password"])[0] == 400
    assert _sign_in("lost@team.test", resp.data["temp_password"])[0] == 200
    assert PlatformActivityLog.objects.filter(action="TEAM_PASSWORD_REGENERATED").count() == 1


def test_regenerate_ends_the_sessions_opened_with_the_old_one(creator):
    import time

    _, client = creator
    created = client.post("/platform/team/", {"email": "leaked@team.test"}, format="json").data
    _, leaked = _sign_in("leaked@team.test", created["temp_password"])
    time.sleep(1.1)

    assert client.post(f"/platform/team/{created['id']}/regenerate-temp-password/").status_code == 200

    assert _as(leaked).get("/platform/team/").status_code == 401
    assert APIClient().post("/platform/auth/refresh/", {"refresh": leaked["refresh"]}, format="json").status_code == 400


def test_regenerate_refuses_your_own_account_and_a_deactivated_member(creator):
    admin, client = creator
    own = client.post(f"/platform/team/{admin.id}/regenerate-temp-password/")
    assert own.status_code == 400 and own.data == {"detail": "Use POST /platform/auth/change-password/ for your own password."}

    created = client.post("/platform/team/", {"email": "gone@team.test"}, format="json").data
    client.patch(f"/platform/team/{created['id']}/", {"is_active": False}, format="json")
    gone = client.post(f"/platform/team/{created['id']}/regenerate-temp-password/")
    assert gone.status_code == 409 and gone.data == {"detail": "This team member is deactivated."}


def test_regenerate_unknown_member_is_404(creator):
    _, client = creator
    assert client.post("/platform/team/00000000-0000-0000-0000-000000000000/regenerate-temp-password/").status_code == 404


@pytest.mark.parametrize("call", [
    lambda c, other: c.post("/platform/team/", {"email": "esc@team.test", "access_level": "FULL_ADMIN"}, format="json"),
    lambda c, other: c.post(f"/platform/team/{other.id}/regenerate-temp-password/"),
    lambda c, other: c.patch(f"/platform/team/{other.id}/", {"is_active": False}, format="json"),
    lambda c, other: c.delete(f"/platform/team/{other.id}/"),
])
def test_a_read_only_member_cannot_manage_the_team(creator, call):
    """Team management hands out passwords now - Full Admins only."""
    readonly = PlatformAdmin.objects.create_user(email="viewer@team.test", password="Test@1234", access_level="SUPPORT_READONLY")
    other = PlatformAdmin.objects.create_user(email="target@team.test", password="Test@1234")
    other.must_change_password = True
    other.save(update_fields=["must_change_password"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(readonly)}")

    resp = call(client, other)

    assert resp.status_code == 403 and resp.data == {"detail": "Only a Full Admin can do this."}
    assert client.get("/platform/team/").status_code == 200  # viewing is still fine
    assert not PlatformAdmin.objects.filter(email="esc@team.test").exists()
    other.refresh_from_db()
    assert other.is_active and other.check_password("Test@1234")


def test_regenerate_refused_once_they_set_their_own(creator):
    _, client = creator
    created = client.post("/platform/team/", {"email": "set@team.test"}, format="json").data
    _, body = _sign_in("set@team.test", created["temp_password"])
    _as(body).post("/platform/auth/change-password/", {"current_password": created["temp_password"], "new_password": "Mine@12345"}, format="json")

    resp = client.post(f"/platform/team/{created['id']}/regenerate-temp-password/")

    assert resp.status_code == 409 and resp.data == {"detail": "This team member has already set their own password."}
    assert _sign_in("set@team.test", "Mine@12345")[0] == 200



# ---- second review (2026-10-13): what a password change must also end ---------

def test_a_2fa_code_from_before_a_regenerate_cannot_finish_the_sign_in(creator):
    """While email is off the code is always 123456 and echoed - a sign-in
    started with the leaked temporary password must not finish after it."""
    _, client = creator
    created = client.post("/platform/team/", {"email": "code@team.test"}, format="json").data
    step1 = APIClient().post("/platform/auth/login/", {"email": "code@team.test", "password": created["temp_password"]}, format="json")

    client.post(f"/platform/team/{created['id']}/regenerate-temp-password/")
    late = APIClient().post("/platform/auth/verify-2fa/", {"email": "code@team.test", "code": step1.data["code"]}, format="json")

    assert late.status_code == 400


def test_a_token_from_the_same_second_as_the_change_is_refused(creator):
    """Exact: the password version, not whole-second token times."""
    _, client = creator
    temp = client.post("/platform/team/", {"email": "fast@team.test"}, format="json").data["temp_password"]
    _, other = _sign_in("fast@team.test", temp)
    _, mine = _sign_in("fast@team.test", temp)

    changed = _as(mine).post("/platform/auth/change-password/", {"current_password": temp, "new_password": "Mine@12345"}, format="json")

    assert changed.status_code == 200
    refused = _as(other).get("/platform/team/")
    assert refused.status_code == 401 and refused.data["detail"] == "The password was changed. Sign in again."
    assert _as(changed.data).get("/platform/team/").status_code == 200


def test_existing_tokens_without_a_password_version_keep_working():
    """Tokens issued before this release carry no version - the two live
    Super Admins must not be signed out by the deploy."""
    from rest_framework_simplejwt.tokens import AccessToken

    admin = PlatformAdmin.objects.create_user(email="old@team.test", password="Test@1234")
    token = AccessToken.for_user(admin)
    token["platform_admin"] = True
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert client.get("/platform/team/").status_code == 200


def test_a_password_change_ends_support_access_sessions(creator, restaurant, admin_client):
    _, client = creator
    temp = client.post("/platform/team/", {"email": "support@team.test"}, format="json").data["temp_password"]
    _, body = _sign_in("support@team.test", temp)
    member = _as(body)
    started = member.post(f"/platform/tenants/{restaurant.id}/impersonate/")
    assert started.status_code == 201, started.data
    tenant = APIClient()
    tenant.credentials(HTTP_AUTHORIZATION=f"Bearer {started.data['access']}")
    assert tenant.get("/v1/auth/me/").status_code == 200

    member.post("/platform/auth/change-password/", {"current_password": temp, "new_password": "Mine@12345"}, format="json")

    assert tenant.get("/v1/auth/me/").status_code == 401


def test_the_last_full_admin_cannot_be_demoted_deactivated_or_removed(creator):
    admin, client = creator  # the only Full Admin
    for body in ({"access_level": "SUPPORT_READONLY"}, {"is_active": False}):
        resp = client.patch(f"/platform/team/{admin.id}/", body, format="json")
        assert resp.status_code == 409 and resp.data == {"detail": "At least one active Full Admin must remain."}
    assert client.delete(f"/platform/team/{admin.id}/").status_code == 409
    admin.refresh_from_db()
    assert admin.is_active and admin.access_level == "FULL_ADMIN"
    # with a second Full Admin it is allowed
    PlatformAdmin.objects.create_user(email="second@team.test", password="Test@1234")
    assert client.patch(f"/platform/team/{admin.id}/", {"name": "Still me"}, format="json").status_code == 200


def test_an_unknown_access_level_is_refused(creator):
    _, client = creator
    other = PlatformAdmin.objects.create_user(email="lvl@team.test", password="Test@1234")
    resp = client.patch(f"/platform/team/{other.id}/", {"access_level": "full_admin"}, format="json")
    assert resp.status_code == 400 and "access_level" in resp.data
    other.refresh_from_db()
    assert other.access_level == "FULL_ADMIN"


def test_a_password_too_close_to_the_name_is_refused():
    admin = PlatformAdmin.objects.create_user(email="named@team.test", password="Test@1234", name="Shereena Thomas")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(admin)}")
    resp = client.post("/platform/auth/change-password/", {"current_password": "Test@1234", "new_password": "ShereenaThomas"}, format="json")
    assert resp.status_code == 400 and "new_password" in resp.data
