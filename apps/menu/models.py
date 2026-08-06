from django.db import models
from django.utils import timezone


class Category(models.Model):
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="menu_categories")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="menu_categories"
    )
    name = models.CharField(max_length=100)
    emoji = models.CharField(max_length=8, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "menu_categories"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"], condition=models.Q(branch__isnull=True),
                name="one_category_name_per_restaurant_legacy",
            ),
            models.UniqueConstraint(
                fields=["branch", "name"], condition=models.Q(branch__isnull=False),
                name="one_category_name_per_branch",
            ),
        ]

    def __str__(self):
        return self.name


class MenuItem(models.Model):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image_url = models.URLField(blank=True)
    is_veg = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "menu_items"
        ordering = ["category__sort_order", "name"]

    def __str__(self):
        return self.name


class PreparedPortion(models.Model):
    """Phase-1 stand-in for the Daily Prep Log: a manually settable counter
    of how many portions of a dish are available today. Phase 2 extends this
    with recipe-driven raw-ingredient deduction — schema stays stable, only
    the mutation logic in decrement_portions()/add_portions() changes.
    """

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="prepared_portions")
    date = models.DateField(default=timezone.localdate)
    portions_initial = models.PositiveIntegerField()
    portions_remaining = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prepared_portions"
        constraints = [models.UniqueConstraint(fields=["menu_item", "date"], name="one_portion_row_per_item_per_day")]

    def __str__(self):
        return f"{self.menu_item.name} — {self.portions_remaining}/{self.portions_initial} ({self.date})"
