import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models.functions import Lower
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.restaurant.models import Branch, Restaurant
from apps.restaurant.serializers import BranchSummarySerializer

User = get_user_model()

# Role → (numeric id, external role-name slug). The Postman collection, mobile
# clients, and any external consumers that want a stable numeric role identifier
# or a lowercase slug read from these. The DB still stores the enum string
# (User.Role.ADMIN etc.), so this mapping is a projection layer only.
ROLE_METADATA = {
    "ADMIN":   {"id": 1, "name": "org_admin"},
    "MANAGER": {"id": 2, "name": "manager"},
    "SERVER":  {"id": 3, "name": "server"},
    "CASHIER": {"id": 4, "name": "cashier"},
}


class TenantSummarySerializer(serializers.ModelSerializer):
    """The subset of a tenant's record staff clients need to render a
    feature-flag-aware UI — not the full platform-management view
    (see apps.platform.serializers.RestaurantSerializer for that).
    """

    plan_tier_display = serializers.CharField(source="get_plan_tier_display", read_only=True)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "plan_tier",
            "plan_tier_display",
            "max_branches",
            "notifications_enabled",
            "kitchen_enabled",
            "billing_enabled",
            "realtime_enabled",
            "customer_ordering_enabled",
            "server_staff_enabled",
        ]


# Roles that work inside one branch; Admin is the only one without.
BRANCH_ROLES = {User.Role.MANAGER, User.Role.SERVER, User.Role.CASHIER}


def refuse_server_role_without_the_flag(serializer, role):
    """server_staff_enabled (2026-09-30, per Shereena): a restaurant with
    the flag off can't add a Server account or turn someone into one.
    Existing Server accounts are left alone - the flag gates adding them -
    so editing a current Server's other details still works."""
    if role != User.Role.SERVER:
        return role
    request = serializer.context.get("request")
    restaurant = getattr(getattr(request, "user", None), "restaurant", None)
    already_server = serializer.instance is not None and serializer.instance.role == User.Role.SERVER
    if restaurant is not None and not restaurant.server_staff_enabled and not already_server:
        raise serializers.ValidationError("Server staff accounts are not enabled for this restaurant.")
    return role


def resolve_branch_context(user):
    """Returns (branch_data, available_branches_data) for the branch
    switcher. Manager/Server/Cashier are pinned to their own user.branch —
    available_branches is None for them (nothing to switch between).
    Admin has no fixed branch, so this resolves to: their last-selected
    branch (user.selected_branch, persisted so it survives the login being
    skipped on later app opens) -> else the restaurant's first active
    branch by name -> else None if the restaurant has no branches yet.
    """
    if user.branch is not None:
        return BranchSummarySerializer(user.branch).data, None

    branches = list(Branch.objects.filter(restaurant_id=user.restaurant_id, is_active=True).order_by(Lower("name"), "id"))
    selected = user.selected_branch if (user.selected_branch and user.selected_branch.is_active) else None
    if selected is None and branches:
        selected = branches[0]

    branch_data = BranchSummarySerializer(selected).data if selected else None
    available_data = BranchSummarySerializer(branches, many=True).data
    return branch_data, available_data


class DineOSTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["name"] = user.name
        token["restaurant_id"] = str(user.restaurant_id)
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        if self.user.restaurant.status == Restaurant.Status.SUSPENDED:
            raise serializers.ValidationError("This organization's account has been suspended.")
        # 2026-09-21, per Karwin: the login response carried role, role_id
        # (the ROLE's id, not the person's) and name — nothing stable to
        # compare against assigned_server_id, so the app had no way to ask
        # "is this one mine". The id was already reachable, via the JWT's
        # own user_id claim and GET /v1/auth/me/, but making the frontend
        # decode a token or spend a second round trip for its own identity
        # is the wrong ergonomics. Same UUID as both of those.
        data["user_id"] = str(self.user.id)
        data["role"] = self.user.role
        data["role_id"] = ROLE_METADATA[self.user.role]["id"]
        data["role_name"] = ROLE_METADATA[self.user.role]["name"]
        data["name"] = self.user.name
        data["restaurant_id"] = str(self.user.restaurant_id)
        data["must_change_password"] = self.user.must_change_password
        branch_data, available_data = resolve_branch_context(self.user)
        data["branch"] = branch_data
        data["available_branches"] = available_data
        return data


class UserSerializer(serializers.ModelSerializer):
    """General-purpose staff representation — List/Get/Create/Update/
    Deactivate Staff. branch here is just the record's own fixed FK
    (cheap, no query beyond the normal join). Do NOT add the branch
    switcher's resolve_branch_context()/available_branches here: that's
    the CALLER's own identity context, not a property of an arbitrary
    staff record, and computing it per row would run an extra "list every
    active branch" query for every Admin-role row returned by List Staff.
    See MeSerializer below for where that enrichment actually belongs.
    """

    restaurant = TenantSummarySerializer(read_only=True)
    branch = BranchSummarySerializer(read_only=True)
    role_id = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "name", "phone", "address", "role", "role_id", "role_name",
                  "is_active", "must_change_password", "restaurant", "branch", "created_at"]
        read_only_fields = ["id", "must_change_password", "created_at"]

    def get_role_id(self, obj):
        return ROLE_METADATA[obj.role]["id"]

    def get_role_name(self, obj):
        return ROLE_METADATA[obj.role]["name"]

    def validate_role(self, role):
        return refuse_server_role_without_the_flag(self, role)


