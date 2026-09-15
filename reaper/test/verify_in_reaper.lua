-- Live suite: real track, real plugin, real focus and learn.
--
-- Builds its own track and removes it again, and restores the previous track
-- selection, so running it does not disturb the project you had open.
--
--   python3 ~/.claude/skills/reascript-lua/assets/reascript_test.py \
--           reaper/test/verify_in_reaper.lua

local here = debug.getinfo(1, "S").source:match("^@(.*[/\\])")
package.path = here .. "../?.lua;" .. package.path

local mapping = require "xt.mapping"
local paths = require "xt.paths"
local statefile = require "xt.state"
local json = require "xt.json"

local fails, checks = 0, 0
local function check(cond, label)
    checks = checks + 1
    if not cond then fails = fails + 1; print("FAIL: " .. label) end
end
local function eq(a, b, label)
    check(a == b, string.format("%s (got %s, want %s)",
          label, tostring(a), tostring(b)))
end
local function bail(msg) fails = fails + 1; print("BAIL: " .. msg) end

-- Never write the real mapping or state files from a test.
local written = {}
paths.write_atomic = function(p, text) written[p] = text; return true end

-- --- fixture ---------------------------------------------------------------

local prev_sel = {}
for i = 0, reaper.CountSelectedTracks(0) - 1 do
    prev_sel[#prev_sel + 1] = reaper.GetSelectedTrack(0, i)
end

reaper.PreventUIRefresh(1)
reaper.InsertTrackAtIndex(reaper.CountTracks(0), false)
local track = reaper.GetTrack(0, reaper.CountTracks(0) - 1)
reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "XT Test", true)
local fx = reaper.TrackFX_AddByName(track, "ReaComp", false, 1)

local function cleanup()
    if track then reaper.DeleteTrack(track) end
    -- An empty project has nothing left to select once the fixture track is
    -- gone, and SetOnlyTrackSelected raises on a nil track rather than
    -- reading it as "select nothing" -- which turns a passing run into a
    -- RUNNER ERROR with no check count at all.
    local restore = prev_sel[1] or reaper.GetTrack(0, 0)
    if restore then reaper.SetOnlyTrackSelected(restore) end
    for _, t in ipairs(prev_sel) do reaper.SetTrackSelected(t, true) end
    reaper.PreventUIRefresh(-1)
    reaper.UpdateArrange()
end

if fx < 0 then
    bail("could not add ReaComp -- is it available?")
    cleanup()
    print(string.format("\n%d checks, %d failures", checks, fails))
    if os.exit then os.exit(1) end
    return
end

-- --- plugin identity -------------------------------------------------------

local ok_ident, ident = reaper.TrackFX_GetNamedConfigParm(track, fx, "fx_ident")
check(ok_ident and ident ~= "", "fx_ident is readable -- it is the mapping key")

local _, fxname = reaper.TrackFX_GetFXName(track, fx, "")
check(fxname ~= "", "FX name is readable")

local nparams = reaper.TrackFX_GetNumParams(track, fx)
check(nparams > 0, "ReaComp reports parameters")

-- --- parameter round-trip --------------------------------------------------

do
    local before = reaper.TrackFX_GetParamNormalized(track, fx, 0)
    reaper.TrackFX_SetParamNormalized(track, fx, 0, 0.25)
    local after = reaper.TrackFX_GetParamNormalized(track, fx, 0)
    check(math.abs(after - 0.25) < 0.01,
          string.format("normalised set/get round-trips (%.3f)", after))
    reaper.TrackFX_SetParamNormalized(track, fx, 0, before)
end

-- --- resolution against a real plugin --------------------------------------

do
    local _, pname = reaper.TrackFX_GetParamName(track, fx, 0, "")
    local slot = mapping.resolve_slot(track, fx, {param = 0, name = pname})
    check(slot ~= nil, "resolves a real parameter")
    if slot then
        eq(slot.param, 0, "index preserved")
        eq(slot.name, pname, "name matches")
        check(slot.value >= 0 and slot.value <= 1, "value is normalised")
        check(slot.default >= 0 and slot.default <= 1, "default is normalised")
    end

    -- A name that does not exist must resolve to nil rather than to index 0.
    eq(mapping.resolve_slot(track, fx, {param = 999, name = "Nope"}), nil,
       "unknown parameter resolves to nil")

    -- Stored index wrong, name right: must follow the name.
    local last = nparams - 1
    local _, lastname = reaper.TrackFX_GetParamName(track, fx, last, "")
    local moved = mapping.resolve_slot(track, fx, {param = 0, name = lastname})
    check(moved ~= nil and moved.param == last,
          "follows a moved parameter by name")
