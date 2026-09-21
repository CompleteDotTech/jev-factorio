"""Execute the actual Lua route adapter with a synthetic engine fixture."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "src/jev_factorio/lua/input_routes.lua"
FIXTURE = ROOT / "tests/fixtures/input_routes_runtime.lua"


def execute(code: str, tmp_path: Path) -> None:
    script = FIXTURE.read_text() + "\n" + ADAPTER.read_text() + "\n" + code
    try:
        from lupa.lua54 import LuaRuntime
    except ImportError:
        executable = shutil.which("luatex")
        if executable is None:
            pytest.skip("Requires Lupa or LuaTeX for the synthetic Lua test")
        path = tmp_path / "scenario.lua"
        path.write_text(script)
        result = subprocess.run([executable, "--luaonly", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        LuaRuntime().execute(script)


@pytest.mark.parametrize("code", [
    'local row=storage.campaign.observe().input_routes.sources["recipe:iron-plate"]; assert(row and #row.steps<=66); assert(placements==0); assert(row.steps[1].part=="inserter" and row.steps[#row.steps].part=="drill")',
    'local a=storage.campaign.observe().input_routes.sources["recipe:iron-plate"].layout; assert(storage.campaign.observe().input_routes.sources["recipe:iron-plate"].layout==a)',
    'local cell=build_all(); assert(placements==#cell.steps and not cell.flow); assert(stock["transport-belt"]==200-cell.belt_count)',
    'local cell=build_all();pulse();pulse();assert(not cell.flow);pulse();assert(cell.flow and cell.flow.new_plates==3 and cell.flow.mined==3)',
    'local cell=build_all();game.tick=game.tick+180;storage.campaign.observe();assert(not cell.flow)',
    'local cell=build_all();chest.get_inventory(4).values["iron-plate"]=10;game.tick=game.tick+180;storage.campaign.observe();assert(not cell.flow)',
    'local cell=build_all();cell.parts["belt:1"].entity.lines[1].values["copper-ore"]=1;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();cell.parts["belt:1"].entity.direction=12;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();cell.parts["belt:1"].entity.valid=false;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();cell.parts.inserter.entity.drop_target=nil;game.tick=game.tick+121;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();assert(not pcall(storage.campaign.transfer,"recipe:iron-plate","iron-ore",1,"x",false));assert(not pcall(storage.campaign.transfer,"out:chest","iron-plate",1,"x",true))',
    'local cell=build_all();storage.campaign.transfer(cell.parts.drill.role,"coal",5,"fuel",false);assert(stock.coal==95);assert(cell.parts.drill.entity.get_inventory(1).values.coal==5)',
    'local cell=build_all();pulse();pulse();pulse();storage.campaign.transfer("out:chest","iron-plate",3,"collect",true);assert(stock["iron-plate"]==3)',
    'local row=storage.campaign.observe().input_routes.sources["recipe:iron-plate"];stock["transport-belt"]=#row.steps-2+19;assert(not pcall(storage.campaign.prepare_input_route,{source=row.source,layout=row.layout,part="inserter",receipt="a",reserve_belts=20}));assert(placements==0)',
    'local row=storage.campaign.observe().input_routes.sources["recipe:iron-plate"];assert(not pcall(storage.campaign.prepare_input_route,{source=row.source,layout=row.layout,part="drill",receipt="a",reserve_belts=0}));assert(placements==0)',
    'local row=storage.campaign.observe().input_routes.sources["recipe:iron-plate"];player.crafting_queue_size=1;assert(not pcall(storage.campaign.prepare_input_route,{source=row.source,layout=row.layout,part="inserter",receipt="a",reserve_belts=20}));assert(placements==0)',
    'obstacle=function(q) return q.name=="transport-belt" end;assert(next(storage.campaign.observe().input_routes.sources)==nil);assert(placements==0)',
    'local cell=build_all();resources[1].amount=resources[1].amount-3;game.tick=game.tick+60;storage.campaign.observe();assert(cell.fault and not cell.flow)',
    'local cell=build_all();source.products_finished=source.products_finished-1;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();game.tick=game.tick-1;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();source.get_inventory(2).values["iron-ore"]=1;game.tick=game.tick+60;storage.campaign.observe();assert(cell.fault)',
    'local cell=build_all();local part=cell.parts["belt:1"];assert(not pcall(storage.campaign.transfer,part.role,"coal",1,"x",false))',
])
def test_route_runtime(code, tmp_path):
    execute(code, tmp_path)


def test_reattachment_preserves_single_observer_and_receipts(tmp_path):
    adapter = ADAPTER.read_text()
    execute('local cell=build_all(); local before=placements\n' + adapter + '\npulse();pulse();pulse();assert(cell.flow and placements==before)', tmp_path)


def test_new_output_requires_more_than_preloaded_furnace_material(tmp_path):
    execute('source.get_inventory(2).values["iron-ore"]=5;local cell=build_all();pulse();pulse();pulse();assert(not cell.flow);for n=1,5 do pulse() end;assert(cell.flow)', tmp_path)


def test_productivity_drift_invalidates_existing_route(tmp_path):
    execute('local cell=build_all();source.force.mining_drill_productivity_bonus=0.1;storage.campaign.observe();assert(cell.fault)', tmp_path)


@pytest.mark.parametrize("member", ["observe", "transfer"])
def test_reattachment_rejects_replaced_observer_or_transfer(tmp_path, member):
    execute('storage.campaign.' + member + '=function() end\nlocal ok=pcall(function()\n' + ADAPTER.read_text() + '\nend);assert(not ok)', tmp_path)


def test_belt_directions_match_real_step_coordinates(tmp_path):
    execute("""
        local row=storage.campaign.observe().input_routes.sources["recipe:iron-plate"]
        local parts={};for _,spec in ipairs(row.steps) do parts[spec.part]=spec end
        local vectors={[0]={x=0,y=-1},[4]={x=1,y=0},[8]={x=0,y=1},[12]={x=-1,y=0}}
        for n=1,#row.steps-2 do
            local current=parts["belt:"..n]
            local following=parts["belt:"..(n+1)] or parts.inserter
            local v=vectors[current.direction]
            assert(current.position.x+v.x==following.position.x)
            assert(current.position.y+v.y==following.position.y)
        end
    """, tmp_path)
