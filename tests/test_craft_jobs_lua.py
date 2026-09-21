"""Run the actual job tracker with a mocked Lua game, never a Factorio server."""
from importlib.resources import files

import pytest


@pytest.fixture
def runtime():
    lua = pytest.importorskip("lupa").LuaRuntime()
    lua.execute('''
        handlers, counts, previous_calls = {}, {plate=20, pack=0}, 0
        defines = {events={on_pre_player_crafted_item=1, on_player_cancelled_crafting=2,
                           on_player_crafted_item=3}}
        script = {
            get_event_handler=function(event) return handlers[event] end,
            on_event=function(event, handler) handlers[event]=handler end
        }
        handlers[3] = function(event) previous_calls = previous_calls + 1 end
        recipe = {name="pack", enabled=true, ingredients={{type="item",name="plate",amount=1}},
                  products={{type="item",name="pack",amount=1}}}
        player = {index=1, character={unit_number=9}, surface={index=1},
                  force={index=1, recipes={pack=recipe}}, crafting_queue_size=0, crafting_queue={}}
        player.get_item_count=function(name) return counts[name] or 0 end
        player.get_main_inventory=function() return {get_contents=function()
            local result={}
            for name, count in pairs(counts) do table.insert(result,{name=name,count=count}) end
            return result
        end} end
        begin_calls = 0
        player.begin_crafting=function(parameters)
            begin_calls=begin_calls+1
            counts.plate=counts.plate-parameters.count
            player.crafting_queue_size=1
            player.crafting_queue={{recipe="pack",count=parameters.count,prerequisite=false}}
            if handlers[1] then handlers[1]{player_index=1,recipe=recipe} end
            return parameters.count
        end
        game={tick=10,speed=1}
        storage={jev_session_id="session", campaign={observe=function() return {} end},
                 fair={actor=function() assert(game.speed==1,"speed changed"); return player end}}
        rcon={print=function(value) last_output=value end}
        function finish_one()
            game.tick=game.tick+60
            handlers[3]{player_index=1,recipe=recipe,
                item_stack={valid_for_read=true,name="pack",count=1,quality={name="normal"}}}
            counts.pack=counts.pack+1
            player.crafting_queue[1].count=player.crafting_queue[1].count-1
            if player.crafting_queue[1].count==0 then
                player.crafting_queue={}; player.crafting_queue_size=0
            end
        end
    ''')
    lua.execute(files("jev_factorio").joinpath("lua/craft_jobs.lua").read_text())
    return lua


def test_native_acceptance_tracks_real_events_not_awarded_items(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        assert(counts.plate==10 and counts.pack==0 and begin_calls==1)
        local job=storage.campaign.observe().craft_job
        assert(job.accepted==10 and job.finished==0 and job.paid and job.queue_valid)
        finish_one()
        job=storage.campaign.observe().craft_job
        assert(job.finished==1 and job.status=="running" and counts.pack==1)
        for i=1,9 do finish_one() end
        job=storage.campaign.observe().craft_job
        assert(job.finished==10 and job.status=="completed" and job.completed_tick==game.tick)
        assert(counts.pack==10 and begin_calls==1 and previous_calls==10)
    ''')


@pytest.mark.parametrize("event", [1, 2])
def test_manual_queue_change_or_cancellation_contaminates_job(runtime, event):
    runtime.execute(f'''
        storage.campaign.begin_craft_job("job1","pack",10)
        handlers[{event}]{{player_index=1,recipe=recipe}}
        assert(storage.campaign.observe().craft_job.status=="invalid")
        assert(counts.pack==0 and begin_calls==1)
    ''')
    with pytest.raises(Exception, match="unresolved"):
        runtime.execute('storage.campaign.begin_craft_job("job2","pack",10)')


def test_receipt_does_not_restart_on_reattachment(runtime):
    runtime.execute('storage.campaign.begin_craft_job("job1","pack",10)')
    source = files("jev_factorio").joinpath("lua/craft_jobs.lua").read_text()
    runtime.execute(source)
    runtime.execute(source)
    runtime.execute('''
        finish_one()
        assert(storage.campaign.observe().craft_job.finished==1)
        assert(begin_calls==1 and previous_calls==1)
    ''')


def test_duplicate_enqueue_is_rejected_even_after_completion(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        for i=1,10 do finish_one() end
    ''')
    with pytest.raises(Exception, match="already used"):
        runtime.execute('storage.campaign.begin_craft_job("job1","pack",10)')
    assert runtime.eval("begin_calls") == 1


def test_partial_native_acceptance_never_becomes_acknowledged_job(runtime):
    runtime.execute('player.begin_crafting=function() begin_calls=begin_calls+1; return 5 end')
    with pytest.raises(Exception, match="Partial native"):
        runtime.execute('storage.campaign.begin_craft_job("job1","pack",10)')
    assert runtime.eval("storage.campaign.craft_jobs.job.status") == "invalid"
    assert runtime.eval("begin_calls") == 1


def test_missing_input_debit_is_not_accepted(runtime):
    runtime.execute('player.begin_crafting=function() begin_calls=begin_calls+1; return 10 end')
    with pytest.raises(Exception, match="debit mismatch"):
        runtime.execute('storage.campaign.begin_craft_job("job1","pack",10)')
    assert runtime.eval("storage.campaign.craft_jobs.job.paid") is False


def test_foreign_player_event_is_not_attributed_to_job(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        handlers[3]{player_index=2,recipe=recipe,item_stack={valid_for_read=true,name="pack",count=1}}
        assert(storage.campaign.observe().craft_job.finished==0)
    ''')


def test_actor_change_invalidates_even_completed_receipt(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        player.character={unit_number=99}
        assert(storage.campaign.observe().craft_job.status=="invalid")
    ''')


def test_wrong_recipe_and_queue_shape_are_detected(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        player.crafting_queue[1].recipe="other"
        assert(storage.campaign.observe().craft_job.status=="invalid")
    ''')


def test_inventory_and_receipt_are_captured_in_same_observation(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        finish_one()
        local result=storage.campaign.observe()
        assert(result.craft_job_inventory.tick==game.tick)
        assert(result.craft_job_inventory.items.pack==result.craft_job.finished)
        assert(result.craft_job_inventory.items.plate==10)
    ''')


def test_replaced_event_handler_requires_reconciliation_before_reattachment(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        local previous=handlers[3]
        handlers[3]=function(event) previous(event) end
    ''')
    with pytest.raises(Exception, match="handler changed"):
        runtime.execute(files("jev_factorio").joinpath("lua/craft_jobs.lua").read_text())
    assert runtime.eval("begin_calls") == 1


def test_observer_wrapping_does_not_recurse_through_mutable_previous_pointer(runtime):
    runtime.execute('''
        storage.campaign.begin_craft_job("job1","pack",10)
        local previous=storage.campaign.observe
        storage.campaign.observe=function() return previous() end
    ''')
    runtime.execute(files("jev_factorio").joinpath("lua/craft_jobs.lua").read_text())
    runtime.execute('''
        finish_one()
        assert(storage.campaign.observe().craft_job.finished==1)
        assert(begin_calls==1 and previous_calls==1)
    ''')
