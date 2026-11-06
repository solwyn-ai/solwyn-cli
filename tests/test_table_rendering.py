"""Table rendering: sub-cent amounts (finding 5) and width (finding 9) from the 0.5.0 run-through."""

import pytest

from solwyn_cli.money import money


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (0, "0.00"),
        (-0.0, "0.00"),
        (100, "100.00"),
        (1.5, "1.50"),
        (0.01, "0.01"),
        (0.002, "0.002"),
        (0.000519, "0.000519"),
        (12.345, "12.345"),
        (0.0014810000000000001, "0.001481"),
        (9.600000000000017e-05, "0.000096"),
        (1e-08, "<0.000001"),
        (-2.5, "-2.50"),
    ],
)
def test_money_keeps_two_decimals_and_shows_sub_cent_digits(
    value: float, shown: str
) -> None:
    assert money(value) == shown


# --- Table width (run-through finding 9) ------------------------------------------

from solwyn_cli.output import table_width  # noqa: E402


@pytest.mark.parametrize(
    ("environ", "tty", "width"),
    [
        ({"COLUMNS": "200"}, False, 200),
        ({"COLUMNS": "200"}, True, 200),
        ({}, False, 80),
        ({"COLUMNS": "junk"}, False, 80),
        ({"COLUMNS": "10"}, False, 80),
    ],
)
def test_tables_use_columns_when_set(
    environ: dict[str, str], tty: bool, width: int
) -> None:
    assert table_width(environ, stdout_is_tty=tty) == width