class MeSerializer(UserSerializer):
    """GET /v1/auth/me/ only — adds the branch switcher's resolved branch
    + available_branches for the CALLING user, same shape as the login
    response. Kept off the base UserSerializer so List/Get/Update Staff
    don't pay for or return this for every other staff record."""

    branch = serializers.SerializerMethodField()
    available_branches = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["available_branches"]

    def get_branch(self, obj):
        return resolve_branch_context(obj)[0]

    def get_available_branches(self, obj):
        return resolve_branch_context(obj)[1]


class UserCreateSerializer(serializers.ModelSerializer):
    # Omit password to invite the new staff member instead — they set their
    # own password via the emailed link (POST /v1/auth/reset-password/
    # with the token). Only supplied directly in tests/legacy flows.
    password = serializers.CharField(write_only=True, required=False)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)

    class Meta:
        model = User
        fields = ["id", "email", "name", "phone", "address", "role", "password", "branch"]

    def validate_branch(self, branch):
        request = self.context.get("request")
        if branch is not None and request is not None and branch.restaurant_id != request.user.restaurant_id:
            raise serializers.ValidationError("Branch does not belong to your restaurant.")
        return branch

    def validate_role(self, role):
        return refuse_server_role_without_the_flag(self, role)

    def validate(self, attrs):
        # 2026-09-30, per Shereena: a Manager, Server or Cashier is created
        # into a branch, never without one. Every branch check in the API
        # treats "no branch" as "not pinned", so such an account fell
        # through all of them and got whole-restaurant reach like an Admin.
        # An Admin has no branch by design, so it stays optional for them.
        if attrs.get("role") in BRANCH_ROLES and attrs.get("branch") is None:
            raise serializers.ValidationError(
                {"branch": ["This field is required for Manager, Server and Cashier accounts."]}
            )
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        return User.objects.create_user(password=password, **validated_data)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


# ---- Admin Self-Registration (2026-09-29) --------------------------------------
# A restaurant signs itself up instead of a Super Admin creating it:
# Get Plans -> Register Restaurant -> Signup (set the Admin's login) -> Login.

# Same phone shape the takeaway order form accepts.
PHONE_PATTERN = r"^[0-9+][0-9 ()\-]{6,20}$"
# GSTIN: 2-digit state code, 10-character PAN, entity number, "Z", checksum.
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# PAN: 5 letters, 4 digits, 1 letter (2026-10-01, per Shereena).
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def clean_pan_number(value):
    """Optional, but a PAN that is given must look like one. Shared with the
    Super Admin's organization form (apps.platform.serializers)."""
    value = value.strip().upper()
    if value and not PAN_PATTERN.match(value):
        raise serializers.ValidationError("Enter a valid 10-character PAN, e.g. ABCDE1234F.")
    return value


class RegisterRestaurantSerializer(serializers.Serializer):
    restaurant_name = serializers.CharField(max_length=255)
    contact_name = serializers.CharField(max_length=255)
    contact_phone = serializers.RegexField(
        PHONE_PATTERN, max_length=32, error_messages={"invalid": "Enter a valid phone number."},
    )
    contact_email = serializers.EmailField()
    # Where invoices go; the contact email when left blank.
    billing_email = serializers.EmailField(required=False, allow_blank=True, default="")
    # Optional - plenty of small restaurants aren't GST-registered - but a
    # value that is given must look like a real GSTIN.
    gst_number = serializers.CharField(required=False, allow_blank=True, default="", max_length=15)
    # Both optional, like the GSTIN (2026-10-01, per Shereena). The
    # registration number is free text: CIN, LLPIN, Udyam, shop licence...
    pan_number = serializers.CharField(required=False, allow_blank=True, default="", max_length=10)
    business_registration_number = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=50,
    )
    service_charge_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False, default=Decimal("0.00"),
    )
    plan_id = serializers.ChoiceField(choices=Restaurant.PlanTier.values)

    def validate_gst_number(self, value):
        value = value.strip().upper()
        if value and not GSTIN_PATTERN.match(value):
            raise serializers.ValidationError("Enter a valid 15-character GSTIN, e.g. 27ABCDE1234F1Z5.")
        return value

    def validate_pan_number(self, value):
        return clean_pan_number(value)


class SelfSignupSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8)
    password_confirm = serializers.CharField()

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": ["Passwords do not match."]})
        return attrs
