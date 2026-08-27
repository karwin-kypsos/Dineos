import uuid

from django.db import models


class Feedback(models.Model):
    """Post-payment 'Rate your experience' screen (2026-08-27, per
    Shereena's star-rating mockup) — one per Bill, submitted by the
    customer with no auth required. Tied to the Bill rather than the
    session/order directly since Bill is the one thing both dine-in and
    takeaway share once a transaction is actually complete.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="feedback")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="feedback"
    )
    bill = models.OneToOneField("billing.Bill", on_delete=models.CASCADE, related_name="feedback")
    rating = models.PositiveSmallIntegerField()
    comment = models.CharField(max_length=140, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "feedback"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(rating__gte=1) & models.Q(rating__lte=5), name="feedback_rating_range_1_to_5"
            ),
        ]

    def __str__(self):
        return f"Feedback {self.id} — {self.rating} stars"
