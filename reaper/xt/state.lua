-- Write state.json, the one file the daemon reads.
--
-- On change only, never on a timer: the daemon polls this file's mtime once
-- per loop, and a rewrite 30 times a second would be 30 wakeups a second for
-- nothing. Atomically, so the daemon can never read a half-written file.

local json = require "xt.json"
local paths = require "xt.paths"
local mapping = require "xt.mapping"
local actions = require "xt.actions"

local M = {}

local seq = 0
local last_text = nil

local function slot_list(list, count)
    local out = {}
    for i = 1, count do out[i] = list[i] or json.NULL end
    return out
end

-- The daemon fires these as /action/<id>, so what goes into state.json is the
-- NUMERIC command ID: REAPER's OSC lookup cannot resolve an address holding an
-- SWS-style name like "_S&M_TOGLFXCHAIN". The named form stays in
-- mappings.json, because a script's number is only valid for this session.
local function reserved_list(reserved)
    local out = {}
    for i = 1, mapping.RESERVED do
        local entry = reserved and reserved[i]
        local number = (type(entry) == "table") and actions.command_number(entry.id)
        if number then
            out[i] = {kind = entry.kind or "action", id = number,
                      named = entry.id}
        else
            -- An unresolvable ID leaves the button dead, which is honest: the
            -- panel shows it in red.
            out[i] = json.NULL
        end
    end
    return out
end

--- Build the document. ctx may be nil (nothing focused).
-- track_index/track_name/volume describe the *selected* track, which the fader
-- follows whether or not a plugin is focused. The focused plugin's OWN track
-- is a different thing and lives in fx.track: select another track and the
-- encoders must keep driving the plugin you are looking at.
function M.build(ctx, layers, sel, reserved, fader)
    local doc = {
        version = mapping.VERSION,
        seq = seq,
        active = ctx ~= nil,
        track = {
            index = sel.index or -1,
            name = sel.name or "",
            volume = sel.volume or 0.0,
        },
        fx = {index = -1, ident = "", name = "", track = -1},
        -- How the fader behaves, edited in the Fader tab. Always written in
        -- full, never as a diff: the daemon defaults the whole block when it
        -- is absent, so a partial one would silently reset whatever it left
        -- out to the default rather than leaving it alone.
        fader = mapping.fader({fader = fader}),
        layers = {},
        reserved = reserved_list(reserved),
        -- Command IDs the daemon fires to tell the panel something. The device
        -- switches layers by itself and only the daemon sees which CC numbers
        -- arrived, so this is the only way the panel can follow the hardware.
        -- Numeric, for the same reason as the reserved row.
        notify = {
            layer_a = actions.command_number(actions.command_for("layer_a"))
                      or json.NULL,
            layer_b = actions.command_number(actions.command_for("layer_b"))
                      or json.NULL,
        },
    }
    if ctx then
        -- ctx.track_index is GetFocusedFX2's track for the focused FX, which
        -- has nothing to do with what is selected. The daemon addresses its
        -- writes with this; using the selected track instead sends them to
        -- whatever plugin sits at the same chain position over there.
        doc.fx = {index = ctx.fx, ident = ctx.ident, name = ctx.name,
                  track = ctx.track_index or -1}
    end
    for _, layer in ipairs(mapping.LAYERS) do
        local block = (layers or {})[layer] or {encoders = {}, buttons = {}}
        doc.layers[layer] = {
            encoders = slot_list(block.encoders or {}, mapping.ENCODERS),
            buttons = slot_list(block.buttons or {}, mapping.BUTTONS),
        }
    end
    return doc
end

--- Serialise and write if anything changed. Returns "written", "unchanged",
-- or nil plus an error.
function M.write(doc)
    -- Compare with seq zeroed: bumping seq must not by itself make the
    -- document look different, or every frame would count as a change.
    local probe = doc.seq
    doc.seq = 0
    local text = json.encode(doc, "  ")
    doc.seq = probe

    if text == last_text then return "unchanged" end

    seq = seq + 1
    doc.seq = seq
    local ok, err = paths.write_atomic(paths.state(), json.encode(doc, "  "))
    if not ok then return nil, err end
    last_text = text
    return "written"
end

--- Build and write in one step.
function M.publish(ctx, layers, sel, reserved, fader)
    return M.write(M.build(ctx, layers, sel, reserved, fader))
end

function M.reset() seq, last_text = 0, nil end

return M
