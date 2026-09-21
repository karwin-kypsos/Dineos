"""Staff broadcast groups — audience in, non-overlapping group names out.

A staff socket joins one Channels group per role it is allowed to hear
(see StaffConsumer._role_groups), and MANAGER/ADMIN deliberately join
several: a Manager is in servers_, cashiers_ AND managers_. So naming two
of those groups in a single broadcast sends the same event TWICE to every
Manager and Admin connected — confirmed live on production 2026-09-21,
where a Manager's socket received one payment_confirmed frame per group
while a Cashier received one. Any screen that counts or appends per event
double-counts, for exactly the two roles that watch the money.

Callers therefore name the audience they want ("servers", "cashiers",
"managers") and this module returns the group names that reach exactly
those people, one copy each.
"""

# Which roles are subscribed to each staff group — the inverse of
# StaffConsumer._role_groups, and must be kept in step with it.
ROLES_IN_GROUP = {
    "staff_all": frozenset({"SERVER", "CASHIER", "MANAGER", "ADMIN"}),
    "servers": frozenset({"SERVER", "MANAGER", "ADMIN"}),
    "cashiers": frozenset({"CASHIER", "MANAGER", "ADMIN"}),
    "managers": frozenset({"MANAGER", "ADMIN"}),
}

# Widest first, so an audience that a single wide group covers exactly
# (servers + cashiers == everyone == staff_all) collapses to that one
# group instead of being rebuilt out of overlapping narrow ones.
_PREFERENCE = ("staff_all", "servers", "cashiers", "managers")


def staff_groups(restaurant_id, audience):
    """Group names reaching exactly `audience`, with no socket hit twice.

    `audience` is any iterable of the keys in ROLES_IN_GROUP. Groups are
    only ever chosen when they are a subset of the requested audience and
    disjoint from what is already covered, so this can never widen who
    hears an event, and never delivers twice. If no exact cover exists
    the requested groups are returned unchanged — a duplicate frame is a
    nuisance, a dropped one is a bug.
    """
    names = list(dict.fromkeys(audience))
    for name in names:
        if name not in ROLES_IN_GROUP:
            raise ValueError(f"unknown staff group {name!r}")

    wanted = frozenset().union(*(ROLES_IN_GROUP[n] for n in names)) if names else frozenset()

    covered, chosen = set(), []
    for name in _PREFERENCE:
        roles = ROLES_IN_GROUP[name]
        if roles <= wanted and not (roles & covered):
            chosen.append(name)
            covered |= roles

    if covered != wanted:
        chosen = names

    return [f"{name}_{restaurant_id}" for name in chosen]
