import enum


def enum_values(enum_class: type[enum.StrEnum]) -> list[str]:
    """Values for SQLAlchemy ``Enum(values_callable=...)``."""
    return [str(member) for member in enum_class]
