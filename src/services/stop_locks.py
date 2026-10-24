"""Place identity and locks shared by direct edits and conversational revisions."""

from __future__ import annotations

from src.models import DayPlanItem, HotelOption, PlaceTarget, TripPlan


def same_place(name: str, address: str, other_name: str, other_address: str) -> bool:
    return (
        name.strip() == other_name.strip() and address.strip() == other_address.strip()
    )


def resolve_target(plan: TripPlan, target: PlaceTarget) -> DayPlanItem | HotelOption:
    if target.day_index > len(plan.daily_plans):
        raise ValueError("这个地点所属日期已变化，请重新打开行程。")
    if target.kind == "hotel":
        stay = plan.daily_stays[target.day_index - 1]
        choices = [hotel for hotel in (stay.start_hotel, stay.end_hotel) if hotel]
        for hotel in choices:
            if same_place(
                hotel.name, hotel.location.address, target.name, target.address
            ):
                return hotel
    else:
        matches = [
            item
            for item in plan.daily_plans[target.day_index - 1].items
            if (
                item.item_type == target.kind
                or target.kind == "meal"
                and item.item_type == "food"
            )
            and same_place(
                item.location_name or item.title,
                item.location_address,
                target.name,
                target.address,
            )
            and (target.kind != "meal" or item.meal_type == target.meal_type)
        ]
        if len(matches) == 1:
            return matches[0]
    raise ValueError("原地点已变化或无法唯一识别，请重新打开候选列表；当前行程已保留。")


def hotel_days(plan: TripPlan, target: PlaceTarget) -> list[int]:
    return [
        stay.day_index
        for stay in plan.daily_stays
        if any(
            hotel
            and same_place(
                hotel.name, hotel.location.address, target.name, target.address
            )
            for hotel in (stay.start_hotel, stay.end_hotel)
        )
    ]


def is_locked(plan: TripPlan, target: PlaceTarget) -> bool:
    return any(
        lock.kind == target.kind
        and same_place(lock.name, lock.address, target.name, target.address)
        and (
            target.kind == "hotel"
            or lock.day_index == target.day_index
            and lock.meal_type == target.meal_type
        )
        for lock in plan.place_locks
    )


def validate_locks(plan: TripPlan) -> None:
    for lock in plan.place_locks:
        resolve_target(plan, lock)


def assert_locks_preserved(before: TripPlan, after: TripPlan) -> None:
    for lock in before.place_locks:
        try:
            days = (
                hotel_days(before, lock) if lock.kind == "hotel" else [lock.day_index]
            )
            for index in days:
                resolve_target(after, lock.model_copy(update={"day_index": index}))
        except ValueError as exc:
            raise ValueError(
                f"「{lock.name}」已锁定，本次调整需要更换它。请先解锁或换一种调整方式，原行程已保留。"
            ) from exc
