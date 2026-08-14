import pytest

from apps.menu.models import PreparedPortion

pytestmark = pytest.mark.django_db


def test_customer_menu_hides_zero_portion_items(api_client, table, menu_item):
    portion = menu_item.prepared_portions.get()
    portion.portions_remaining = 0
    portion.save()

    response = api_client.get(f"/v1/menu/customer/{table.id}/")
    assert response.status_code == 200
    ids = [item["id"] for item in response.data]
    assert menu_item.id not in ids


def test_untracked_item_always_shows(api_client, table, menu_item):
    # A second item with no PreparedPortion row today is always available.
    from apps.menu.models import MenuItem

    untracked = MenuItem.objects.create(category=menu_item.category, name="Fries", price=80)

    response = api_client.get(f"/v1/menu/customer/{table.id}/")
    ids = [item["id"] for item in response.data]
    assert untracked.id in ids


def test_add_portions_increments_existing_row(manager_client, menu_item):
    _, client = manager_client
    portion = menu_item.prepared_portions.get()
    starting = portion.portions_remaining

    response = client.patch(f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 5}, format="json")
    assert response.status_code == 200

    portion.refresh_from_db()
    assert portion.portions_remaining == starting + 5


def test_manager_can_update_menu_item(manager_client, menu_item):
    _, client = manager_client

    response = client.put(
        f"/v1/menu/{menu_item.id}/",
        {"category": menu_item.category_id, "name": "Chicken Biryani (Large)", "price": "260.00"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["name"] == "Chicken Biryani (Large)"

    menu_item.refresh_from_db()
    assert str(menu_item.price) == "260.00"


def test_menu_items_within_a_category_are_ordered_by_sort_order_then_name(manager_client, menu_item):
    from apps.menu.models import MenuItem

    _, client = manager_client
    # menu_item ("Chicken Biryani") defaults to sort_order=0; give it a
    # higher number so an alphabetically-earlier item can still be forced
    # to the front — proves ordering isn't falling back to pure alpha sort.
    menu_item.sort_order = 5
    menu_item.save()
    zebra = MenuItem.objects.create(category=menu_item.category, name="Zebra Special", price=99, sort_order=1)

    response = client.get("/v1/menu/all/")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    names_in_this_category = [
        item["name"] for item in results if item["category"] == menu_item.category_id
    ]
    assert names_in_this_category.index(zebra.name) < names_in_this_category.index(menu_item.name)


def test_manager_can_update_menu_item_sort_order(manager_client, menu_item):
    _, client = manager_client

    response = client.patch(f"/v1/menu/{menu_item.id}/", {"sort_order": 3}, format="json")
    assert response.status_code == 200
    assert response.data["sort_order"] == 3

    menu_item.refresh_from_db()
    assert menu_item.sort_order == 3


def test_manager_can_delete_menu_item(manager_client, menu_item):
    from apps.menu.models import MenuItem

    _, client = manager_client

    response = client.delete(f"/v1/menu/{menu_item.id}/")
    assert response.status_code == 204
    assert not MenuItem.objects.filter(id=menu_item.id).exists()


def test_manager_can_update_category(manager_client, menu_item):
    _, client = manager_client
    category = menu_item.category

    response = client.put(
        f"/v1/menu/categories/{category.id}/", {"name": "Main Course (Updated)"}, format="json"
    )
    assert response.status_code == 200

    category.refresh_from_db()
    assert category.name == "Main Course (Updated)"


def test_renaming_category_to_a_name_already_in_use_returns_400(manager_client, menu_item, restaurant):
    from apps.menu.models import Category

    _, client = manager_client
    other_category = Category.objects.create(restaurant=restaurant, name="Desserts")

    response = client.put(
        f"/v1/menu/categories/{other_category.id}/", {"name": menu_item.category.name}, format="json"
    )
    assert response.status_code == 400


def test_deleting_category_with_items_is_blocked(manager_client, menu_item):
    _, client = manager_client
    category = menu_item.category

    response = client.delete(f"/v1/menu/categories/{category.id}/")
    assert response.status_code == 409


def test_deleting_empty_category_succeeds(manager_client, restaurant):
    from apps.menu.models import Category

    _, client = manager_client
    empty_category = Category.objects.create(restaurant=restaurant, name="Empty Category")

    response = client.delete(f"/v1/menu/categories/{empty_category.id}/")
    assert response.status_code == 204
    assert not Category.objects.filter(id=empty_category.id).exists()


def test_list_menu_filter_by_category(manager_client, menu_item):
    from apps.menu.models import Category, MenuItem

    _, client = manager_client
    other_category = Category.objects.create(restaurant=menu_item.category.restaurant, name="Drinks")
    MenuItem.objects.create(category=other_category, name="Lemonade", price=60)

    response = client.get(f"/v1/menu/all/?category={menu_item.category_id}")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    names = {item["name"] for item in results}
    assert names == {menu_item.name}


def test_list_menu_search_by_name(manager_client, menu_item):
    _, client = manager_client

    response = client.get("/v1/menu/all/?search=biryani")
    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert {item["name"] for item in results} == {menu_item.name}

    response = client.get("/v1/menu/all/?search=nonexistent-dish")
    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert results == []


def test_create_menu_item_with_recipe_items(manager_client, menu_item):
    from decimal import Decimal

    from apps.inventory.models import Ingredient, RecipeItem

    _, client = manager_client
    restaurant = menu_item.category.restaurant
    ginger = Ingredient.objects.create(restaurant=restaurant, name="Ginger", unit="G")
    chicken = Ingredient.objects.create(restaurant=restaurant, name="Chicken", unit="KG")

    response = client.post(
        "/v1/menu/",
        {
            "category": menu_item.category_id,
            "name": "Paneer Tikka",
            "price": "180.00",
            "description": "Grilled paneer",
            "recipe_items": [
                {"ingredient": str(ginger.id), "quantity_per_serving": "0.010"},
                {"ingredient": str(chicken.id), "quantity_per_serving": "0.250"},
            ],
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert len(response.data["recipe"]) == 2
    units = {line["unit"] for line in response.data["recipe"]}
    assert units == {"G", "KG"}
    assert RecipeItem.objects.filter(menu_item_id=response.data["id"]).count() == 2
    ginger_line = RecipeItem.objects.get(menu_item_id=response.data["id"], ingredient=ginger)
    assert ginger_line.quantity_per_serving == Decimal("0.010")


def test_create_menu_item_rejects_duplicate_ingredient_in_recipe(manager_client, menu_item):
    from apps.inventory.models import Ingredient

    _, client = manager_client
    chicken = Ingredient.objects.create(restaurant=menu_item.category.restaurant, name="Chicken", unit="KG")

    response = client.post(
        "/v1/menu/",
        {
            "category": menu_item.category_id,
            "name": "Chicken 65",
            "price": "150.00",
            "recipe_items": [
                {"ingredient": str(chicken.id), "quantity_per_serving": "0.100"},
                {"ingredient": str(chicken.id), "quantity_per_serving": "0.050"},
            ],
        },
        format="json",
    )

    assert response.status_code == 400


def test_create_menu_item_rejects_ingredient_from_another_restaurant(manager_client, menu_item):
    from apps.inventory.models import Ingredient
    from apps.restaurant.models import Restaurant

    _, client = manager_client
    foreign_restaurant = Restaurant.objects.create(name="Foreign Kitchen", slug="foreign-kitchen-menu")
    foreign_ingredient = Ingredient.objects.create(restaurant=foreign_restaurant, name="Butter", unit="KG")

    response = client.post(
        "/v1/menu/",
        {
            "category": menu_item.category_id,
            "name": "Butter Naan",
            "price": "60.00",
            "recipe_items": [{"ingredient": str(foreign_ingredient.id), "quantity_per_serving": "0.020"}],
        },
        format="json",
    )

    assert response.status_code == 400


def test_update_menu_item_replaces_recipe_items(manager_client, menu_item):
    from apps.inventory.models import Ingredient, RecipeItem

    _, client = manager_client
    restaurant = menu_item.category.restaurant
    ginger = Ingredient.objects.create(restaurant=restaurant, name="Ginger", unit="G")
    RecipeItem.objects.create(menu_item=menu_item, ingredient=ginger, quantity_per_serving="0.005")
    garlic = Ingredient.objects.create(restaurant=restaurant, name="Garlic", unit="G")

    response = client.put(
        f"/v1/menu/{menu_item.id}/",
        {
            "category": menu_item.category_id,
            "name": menu_item.name,
            "price": "220.00",
            "recipe_items": [{"ingredient": str(garlic.id), "quantity_per_serving": "0.008"}],
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    assert RecipeItem.objects.filter(menu_item=menu_item).count() == 1
    assert RecipeItem.objects.get(menu_item=menu_item).ingredient == garlic
