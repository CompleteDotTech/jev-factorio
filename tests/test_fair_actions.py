from importlib.resources import files

import pytest


@pytest.fixture
def fair_runtime():
    runtime = pytest.importorskip("lupa.lua54").LuaRuntime()
    runtime.execute("""
        handlers = {}
        defines = {
            controllers = {character = 1},
            events = {on_tick = 1, on_script_path_request_finished = 2},
            direction = {north = 0, northeast = 2, east = 4, southeast = 6,
                         south = 8, southwest = 10, west = 12, northwest = 14}
        }
        script = {
            get_event_handler = function(event) return handlers[event] end,
            on_event = function(event, callback) handlers[event] = callback end,
            on_nth_tick = function(interval, callback)
                handlers["nth" .. interval] = callback
            end
        }
        ticks_forwarded, paths_forwarded = 0, 0
        handlers[1] = function() ticks_forwarded = ticks_forwarded + 1 end
        handlers[2] = function() paths_forwarded = paths_forwarded + 1 end
        force = {}
        character = {
            valid = true, unit_number = 9,
            prototype = {collision_box = {}, collision_mask = {}}
        }
        resource = {valid = true, minable = true, position = {x = 2, y = 0}}
        quantities = {coal = 0, pipe = 4}
        surface = {
            request_path = function(parameters)
                requested_path = parameters
                return 17
            end,
            find_entities_filtered = function() return {resource} end,
            find_entity = function() return built_entity end
        }
        cursor = {count = 0}
        inventory = {
            find_item_stack = function(name)
                if (quantities[name] or 0) == 0 then return nil end
                return {name = name, count = quantities[name], valid_for_read = true}
            end,
            get_item_count = function(name) return quantities[name] or 0 end,
            remove = function(stack)
                quantities[stack.name] = quantities[stack.name] - stack.count
                return stack.count
            end,
            insert = function(stack)
                quantities[stack.name] = (quantities[stack.name] or 0) + stack.count
                return stack.count
            end
        }
        cursor.transfer_stack = function(stack)
            cursor.name, cursor.count = stack.name, stack.count
            quantities[stack.name] = 0
            return true
        end
        reachable, buildable = true, true
        player = {
            connected = true, character = character, cheat_mode = false,
            position = {x = 0, y = 0}, surface = surface, force = force,
            cursor_stack = cursor, build_distance = 10,
            get_item_count = inventory.get_item_count,
            get_main_inventory = function() return inventory end,
            can_reach_entity = function() return reachable end,
            can_build_from_cursor = function() return buildable end,
            clear_cursor = function()
                if cursor.name then
                    quantities[cursor.name] = quantities[cursor.name] + cursor.count
                end
                cursor.count, cursor.name = 0, nil
                return true
            end,
            build_from_cursor = function(parameters)
                cursor.count = cursor.count - 1
                built_entity = {
                    name = cursor.name, valid = true, force = force,
                    position = parameters.position, unit_number = 19, type = "pipe"
                }
                return true
            end
        }
        game = {tick = 0, speed = 1, get_player = function() return player end}
        player.update_selected_entity = function() player.selected = resource end
        storage = {agent_characters = {character}}
    """)
    runtime.execute(files("jev_factorio").joinpath("lua/fair_actions.lua").read_text())
    runtime.execute("storage.fair.bind()")
    return runtime


def test_native_walk_controls_player_and_stops_at_destination(fair_runtime):
    fair_runtime.execute("""
        storage.fair.begin_move{x = 2, y = 0}
        assert(storage.fair.job.status == "path_pending")
        handlers[2]{id = 17, path = {
            {position = {x = 0, y = 0}}, {position = {x = 2, y = 0}}
        }}
        handlers[1]{}
        assert(player.walking_state.walking)
        assert(player.walking_state.direction == defines.direction.east)
        assert(character.walking_state == nil)
        assert(player.position.x == 0)
        player.position = {x = 2, y = 0}
        handlers[1]{}
        assert(storage.fair.job.status == "completed")
        assert(not player.walking_state.walking)
    """)


@pytest.mark.parametrize("queue", ["crafting_queue", "harvest_queues", "walking_queues"])
def test_retained_legacy_work_is_quarantined_not_discarded(fair_runtime, queue):
    fair_runtime.execute("""
        handlers.nth5 = function() error("unsafe legacy walking") end
        handlers.nth15 = function() error("unsafe legacy mining") end
    """)
    fair_runtime.execute(f"storage.{queue} = {{ retained = true }}")
    with pytest.raises(Exception, match="Legacy scripted work must be reconciled"):
        fair_runtime.execute("storage.fair.bind()")
    assert fair_runtime.eval(f"storage.{queue}.retained") is True
    assert fair_runtime.eval("handlers.nth5") is None
    assert fair_runtime.eval("handlers.nth15") is None