end

-- --- non-linear parameters -------------------------------------------------

do
    -- ReaEQ's gain parameters have a curve: a linear normalisation of
    -- GetParamEx disagrees with GetParamNormalized, and seeding the shadow
    -- with the linear figure made the first encoder detent jump.
    reaper.InsertTrackAtIndex(reaper.CountTracks(0), false)
    local eqtrack = reaper.GetTrack(0, reaper.CountTracks(0) - 1)
    local eq = reaper.TrackFX_AddByName(eqtrack, "ReaEQ", false, 1)
    if eq < 0 then
        bail("could not add ReaEQ")
    else
        local nonlinear = nil
        for i = 0, reaper.TrackFX_GetNumParams(eqtrack, eq) - 1 do
            local raw, lo, hi = reaper.TrackFX_GetParamEx(eqtrack, eq, i)
            local norm = reaper.TrackFX_GetParamNormalized(eqtrack, eq, i)
            local linear = (hi ~= lo) and (raw - lo) / (hi - lo) or 0
            if math.abs(linear - norm) > 0.001 then nonlinear = i break end
        end
        check(nonlinear ~= nil,
              "ReaEQ still has a non-linear parameter to test against")
        if nonlinear then
            local _, pname = reaper.TrackFX_GetParamName(
                eqtrack, eq, nonlinear, "")
            local slot = mapping.resolve_slot(eqtrack, eq,
                                              {param = nonlinear, name = pname})
            check(slot ~= nil, "resolves a non-linear parameter")
            if slot then
                local norm = reaper.TrackFX_GetParamNormalized(
                    eqtrack, eq, nonlinear)
                check(math.abs(slot.value - norm) < 0.0001,
                      string.format(
                        "value is REAPER's normalisation, not linear "
                        .. "(got %.4f, want %.4f)", slot.value, norm))
                check(slot.default >= 0 and slot.default <= 1,
                      "default stays in the normalised range")
            end
        end
    end
    reaper.DeleteTrack(eqtrack)
end

-- --- GetLastTouchedFX, the basis of the learn gesture -----------------------

do
    reaper.TrackFX_SetParamNormalized(track, fx, 1, 0.4)
    local ok, tnum, fxnum, param = reaper.GetLastTouchedFX()
    check(ok, "GetLastTouchedFX reports something after a parameter write")
    if ok then
        eq(param, 1, "reports the parameter that moved")
        eq(fxnum, fx, "reports the FX index")
        -- Track numbering is 1-based with 0 for the master, matching
        -- GetFocusedFX2, which is what lets learn compare the two directly.
        eq(tnum, math.floor(reaper.GetMediaTrackInfo_Value(
            track, "IP_TRACKNUMBER")), "track numbering matches GetFocusedFX2")
    end
end

-- --- state.json ------------------------------------------------------------

