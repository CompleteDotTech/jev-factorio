import math

import pytest

from jev_factorio.planning.connections import (
    MAX_CELLS,
    select_pole_positions,
    shortest_pipe_path,
)


def test_pipe_route_detours_around_collision_on_half_integer_grid():
    buildable = {(column + 0.5, row + 0.5)
                 for column in range(3) for row in range(2)}
    buildable.remove((1.5, 0.5))
    route = shortest_pipe_path((0.5, 0.5), (2.5, 0.5), buildable)
    assert route[0] == (0.5, 0.5)
    assert route[-1] == (2.5, 0.5)
    assert len(route) == 5
    assert set(route) <= buildable
    assert all(math.dist(left, right) == 1
               for left, right in zip(route, route[1:]))


def test_existing_pipes_bridge_otherwise_disconnected_cells():
    endpoints = {(0, 0), (2, 0)}
    with pytest.raises(ValueError, match="No passable"):
        shortest_pipe_path((0, 0), (2, 0), endpoints)
    assert shortest_pipe_path((0, 0), (2, 0), endpoints, {(1, 0)}) == [
        (0, 0), (1, 0), (2, 0),
    ]


@pytest.mark.parametrize("invalid", [
    (float("nan"), 0), (float("inf"), 0), (0.25, 0),
    (True, 0), ("0", 0), (10**1000, 0), (0,),
])
def test_pipe_rejects_invalid_coordinates(invalid):
    with pytest.raises(ValueError):
        shortest_pipe_path(invalid, (0, 0), {(0, 0)})


def test_pipe_requires_verified_endpoints_and_common_grid():
    with pytest.raises(ValueError, match="endpoints must be passable"):
        shortest_pipe_path((0, 0), (1, 0), {(0, 0)})
    with pytest.raises(ValueError, match="share a unit grid"):
        shortest_pipe_path((0, 0), (0.5, 0), {(0, 0), (0.5, 0)})
    assert shortest_pipe_path((0, 0), (0, 0), {(0, 0)}) == [(0, 0)]


def test_pipe_bounds_input_and_path_lengths():
    with pytest.raises(ValueError, match="Too many"):
        shortest_pipe_path((0, 0), (1, 0),
                           {(column, 0) for column in range(MAX_CELLS + 1)})
    with pytest.raises(ValueError, match="maximum length"):
        shortest_pipe_path((0, 0), (512, 0),
                           {(column, 0) for column in range(513)})


def test_poles_preserve_endpoints_and_never_exceed_wire_distance():
    path = [(column, 0) for column in range(20)]
    selected = select_pole_positions(path)
    assert selected == [(0, 0), (7, 0), (14, 0), (19, 0)]
    assert all(math.dist(left, right) <= 7.5
               for left, right in zip(selected, selected[1:]))


def test_poles_handle_bends_without_inventing_placement_cells():
    path = [(0, 0), (1, 0), (1, 1), (1, 2), (0, 2)]
    selected = select_pole_positions(path, 1.5)
    assert selected[0] == path[0] and selected[-1] == path[-1]
    assert set(selected) <= set(path)
    assert all(math.dist(left, right) <= 1.5
               for left, right in zip(selected, selected[1:]))


@pytest.mark.parametrize("distance", [0, -1, float("nan"), float("inf"), True, "7"])
def test_poles_reject_invalid_wire_distance(distance):
    with pytest.raises(ValueError):
        select_pole_positions([(0, 0)], distance)


def test_poles_reject_empty_disconnected_and_excessive_routes():
    for path in ([], [(0, 0), (10, 0)], [(column, 0) for column in range(513)]):
        with pytest.raises(ValueError):
            select_pole_positions(path)
    assert select_pole_positions([(0, 0)]) == [(0, 0)]
