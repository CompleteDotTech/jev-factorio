"""Execute the real Lua buffer adapter against a deliberately synthetic game."""
from importlib.resources import files
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures/output_buffers_runtime.lua"
SCENARIOS = [
    """install_parts(); assert(build_calls==2); assert(quantities['wooden-chest']==0);
    assert(quantities['burner-inserter']==0); assert(not cell.flow); commission();
    assert(fair_calls==5); assert(campaign.observe().output_buffers.sources['recipe:iron-plate'].topology)""",
    """install_parts(); assert(not pcall(campaign.build_output_buffer,params)); assert(build_calls==2)""",
    """install_parts(); params.layout='wrong'; assert(not pcall(campaign.prepare_output_buffer,params))""",
    """install_parts(); tick(30); tick(300); assert(not cell.flow); assert(source.stored==10)""",
    """install_parts(); tick(30); entities[2].stored=9; tick(60); assert(cell.fault); assert(not cell.flow)""",
    """install_parts(); tick(30); deliver(); tick(60); source.unit_number=99; tick(90); assert(cell.fault)""",
    """install_parts(); entities[3].direction=(entities[3].direction+4)%16; tick(30); assert(cell.fault)""",
    """install_parts(); entities[3].drop_target=nil; tick(180);
    assert(campaign.observe().output_buffers.sources['recipe:iron-plate'].state=='fault')""",
    """install_parts(); assert(not pcall(campaign.transfer,cell.chest_role,'iron-plate',1,'t',false));
    assert(not pcall(campaign.transfer,cell.source,'iron-plate',1,'t',true));
    assert(not pcall(campaign.transfer,cell.chest_role,'iron-plate',1,'t',true));
    assert(not transfer_called); commission(); campaign.transfer(cell.chest_role,'iron-plate',1,'t',true);
    assert(transfer_called)""",
    """install_parts(); source.products_finished=10; tick(30); source.products_finished=9; tick(60);
    assert(cell.fault)""",
    """install_parts(); entities[3].held_stack={valid_for_read=true,name='copper-plate',count=1};
    tick(30); assert(cell.fault)""",
    """obstructed=true; assert(next(campaign.observe().output_buffers.sources)==nil); assert(build_calls==0)""",
    """install_parts(); handlers[1]=function() end;
    assert(campaign.observe().output_buffers.sources['recipe:iron-plate'].state=='fault')""",
    """local s=campaign.observe().output_buffers.sources['recipe:iron-plate'];
    params={source=s.source,layout=s.layout,part='chest',receipt='r1'};
    campaign.prepare_output_buffer(params); out_of_reach=true;
    assert(not pcall(campaign.build_output_buffer,params)); assert(build_calls==0)""",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_native_buffer_scenarios(scenario):
    lua = pytest.importorskip("lupa").LuaRuntime()
    lua.execute(FIXTURE.read_text())
    lua.execute(files("jev_factorio").joinpath("lua/output_buffers.lua").read_text())
    lua.execute(scenario)


def test_reattachment_preserves_receipts_and_does_not_recurse():
    lua = pytest.importorskip("lupa").LuaRuntime()
    lua.execute(FIXTURE.read_text())
    code = files("jev_factorio").joinpath("lua/output_buffers.lua").read_text()
    lua.execute(code)
    lua.execute("install_parts(); commission()")
    for _ in range(3):
        lua.execute(code)
        lua.execute("assert(campaign.observe().output_buffers.sources['recipe:iron-plate'].flow.received==3)")
    lua.execute("assert(build_calls==2)")