do
    statefile.reset()
    local ctx = {track = track, track_index = 3, track_name = "XT Test",
                 fx = fx, ident = ident, name = fxname}
    local entry = mapping.ensure(mapping.empty(), ident, fxname)
    local layers = mapping.resolve(entry, track, fx)
    -- Built the way the panel builds it, from a real track at a real gain.
    -- The number this produces is the one the daemon compares the fader
    -- against, so it has to be a fader POSITION: unity gain is 0.716 along
    -- REAPER's taper, and publishing D_VOL (1.0) or D_VOL/4 (0.25) instead
    -- puts the pickup target and the relative base in a scale of their own.
    local volume = require "xt.volume"
    reaper.SetMediaTrackInfo_Value(track, "D_VOL", 1.0)
    local gain = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    local position = volume.gain_to_slider(gain)
    check(math.abs(position - 0.716) < 0.01,
          string.format("unity gain publishes as 0.716, not %.4f", position))
    check(math.abs(position - gain) > 0.1, "and is not the raw gain")
    check(math.abs(position - gain / 4) > 0.1, "nor D_VOL/4")

    local sel = {index = 3, name = "XT Test", volume = position}

    local fader = mapping.fader(nil)
    fader.mode = "relative"
    local result = statefile.publish(ctx, layers, sel, {}, fader)
    eq(result, "written", "state.json written on first publish")
    eq(statefile.publish(ctx, layers, sel, {}, fader), "unchanged",
       "an unchanged frame does not rewrite the file")

    local text = written[paths.state()]
    check(text ~= nil, "state written to the expected path")
    if text then
        local ok, doc = json.decode(text)
        check(ok, "state.json parses")
        if ok then
            eq(doc.version, 1, "version")
            eq(doc.active, true, "active")
            eq(doc.fx.ident, ident, "fx ident carried")
            eq(#doc.layers.A.encoders, mapping.ENCODERS, "8 encoder slots")
            eq(#doc.layers.B.encoders, mapping.ENCODERS, "layer B present too")
            eq(#doc.reserved, mapping.RESERVED, "8 reserved slots")
            -- Unassigned slots must be null, not absent, or the array
            -- collapses and the daemon reads the wrong index.
            eq(doc.layers.A.encoders[1], json.NULL, "unassigned slot is null")
            -- The fader block is what carries the mode to the daemon, and it
            -- has to be complete: the daemon defaults the whole thing when a
            -- field is missing rather than only that field.
            eq(doc.fader.mode, "relative", "fader mode reaches state.json")
            eq(doc.fader.end_zone, 3, "and the tuning alongside it")
            eq(doc.fader.sensitivity, 1.0, "sensitivity too")
            eq(doc.fader.recal_timeout, 1.0, "and the walk-back timeout")
            check(math.abs(doc.track.volume - position) < 1e-6,
                  "the fader position reaches state.json unchanged")
        end
    end
end

-- --- reserved-row built-ins ------------------------------------------------

do
    local actions = require "xt.actions"
    actions.reset()
    local resolved = actions.resolve(reaper.GetResourcePath()
                                     .. "/Scripts/XtouchMini/")

    for _, entry in ipairs(actions.BUILTINS) do
        local id = resolved[entry.id]
        check(id ~= nil, "built-in registered: " .. entry.label)
        if id then
            -- The leading underscore matters: ReverseNamedCommandLookup omits
            -- it, but /action/<id> and NamedCommandLookup both require it.
            -- Without it every reserved button silently does nothing.
            eq(id:sub(1, 1), "_", "id is underscore-prefixed: " .. entry.id)
            check(reaper.NamedCommandLookup(id) ~= 0,
                  "REAPER resolves " .. id .. " back to a command")
        end
    end
end

-- --- action names against the real REAPER ----------------------------------

do
    local actions = require "xt.actions"
    local mine

    -- A native action everyone has. Its exact wording may change between
    -- REAPER versions, so assert the shape rather than the string.
    local native = actions.resolve_name("40044")
    check(native ~= nil and native ~= "",
          "a native command ID resolves to a name")
    check(actions.is_known("40044"), "a native command ID is known")

    -- One of ours, registered by resolve() above.
    mine = actions.command_for("bypass")
    if mine then
        check(actions.is_known(mine), "our own script ID is known")
        local name = actions.resolve_name(mine)
        check(name ~= nil, "our own script resolves to a name")
        if name then
            check(not name:match("^Script:"),
                  "the 'Script:' prefix is stripped: " .. name)
            check(not name:match("%.lua$"),
                  "the .lua extension is stripped: " .. name)
        end
    else
        bail("built-in not registered, cannot check its name")
    end

    -- Numeric resolution: OSC addresses cannot carry an SWS-style name.
    -- Measured: /action/_S&M_TOGLFXCHAIN never runs, while the same action
    -- fires fine through Main_OnCommand, and /action/<number> works.
    local numeric = actions.command_number("40044")
    eq(numeric, "40044", "a native ID resolves to itself")
    check(actions.command_number("_S&M_TOGLFXCHAIN") == nil
          or actions.command_number("_S&M_TOGLFXCHAIN"):match("^%d+$") ~= nil,
          "an SWS name resolves to digits, or to nothing if SWS is absent")
    if mine then
        local n = actions.command_number(mine)
        check(n ~= nil and n:match("^%d+$") ~= nil,
              "our own script ID resolves to digits: " .. tostring(n))
    end
    eq(actions.command_number("_RSdefinitelynotarealcommandid"), nil,
       "an unknown ID resolves to no number")
    eq(actions.command_number(""), nil, "empty ID")
    eq(actions.command_number(nil), nil, "nil ID")

    -- A typo must be reported, not silently bound to nothing.
    check(not actions.is_known("_RSdefinitelynotarealcommandid"),
          "an unknown command ID is reported as unknown")
    eq(actions.resolve_name("_RSdefinitelynotarealcommandid"), nil,
       "and gets no invented name")
end

cleanup()
print(string.format("\n%d checks, %d failures", checks, fails))
if os.exit then os.exit(fails == 0 and 0 or 1) end
