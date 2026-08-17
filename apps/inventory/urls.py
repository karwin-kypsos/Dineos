from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AIInsightViewSet, IngredientViewSet, PurchaseOrderViewSet, RecipeItemViewSet, WastageLogView

router = DefaultRouter()
router.register("ingredients", IngredientViewSet, basename="ingredient")
router.register("purchase-orders", PurchaseOrderViewSet, basename="purchase-order")
router.register("recipe-items", RecipeItemViewSet, basename="recipe-item")
router.register("ai-insights", AIInsightViewSet, basename="ai-insight")

urlpatterns = [
    path("wastage/", WastageLogView.as_view(), name="wastage-log"),
] + router.urls
