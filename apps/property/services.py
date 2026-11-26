from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.property.models import Folio, Guest, Reservation, Room, RoomType


def _validate_stay_dates(check_in: date, check_out: date) -> None:
    if check_out <= check_in:
        raise ValidationError("Check-out date must be after check-in date.")


@transaction.atomic
def create_reservation(
    *,
    guest: Guest,
    room_type: RoomType,
    room: Room,
    check_in: date,
    check_out: date,
    open_folio: bool = True,
    **kwargs,
) -> Reservation:
    _validate_stay_dates(check_in, check_out)

    # 1. Serialize concurrent bookings on the target room
    locked_room = Room.objects.select_for_update().get(pk=room.pk)

    if locked_room.room_type != room_type:
        raise ValidationError("Selected room does not match the chosen room type.")

    # 2. Prevent overlapping active reservations in service layer
    overlapping = Reservation.objects.filter(
        room=locked_room,
        status__in=Reservation.ACTIVE_STATUSES,
        check_in__lt=check_out,
        check_out__gt=check_in,
    ).exists()

    if overlapping:
        raise ValidationError(f"Room {locked_room.number} is already booked for those dates.")

    # 3. Create reservation (and folio if needed) atomically
    reservation = Reservation.objects.create(
        guest=guest,
        room_type=room_type,
        room=locked_room,
        check_in=check_in,
        check_out=check_out,
        **kwargs,
    )

    if open_folio:
        Folio.objects.get_or_create(reservation=reservation)

    return reservation