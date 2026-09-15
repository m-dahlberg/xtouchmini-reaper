-- Click an encoder in the panel, then touch the control in the plugin.
--
-- The whole gesture rests on GetLastTouchedFX: REAPER reports the parameter
-- you last moved *anywhere*, including from inside the plugin's own GUI. So
-- there is nothing to hook and no need to understand any plugin's interface --
-- the user tells REAPER which control they mean by wiggling it, and we read
-- back what REAPER saw.
--
-- The catch is that "last touched" is sticky: it still names whatever was
-- touched before arming. So arming records the current value and waits for it
-- to change, rather than trusting whatever is already sitting there.

local M = {}

M.IDLE, M.ARMED, M.CAPTURED = "idle", "armed", "captured"

local armed = nil

--- Arm a slot. kind is "encoders" or "buttons"; index is 1-based.
function M.arm(layer, kind, index)
    local ok, track, fx, param = reaper.GetLastTouchedFX()
    armed = {
        layer = layer, kind = kind, index = index,
        -- What was already "last touched" when we armed. Anything still
        -- reporting this is stale, not the user's answer.
        seen_track = ok and track or nil,
        seen_fx = ok and fx or nil,
        seen_param = ok and param or nil,
        started = reaper.time_precise(),
    }
    return armed
end

function M.cancel() armed = nil end

function M.state()
    if not armed then return M.IDLE end
    return M.ARMED
end

--- The slot currently waiting for a control, or nil.
function M.target()
    if not armed then return nil end
    return armed.layer, armed.kind, armed.index
end

function M.is_armed(layer, kind, index)
    return armed ~= nil and armed.layer == layer and armed.kind == kind
           and armed.index == index
end

--- Poll for a touched parameter. Call once per frame while armed.
-- ctx is the active focus context, so a touch on some *other* plugin is
-- ignored rather than silently mapped to the wrong FX.
-- Returns nil while waiting, or a table {layer, kind, index, param, name}.
function M.poll(ctx)
    if not armed or not ctx then return nil end

    local ok, track_index, fx_index, param = reaper.GetLastTouchedFX()
    if not ok then return nil end

    -- Still reporting what was there before we armed: the user has not
    -- answered yet.
    if track_index == armed.seen_track and fx_index == armed.seen_fx
       and param == armed.seen_param then
        return nil
    end

    -- GetLastTouchedFX numbers tracks from 1 with 0 for the master, the same
    -- as GetFocusedFX2, so the two are directly comparable.
    if track_index ~= ctx.track_index or fx_index ~= ctx.fx then
        -- A control on a different plugin. Not an error -- the user may have
        -- brushed something -- but not an answer either.
        return nil
    end

    local _, name = reaper.TrackFX_GetParamName(ctx.track, ctx.fx, param, "")
    local result = {
        layer = armed.layer, kind = armed.kind, index = armed.index,
        param = param, name = name,
    }
    armed = nil
    return result
end

--- How long the current arm has been waiting, for the panel's prompt.
function M.waiting_for()
    if not armed then return 0 end
    return reaper.time_precise() - armed.started
end

function M.reset() armed = nil end

return M
