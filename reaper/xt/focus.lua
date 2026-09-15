-- Which plugin is the controller driving?
--
-- The rule is strict: the encoders are live only while an FX window is
-- focused. Click the arrange view or the mixer and the panel blanks.
--
-- With one exception. A ReaImGui panel is a real window, so clicking it takes
-- focus away from the plugin -- under a literal reading of the rule the panel
-- would blank itself the instant you touched it, and click-to-learn could
-- never work. So focus landing on our own panel does not count as losing
-- focus; focus landing anywhere else does.

local M = {}

-- GetFocusedFX2 retval bits.
local TRACK_FX, ITEM_FX, NOT_FOCUSED = 1, 2, 4
-- Set in the FX index when the FX lives in the input/monitoring chain.
local INPUT_FX_FLAG = 0x1000000

local last = nil          -- the context we were driving when focus was ours

--- Read REAPER's focused-FX state. Returns a raw table or nil.
local function read()
    local retval, tracknumber, _itemnumber, fxnumber = reaper.GetFocusedFX2()
    if retval == 0 then return nil end
    if retval & ITEM_FX ~= 0 then
        -- Take/item FX have a different addressing scheme in OSC and no track
        -- index to write to. Out of scope rather than silently mis-addressed.
        return nil
    end
    if retval & TRACK_FX == 0 then return nil end

    local track = tracknumber == 0 and reaper.GetMasterTrack(0)
                  or reaper.GetTrack(0, tracknumber - 1)
    if not track then return nil end

    return {
        track = track,
        track_index = tracknumber,
        fx = fxnumber & 0xFFFFFF,
        input_fx = (fxnumber & INPUT_FX_FLAG) ~= 0,
        focused = (retval & NOT_FOCUSED) == 0,
    }
end

local function describe(raw)
    local _, ident = reaper.TrackFX_GetNamedConfigParm(raw.track, raw.fx, "fx_ident")
    local _, name = reaper.TrackFX_GetFXName(raw.track, raw.fx, "")
    local _, track_name = reaper.GetSetMediaTrackInfo_String(
        raw.track, "P_NAME", "", false)
    return {
        track = raw.track,
        track_index = raw.track_index,
        track_name = track_name or "",
        fx = raw.fx,
        input_fx = raw.input_fx,
        ident = (ident ~= "" and ident) or name,
        name = name,
    }
end

local function same(a, b)
    return a and b and a.track == b.track and a.fx == b.fx
end

--- Resolve the active plugin for this frame.
-- panel_focused: true when our own ImGui window has focus or is being used.
-- Returns context table, or nil when nothing is active.
function M.poll(panel_focused)
    local raw = read()

    if raw == nil then
        -- No FX window open at all: nothing to hold on to, even for us.
        last = nil
        return nil
    end

    if raw.focused then
        if not same(last, raw) then last = describe(raw) end
        return last
    end

    -- The FX is open but something else has focus. Only our own panel earns a
    -- pass, and only for the plugin we were already driving.
    if panel_focused and same(last, raw) then
        return last
    end

    last = nil
    return nil
end

--- Forget the latched plugin (used when the panel closes).
function M.reset() last = nil end

--- Exposed for tests: the currently latched context.
function M.current() return last end

return M
