-- Drive one panel frame against a stub ImGui.
--
-- Catches unbalanced style/disabled stacks and renamed symbols without needing
-- a window. Every control is rendered twice: once quiet, and once with every
-- widget reporting "changed", which is the pass that actually exercises the
-- click handlers.
--
--   python3 ~/.claude/skills/reascript-lua/assets/reascript_test.py \
--           reaper/test/ui_frame.lua

local here = debug.getinfo(1, "S").source:match("^@(.*[/\\])")
package.path = here .. "../?.lua;" .. package.path

local fails, checks = 0, 0
local function check(cond, label)
    checks = checks + 1
    if not cond then fails = fails + 1; print("FAIL: " .. label) end
end
local function eq(a, b, label)
    check(a == b, string.format("%s (got %s, want %s)",
          label, tostring(a), tostring(b)))
end

-- --- stub REAPER -----------------------------------------------------------

local FX_PARAMS = {"Threshold", "Ratio", "Attack"}
local stub_focus = {retval = 1, track = 1, fx = 0}
local touched = {ok = false}

local saved = {}
local function patch(name, fn)
    saved[name] = reaper[name]
    reaper[name] = fn
end

patch("GetFocusedFX2", function()
    return stub_focus.retval, stub_focus.track, 0, stub_focus.fx
end)
patch("GetTrack", function() return "TRACK" end)
patch("GetMasterTrack", function() return "MASTER" end)
patch("GetSelectedTrack", function() return "TRACK" end)
patch("GetMediaTrackInfo_Value", function(_, key)
    return key == "IP_TRACKNUMBER" and 3 or 1.0
end)
-- REAPER's fader taper. Unity gain is 0.716 along it, not 0.25 and not 0.5,
-- which is the whole reason state.json carries a position rather than a gain.
patch("DB2SLIDER", function(db) return (db + 150) / 162 * 1000 end)
patch("SLIDER2DB", function(s) return s / 1000 * 162 - 150 end)
patch("GetSetMediaTrackInfo_String", function() return true, "Vox" end)
patch("TrackFX_GetNamedConfigParm", function() return true, "ReaComp.vst3" end)
patch("TrackFX_GetFXName", function() return true, "VST: ReaComp" end)
patch("TrackFX_GetNumParams", function() return #FX_PARAMS end)
patch("TrackFX_GetParamName", function(_, _, i)
    return true, FX_PARAMS[i + 1] or ""
end)
patch("TrackFX_GetParamEx", function() return 0.5, 0.0, 1.0, 0.25 end)
-- The panel seeds its shadow from REAPER's normalisation, not a linear one.
patch("TrackFX_GetParamNormalized", function() return 0.5 end)
patch("TrackFX_GetFormattedParamValue", function() return true, "-12.0 dB" end)
patch("GetLastTouchedFX", function()
    return touched.ok, touched.track, touched.fx, touched.param
end)
local NOW = 1000.0
patch("time_precise", function() return NOW end)
local ext_layer = "A"
patch("GetExtState", function(_, key)
    return key == "layer" and ext_layer or ""
end)
patch("AddRemoveReaScript", function() return 12345 end)
-- Command IDs the stub "knows"; anything else resolves to 0, as REAPER does.
local KNOWN = {["40044"] = 40044, ["_RSbuiltin"] = 66474}
patch("NamedCommandLookup", function(id) return KNOWN[id] or 0 end)
patch("APIExists", function(name) return name == "CF_GetCommandText" end)
patch("CF_GetCommandText", function(_, cmd)
    if cmd == 40044 then return "Transport: Play/stop" end
    if cmd == 66474 then return "Script: XT Bypass Focused FX.lua" end
    return ""
end)
patch("ReverseNamedCommandLookup", function() return "_RSbuiltin" end)

-- Keep the test off the real mappings.json and state.json.
local paths = require "xt.paths"
local written = {}
paths.read = function() return nil end
paths.write_atomic = function(path, text) written[path] = text; return true end

-- --- stub ImGui ------------------------------------------------------------

local calls, changed, hovered = {}, false, true

local Stub = {}
local function record(name)
    return function(...) calls[name] = (calls[name] or 0) + 1; return nil end
end

for _, name in ipairs({
    "SetNextWindowSize", "TextColored", "Separator", "SameLine",
    "ProgressBar", "EndTable", "End", "PopStyleVar",
}) do Stub[name] = record(name) end

-- Widgets that take a string must be handed a string. Real ImGui raises on
-- anything else; a stub that shrugs lets a type error reach the user instead
-- of the test -- which is exactly how a boolean got passed to Text.
local function record_str(name, argn)
    return function(...)
        calls[name] = (calls[name] or 0) + 1
        local v = select(argn, ...)
        if type(v) ~= "string" then
            error(string.format("%s: argument %d must be a string, got %s (%s)",
                  name, argn, type(v), tostring(v)), 2)
        end
        return nil
    end
end

Stub.Text = record_str("Text", 2)
Stub.SeparatorText = record_str("SeparatorText", 2)
Stub.SetTooltip = record_str("SetTooltip", 2)

Stub.CreateContext = function() return "CTX" end
Stub.PushID = record("PushID")
Stub.PopID = record("PopID")
Stub.SetNextItemWidth = record("SetNextItemWidth")
Stub.BeginTabBar = function() return true end
Stub.EndTabBar = record("EndTabBar")
-- Both tabs are reported open, so one frame exercises every control rather
-- than only whichever tab happens to be selected.
Stub.BeginTabItem = function() calls.BeginTabItem = (calls.BeginTabItem or 0) + 1; return true end
Stub.EndTabItem = record("EndTabItem")
Stub.Combo = function(_, _, value, _) calls.Combo = (calls.Combo or 0) + 1
    -- On the "changed" pass, pick the last entry (the custom-action option),
    -- which is the branch that also renders an InputText.
    if changed then return true, 3 end
    return false, value
end
Stub.InputText = function(_, _, text) calls.InputText = (calls.InputText or 0) + 1
    if changed then return true, "_RSstub" end
    return false, text
end
Stub.Begin = function() return true, true end
Stub.BeginTable = function() calls.BeginTable = (calls.BeginTable or 0) + 1; return true end
Stub.TableNextColumn = record("TableNextColumn")
Stub.TableNextRow = record("TableNextRow")
Stub.TableSetupColumn = record_str("TableSetupColumn", 2)
Stub.TableHeadersRow = record("TableHeadersRow")
Stub.PushFont = function() calls.PushFont = (calls.PushFont or 0) + 1 end
Stub.PopFont = function() calls.PopFont = (calls.PopFont or 0) + 1 end
Stub.CreateFont = function() return "FONT" end
Stub.Attach = function() return true end
Stub.IsItemDeactivatedAfterEdit = function() return changed end
Stub.TableColumnFlags_WidthFixed = 8
Stub.TableColumnFlags_WidthStretch = 16
Stub.TableFlags_RowBg = 64
Stub.TableFlags_SizingFixedFit = 8192
Stub.SelectableFlags_SpanAllColumns = 1
Stub.Selectable = function(_, label, _selected, flags)
    calls.Selectable = (calls.Selectable or 0) + 1
    if type(label) ~= "string" then
        error("Selectable: label must be a string, got " .. type(label), 2)
    end
    -- A row-spanning selectable sits on top of the label field and eats its
    -- clicks, so the field can never be typed into. Guard the exact mistake.
    if flags and (flags & Stub.SelectableFlags_SpanAllColumns) ~= 0 then
        error("Selectable must not span all columns: it would cover the "
              .. "label input and swallow its clicks", 2)
    end
    return changed
end
Stub.IsItemClicked = function(_, button) return changed and button == 1 end
Stub.IsItemHovered = function() return changed end
-- Docked panels do NOT report focused, which is the case that broke: only
-- hover distinguishes "clicked our panel" from "clicked the arrange view".
Stub.IsWindowFocused = function() return false end
Stub.IsWindowHovered = function() return hovered end
Stub.Checkbox = function(_, _, v) return false, v end
Stub.TextWrapped = record_str("TextWrapped", 2)
-- The Fader tab. On the "changed" pass every one of these reports a click or
-- a drag, which is the pass that exercises the setters behind them.
Stub.RadioButton = function(_, label, _active)
    calls.RadioButton = (calls.RadioButton or 0) + 1
    if type(label) ~= "string" then
        error("RadioButton: label must be a string, got " .. type(label), 2)
    end
    return changed
end
Stub.SliderDouble = function(_, _, v, lo, hi)
    calls.SliderDouble = (calls.SliderDouble or 0) + 1
    -- Hand back an in-range value, not a canned one: a setter that clamps
    -- wrongly should show up as a value the panel then refuses to store.
    if changed then return true, lo + (hi - lo) * 0.25 end
    return false, v
end
Stub.SliderInt = function(_, _, v, lo, hi)
    calls.SliderInt = (calls.SliderInt or 0) + 1
    if changed then return true, math.floor(lo + (hi - lo) * 0.5) end
    return false, v
end
Stub.HoveredFlags_RootAndChildWindows = 3
Stub.IsAnyItemActive = function() return false end

local colour_depth, disabled_depth = 0, 0
Stub.PushStyleColor = function() colour_depth = colour_depth + 1 end
Stub.PopStyleColor = function(_, n) colour_depth = colour_depth - (n or 1) end
Stub.BeginDisabled = function() disabled_depth = disabled_depth + 1 end
Stub.EndDisabled = function() disabled_depth = disabled_depth - 1 end

Stub.Col_Text = 0
Stub.Cond_FirstUseEver = 0
Stub.TableFlags_Borders = 1
Stub.TableFlags_SizingStretchSame = 2
Stub.FocusedFlags_RootAndChildWindows = 4

-- The real imgui shim raises on an unknown field rather than returning nil.
-- Mimic that, so a renamed symbol fails here instead of at runtime.
setmetatable(Stub, {__index = function(_, k)
    error("unknown ImGui field: " .. tostring(k), 2)
end})

-- --- run -------------------------------------------------------------------

local UI = require "xt.ui"
UI._init(Stub, here .. "../")

for _, pass in ipairs({{name = "quiet", changed = false},
                       {name = "changed", changed = true}}) do
    changed = pass.changed
    calls = {}
    colour_depth, disabled_depth = 0, 0

    local ok, err = pcall(UI._frame)
    check(ok, "frame renders (" .. pass.name .. "): " .. tostring(err))
    eq(colour_depth, 0, "colour stack balanced (" .. pass.name .. ")")
    eq(disabled_depth, 0, "disabled stack balanced (" .. pass.name .. ")")

    local depth = UI._depth()
    eq(depth.colour, 0, "module colour depth reset (" .. pass.name .. ")")
    eq(depth.disabled, 0, "module disabled depth reset (" .. pass.name .. ")")

    -- 8 encoders + 8 buttons, on each of two layers.
    eq(calls.Selectable, 32, "every slot rendered (" .. pass.name .. ")")
    eq(calls.PushFont, calls.PopFont, "font stack balanced (" .. pass.name .. ")")
    eq(UI._depth().font, 0, "module font depth reset (" .. pass.name .. ")")
    eq(calls.BeginTabItem, 4, "four tabs rendered (" .. pass.name .. ")")
    -- One per mode. The tab must render them whatever mode is selected, or
    -- there would be no way back out of the one you are in.
    eq(calls.RadioButton, 3, "a radio per fader mode (" .. pass.name .. ")")
    eq(calls.SliderDouble, 2, "sensitivity and timeout (" .. pass.name .. ")")
    eq(calls.SliderInt, 1, "end zone (" .. pass.name .. ")")
    -- One text field per reserved button, plus one per built-in ID to copy.
    check((calls.InputText or 0) >= 8,
          "a field for every reserved button (" .. pass.name .. ")")
    eq(calls.PushID, calls.PopID, "ID stack balanced (" .. pass.name .. ")")
end

-- state.json must be written even with nothing focused, because the fader and
-- the reserved row keep working when the encoders do not.
check(next(written) ~= nil, "state.json written")

stub_focus.retval = 0
changed = false
local ok = pcall(UI._frame)
check(ok, "frame renders with no FX focused")

-- Strict focus: no plugin, no slots.
local ST = UI._state()
eq(ST.ctx, nil, "context cleared when nothing is focused")
eq(ST.layers, nil, "layers cleared when nothing is focused")

-- The docked-panel case, which is what actually broke in use: ImGui never
-- reports a docked window as focused, because clicking it focuses REAPER's
-- main window. Hover is the only thing separating "clicked our panel" from
-- "clicked the arrange view".
local learn = require "xt.learn"
local function frame() pcall(UI._frame) end

stub_focus.retval = 1                 -- plugin focused: latch it
hovered = false
frame()
check(UI._state().ctx ~= nil, "latches the plugin while it is focused")

stub_focus.retval = 1 | 4             -- open, focus moved to our docked panel
hovered = true
frame()
check(UI._state().ctx ~= nil,
      "hovering the docked panel keeps the plugin active")

-- Reaching from the panel across to the plugin's own control: the pointer
-- leaves us for a moment and must not blank the panel mid-gesture.
hovered = false
NOW = NOW + 0.5
frame()
check(UI._state().ctx ~= nil, "grace period survives the pointer leaving")

-- Long gone with nothing armed: strict focus applies.
NOW = NOW + 5.0
frame()
eq(UI._state().ctx, nil, "focus elsewhere deactivates once grace expires")

-- An armed slot outlives the grace period, because touching the control
-- necessarily takes focus away from here.
stub_focus.retval = 1
hovered = true
NOW = NOW + 1.0
frame()
check(UI._state().ctx ~= nil, "re-latched")
learn.arm("A", "encoders", 1)
stub_focus.retval = 1 | 4
hovered = false
NOW = NOW + 30.0
frame()
check(UI._state().ctx ~= nil, "an armed slot keeps the panel alive")

learn.cancel()
NOW = NOW + 30.0
frame()
eq(UI._state().ctx, nil, "deactivates again once the arm is cleared")

-- The full learn gesture: arm a slot, then "touch" a control in the plugin.
-- This is the path that crashed in real use, and it had never been driven --
-- every earlier scenario stopped at arming.
do
    stub_focus.retval = 1
    hovered = true
    NOW = NOW + 1.0
    changed = false
    frame()
    check(UI._state().ctx ~= nil, "plugin active before learning")

    learn.arm("A", "encoders", 2)
    check(learn.state() == learn.ARMED, "armed")

    -- GetLastTouchedFX now reports a parameter on the focused FX. Track
    -- numbering must match what GetFocusedFX2 reports, or learn ignores it.
    touched = {ok = true, track = stub_focus.track, fx = stub_focus.fx,
               param = 2}

    calls = {}
    local ok, err = pcall(UI._frame)
    check(ok, "capture frame renders: " .. tostring(err))
    eq(UI._depth().table, 0, "table stack balanced after capture")
    eq(UI._depth().tab_item, 0, "tab item stack balanced after capture")
    eq(UI._depth().tab_bar, 0, "tab bar stack balanced after capture")
    eq(learn.state(), learn.IDLE, "arm cleared once captured")

    -- A mapped slot must render a label field, and the parameter cell's
    -- selectable must not span the row on top of it.
    check((calls.InputText or 0) > 0,
          "a mapped slot renders its label field")

    local slot = (UI._state().layers or {}).A
    slot = slot and slot.encoders and slot.encoders[2]
    check(slot ~= nil, "the captured slot is now resolved")
    if slot then
        eq(slot.param, 2, "captured the parameter that was touched")
        eq(slot.name, FX_PARAMS[3], "and its name")
        -- Seeded so the label is there to be shortened, not typed from
        -- scratch. Registered names are long; editing beats authoring.
        eq(slot.label, FX_PARAMS[3], "label seeded with the parameter name")
        local m = require "xt.mapping"
        eq(m.display_name(slot), FX_PARAMS[3],
           "display shows the seeded label")
    end

    -- Re-pointing a slot at the SAME parameter keeps what you tuned.
    local ident = UI._state().ctx.ident
    local m = require "xt.mapping"
    m.set_label(UI._state().data, ident, "A", "encoders", 2, "My Name")
    m.set_taper(UI._state().data, ident, "A", "encoders", 2, "log")
    m.set_sensitivity(UI._state().data, ident, "A", "encoders", 2, 2.0)

    learn.arm("A", "encoders", 2)
    touched = {ok = true, track = stub_focus.track, fx = stub_focus.fx,
               param = 2}                       -- the same parameter again
    frame()

    local kept = m.stored_slot(UI._state().data, ident, "A", "encoders", 2)
    check(kept ~= nil, "slot still mapped after re-learning")
    if kept then
        eq(kept.param, 2, "still the same parameter")
        eq(kept.label, "My Name", "a written label survives")
        eq(kept.taper, "log", "taper survives")
        eq(kept.sensitivity, 2.0, "sensitivity survives")
    end

    -- Pointing it at a DIFFERENT parameter starts clean: a label reading
    -- "Threshold" while driving "Attack" is worse than no label, and a taper
    -- chosen for one control means nothing on another.
    learn.arm("A", "encoders", 2)
    touched = {ok = true, track = stub_focus.track, fx = stub_focus.fx,
               param = 1}
    frame()

    local fresh = m.stored_slot(UI._state().data, ident, "A", "encoders", 2)
    check(fresh ~= nil, "slot mapped to the new parameter")
    if fresh then
        eq(fresh.param, 1, "re-learned onto the new parameter")
        eq(fresh.label, FX_PARAMS[2], "label reset to the new parameter name")
        eq(fresh.taper, nil, "taper reset to linear")
        eq(fresh.sensitivity, nil, "sensitivity reset to 1x")
    end
end

-- The Display tab follows the hardware layer, which the daemon reports by
-- setting an ExtState. Nothing else knows which layer the device is on.
do
    stub_focus.retval = 1
    hovered = true
    changed = false
    NOW = NOW + 1.0

    ext_layer = "A"
    calls = {}
    frame()
    check((calls.Text or 0) > 0, "display renders on layer A")

    ext_layer = "B"
    calls = {}
    frame()
    check((calls.Text or 0) > 0, "display renders on layer B")

    -- Junk in the ExtState must not break anything; A is the safe default.
    ext_layer = "nonsense"
    calls = {}
    check(pcall(UI._frame), "unknown layer value does not raise")
    ext_layer = "A"
end

-- A custom label replaces the registered parameter name in the display.
do
    local m = require "xt.mapping"
    eq(m.display_name({name = "Gain-Low Shelf"}), "Gain-Low Shelf",
       "falls back to the parameter name")
    eq(m.display_name({name = "Gain-Low Shelf", label = "Low"}), "Low",
       "prefers the custom label")
    eq(m.display_name({name = "Gain-Low Shelf", label = ""}), "Gain-Low Shelf",
       "an empty label is not a label")
    eq(m.display_name(nil), nil, "no slot, no name")

    local data = m.empty()
    m.set_slot(data, "P", "P", "A", "encoders", 1, {param = 4, name = "Freq"})
    m.set_label(data, "P", "A", "encoders", 1, "Cutoff")
    eq(data.plugins.P.layers.A.encoders[1].label, "Cutoff", "label stored")
    m.set_label(data, "P", "A", "encoders", 1, "")
    eq(data.plugins.P.layers.A.encoders[1].label, nil, "empty label clears it")
    m.set_label(data, "P", "A", "encoders", 5, "x")     -- empty slot: no-op
    m.set_label(data, "nope", "A", "encoders", 1, "x")  -- unknown plugin

    eq(m.stored_slot(data, "P", "A", "encoders", 1).param, 4, "stored_slot")
    eq(m.stored_slot(data, "P", "A", "encoders", 5), nil, "empty slot is nil")
    eq(m.stored_slot(data, "nope", "A", "encoders", 1), nil, "unknown plugin")
end

-- Combo item strings are \0-separated, and a "\0" written before a DIGIT is
-- read by Lua as a multi-digit decimal escape -- which turned the sensitivity
-- menu into one usable entry followed by control characters.
do
    local menus = UI._menus()
    local function split(text)
        local out = {}
        for piece in text:gmatch("([^%z]*)%z") do out[#out + 1] = piece end
        return out
    end

    local taper = split(menus.taper)
    eq(#taper, #menus.taper_values, "taper menu has one entry per value")
    for i, v in ipairs(menus.taper_values) do
        eq(taper[i], v, "taper entry " .. i)
    end

    local sens = split(menus.sens)
    eq(#sens, #menus.sens_values, "sensitivity menu has one entry per value")
    for i, entry in ipairs(sens) do
        check(entry:match("^[%d%.]+x$") ~= nil,
              "sensitivity entry " .. i .. " is readable: " .. entry)
        check(not entry:find("%c"), "entry " .. i .. " has no control chars")
    end
    eq(sens[1], "0.25x", "first sensitivity entry")
    eq(sens[3], "1x", "default sensitivity entry")
    eq(sens[5], "4x", "last sensitivity entry")
end

-- Reserved-button labels fill themselves in from REAPER's own action names,
-- and a command ID REAPER does not know must be visible as such rather than
-- binding a button to nothing.
do
    local a = require "xt.actions"

    eq(a.resolve_name("40044"), "Transport: Play/stop", "native action name")
    eq(a.resolve_name("_RSbuiltin"), "XT Bypass Focused FX",
       "script name, stripped of its prefix and extension")
    eq(a.resolve_name("_RSnonsense"), nil, "unknown id has no name")
    eq(a.resolve_name(""), nil, "empty id")
    eq(a.resolve_name(nil), nil, "nil id")

    check(a.is_known("40044"), "native id is known")
    check(not a.is_known("_RSnonsense"), "typo is reported as unknown")
    check(not a.is_known(""), "empty id is not known")

    local reserved = {
        {kind = "action", id = "40044"},
        {kind = "action", id = "_RSbuiltin", label = "Bypass"},
        {kind = "action", id = "_RSnonsense"},
    }
    check(a.fill_labels(reserved), "reports that it filled something in")
    eq(reserved[1].label, "Transport: Play/stop", "empty label filled")
    eq(reserved[2].label, "Bypass", "a written label is left alone")
    eq(reserved[3].label, nil, "an unknown id gets no invented label")
    check(not a.fill_labels(reserved), "second pass changes nothing")
end

for name, fn in pairs(saved) do reaper[name] = fn end

print(string.format("\n%d checks, %d failures", checks, fails))
if os.exit then os.exit(fails == 0 and 0 or 1) end