def test_native_mining_observes_yield_without_inserting_resources(fair_runtime):
    fair_runtime.execute("""
        storage.fair.begin_mine({x = 2, y = 0}, "coal", 2)
        handlers[1]{}
        assert(player.mining_state.mining)
        assert(quantities.coal == 0)
        assert(character.mining_state == nil)
        quantities.coal = 1
        handlers[1]{}
        assert(storage.fair.job.status == "mining")
        quantities.coal = 2
        handlers[1]{}
        assert(storage.fair.job.status == "completed")
        assert(not player.mining_state.mining)
        assert(storage.fair.observe().gained == 2)
    """)


def test_mining_rejects_remote_target_and_stops_if_reach_changes(fair_runtime):
    fair_runtime.execute("""
        reachable = false
        assert(not pcall(storage.fair.begin_mine, {x = 2, y = 0}, "coal", 1))
        assert(quantities.coal == 0)
        reachable = true
        storage.fair.begin_mine({x = 2, y = 0}, "coal", 1)
        reachable = false
        handlers[1]{}
        assert(storage.fair.job.status == "failed")
        assert(not player.mining_state.mining)
    """)


@pytest.mark.parametrize("change", [
    "game.tick = 181",
    "player.connected = false",
    "game.speed = 2",
    "player.cheat_mode = true",
    "character.unit_number = 10",
])
def test_control_lease_and_actor_invariants_stop_native_controls(fair_runtime, change):
    fair_runtime.execute("""
        storage.fair.begin_mine({x = 2, y = 0}, "coal", 5)
    """)
    fair_runtime.execute(change)
    fair_runtime.execute("""
        handlers[1]{}
        assert(storage.fair.job.status == "failed")
        assert(not player.walking_state.walking)
        assert(not player.mining_state.mining)
    """)


def test_observation_renews_lease_and_explicit_stop_cancels(fair_runtime):
    fair_runtime.execute("""
        storage.fair.begin_mine({x = 2, y = 0}, "coal", 5)
        game.tick = 170
        storage.fair.observe()
        game.tick = 190
        handlers[1]{}
        assert(storage.fair.job.status == "mining")
        storage.fair.stop("Cancelled")
        assert(storage.fair.job.status == "failed")
        assert(not player.mining_state.mining)
    """)


def test_place_consumes_existing_stack_and_returns_cursor_remainder(fair_runtime):
    fair_runtime.execute("""
        local result = storage.fair.place("pipe", {x = 1, y = 0}, 0)
        assert(result.name == "pipe")
        assert(quantities.pipe == 3)
        assert(cursor.count == 0)
        assert(player.position.x == 0 and player.position.y == 0)
    """)


def test_failed_build_returns_all_cursor_items(fair_runtime):
    fair_runtime.execute("""
        buildable = false
        assert(not pcall(storage.fair.place, "pipe", {x = 100, y = 0}, 0))
        assert(quantities.pipe == 4)
        assert(cursor.count == 0)
        assert(built_entity == nil)
        assert(player.position.x == 0 and player.position.y == 0)
    """)


def test_callback_reload_preserves_previous_handlers_without_recursion(fair_runtime):
    source = files("jev_factorio").joinpath("lua/fair_actions.lua").read_text()
    fair_runtime.execute("""
        handlers.nth5 = function() error("unsafe movement") end
        handlers.nth15 = function() error("unsafe mining") end
    """)
    fair_runtime.execute(source)
    assert fair_runtime.eval("handlers.nth5") is None
    assert fair_runtime.eval("handlers.nth15") is None
    fair_runtime.execute(source)
    fair_runtime.execute("storage.fair.bind()")
    fair_runtime.execute("""
        handlers[1]{}
        handlers[2]{id = 500}
        assert(ticks_forwarded == 1)
        assert(paths_forwarded == 1)
    """)


def test_failed_path_reports_failure_without_moving(fair_runtime):
    fair_runtime.execute("""
        storage.fair.begin_move{x = 2, y = 0}
        handlers[2]{id = 17}
        assert(storage.fair.job.status == "failed")
        assert(not player.walking_state.walking)
        assert(player.position.x == 0)
    """)
