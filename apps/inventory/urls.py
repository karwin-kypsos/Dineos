from rest_framework.routers import DefaultRouter

from .views import IngredientViewSet, PurchaseOrderViewSet, RecipeItemViewSet

router = DefaultRouter()
router.register("ingredients", IngredientViewSet, basename="ingredient")
router.register("purchase-orders", PurchaseOrderViewSet, basename="purchase-order")
router.register("recipe-items", RecipeItemViewSet, basename="recipe-item")

urlpatterns = router.urls
