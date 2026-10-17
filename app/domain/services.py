from collections.abc import Iterable

from app.domain.models import Coordinates, Warehouse


def rank_warehouses_by_distance(
    warehouses: Iterable[Warehouse], destination: Coordinates
) -> list[Warehouse]:
    """The warehouses ordered from the closest to the farthest from the destination.

    Warehouses at the same distance are ordered by id, so the choice is always the same.
    """
    return sorted(
        warehouses,
        key=lambda warehouse: (warehouse.location.distance_km_to(destination), warehouse.id),
    )
