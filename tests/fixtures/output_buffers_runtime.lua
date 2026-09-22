-- Synthetic engine fixture. Test code, not a native campaign or speed benchmark.
handlers, entities, quantities = {}, {}, {["wooden-chest"] = 1, ["burner-inserter"] = 1, coal = 10}
defines = {events = {on_tick = 1}, build_check_type = {manual = 1},
           inventory = {furnace_result = 1, chest = 2}}
script = {get_event_handler = function(id) return handlers[id] end,
          on_event = function(id, callback) handlers[id] = callback end}
prototypes = {entity = {["burner-inserter"] = {
    inserter_pickup_position = {x=0,y=-1}, inserter_drop_position = {x=0,y=1.2}
}}}
force = {}
surface = {}
source = {name="stone-furnace",type="furnace",valid=true,unit_number=17,
    position={x=0,y=0},bounding_box={left_top={x=-0.8,y=-0.8},right_bottom={x=0.8,y=0.8}},
    force=force,surface=surface,products_finished=20,stored=10}
source.get_recipe = function() return {name="iron-plate"} end
source.get_inventory = function() return {get_item_count=function() return source.stored end} end
entities[1] = source
surface.find_entity = function(name,pos)
    for _,e in ipairs(entities) do
        if e.valid and e.name==name and math.abs(e.position.x-pos.x)<0.01
            and math.abs(e.position.y-pos.y)<0.01 then return e end
    end
end
surface.can_place_entity = function(p)
    if obstructed then return false end
    for _,e in ipairs(entities) do
        if e.valid and math.abs(e.position.x-p.position.x)<0.9
            and math.abs(e.position.y-p.position.y)<0.9 then return false end
    end
    return true
end
player = {position={x=5,y=0},force=force,surface=surface,crafting_queue_size=0,
          get_item_count=function(name) return quantities[name] or 0 end}
game = {tick=0}
fair_calls, build_calls = 0,0
fair = {actor=function() assert(not disconnected,"disconnected"); return player end,
        tick_handler=function() fair_calls=fair_calls+1 end}
handlers[1]=fair.tick_handler
fair.place = function(name,position,direction)
    assert(not out_of_reach,"native reach")
    assert(surface.can_place_entity{name=name,position=position},"obstructed")
    assert(quantities[name]>0,"unpaid")
    quantities[name]=quantities[name]-1; build_calls=build_calls+1
    local e={name=name,valid=true,position=position,direction=direction,unit_number=20+build_calls,
             force=force,surface=surface,stored=0,held_stack={valid_for_read=false}}
    e.get_inventory=function() return {get_item_count=function() return e.stored end} end
    if name=="burner-inserter" then
        e.pickup_target=source
        e.drop_target=entities[2]
    end
    table.insert(entities,e)
end
campaign = {entities={["recipe:iron-plate"]=source}, observe=function()
    return {tick=game.tick,entities={}}
end,transfer=function(role,item,n,id,extracting) transfer_called=true end}
helpers={table_to_json=function(value) return value end}
rcon={print=function(value) printed=value end}
storage={fair=fair,campaign=campaign,jev_session_id="test"}
function install_parts()
    local s=campaign.observe().output_buffers.sources["recipe:iron-plate"]
    assert(s and s.state=="proposed")
    params={source="recipe:iron-plate",layout=s.layout,part="chest",receipt="r1"}
    campaign.prepare_output_buffer(params); campaign.build_output_buffer(params)
    params={source="recipe:iron-plate",layout=s.layout,part="inserter",receipt="r2"}
    campaign.prepare_output_buffer(params); campaign.build_output_buffer(params)
    cell=storage.output_buffers.cells["recipe:iron-plate"]
end
function tick(t)
    game.tick=t
    handlers[1]({tick=t})
end
function deliver()
    source.stored=source.stored-1
    entities[2].stored=entities[2].stored+1
end
function commission()
    tick(30)
    deliver(); tick(60)
    deliver(); tick(90)
    deliver(); tick(120)
    assert(not cell.flow)
    tick(150)
    assert(cell.flow and cell.flow.received==3)
end
