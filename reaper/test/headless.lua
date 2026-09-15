-- Pure-logic suite: json, mapping resolution, state building.
-- No project is touched. Run through the shared harness:
--   python3 ~/.claude/skills/reascript-lua/assets/reascript_test.py \
--           reaper/test/headless.lua

local here = debug.getinfo(1, "S").source:match("^@(.*[/\\])")
package.path = here .. "../?.lua;" .. package.path

local json = require "xt.json"
local mapping = require "xt.mapping"
local paths = require "xt.paths"

local fails, checks = 0, 0

local function check(cond, label)
    checks = checks + 1
    if not cond then
        fails = fails + 1
        print("FAIL: " .. label)
    end
end

local function eq(got, want, label)
    check(got == want, string.format("%s (got %s, want %s)",
          label, tostring(got), tostring(want)))
end

-- Any early return must count as a failure, not a silent pass.
local function bail(msg)
    fails = fails + 1
    print("BAIL: " .. msg)
end

-- --- json ------------------------------------------------------------------

do
    local cases = {
        {v = 42, name = "integer"},
        {v = -1, name = "negative"},
        {v = 0.25, name = "float"},
        {v = "plain", name = "string"},
        {v = 'quo"te\\back', name = "escapes"},
        {v = "new\nline\ttab", name = "control chars"},
        {v = true, name = "true"},
        {v = false, name = "false"},
    }
    for _, case in ipairs(cases) do
        local ok, back = json.decode(json.encode(case.v))
        check(ok, "decode " .. case.name)
        eq(back, case.v, "round-trip " .. case.name)
    end

    -- Integers must stay integers or a param index round-trips as 3.0.
    local ok, back = json.decode(json.encode({param = 3}))
    check(ok and math.type(back.param) == "integer", "integer stays integer")

    -- Arrays with holes: the whole reason NULL exists.
    local arr = {1, json.NULL, 3}
    local ok2, back2 = json.decode(json.encode(arr))
    check(ok2, "decode array with null")
    eq(#back2, 3, "null holds its position in an array")
    eq(back2[1], 1, "array element 1")
    eq(back2[3], 3, "array element 3")
    eq(back2[2], json.NULL, "null decodes to the sentinel")

    -- Nesting, which is why this is JSON and not the key=value file the
    -- ShuttleXpress bridge uses.
    local nested = {plugins = {["ReaComp.vst3"] = {
        layers = {A = {encoders = {{param = 12, name = "Thr"}}}}}}}
    local ok3, back3 = json.decode(json.encode(nested, "  "))
    check(ok3, "decode nested")
    if ok3 then
        eq(back3.plugins["ReaComp.vst3"].layers.A.encoders[1].param, 12,
           "nested param survives")
        eq(back3.plugins["ReaComp.vst3"].layers.A.encoders[1].name, "Thr",
           "nested name survives")
    else
        bail("nested decode failed: " .. tostring(back3))
    end

    eq(json.encode({}), "{}", "empty table is an object")
    eq(json.encode(json.EMPTY_ARRAY), "[]", "tagged empty array")

    for _, bad in ipairs({"", "{", "[1,", '{"a"}', "tru", "{'a':1}", "1 2"}) do
        local okb = json.decode(bad)
        check(not okb, "rejects malformed: " .. bad)
    end

    -- Keys are sorted, so a mapping file only changes when a mapping changes,
    -- which keeps a diff of it readable.
    eq(json.encode({b = 1, a = 2}), '{"a": 2,"b": 1}', "keys are sorted")
end

-- --- mapping resolution ----------------------------------------------------

do
    -- Stub the FX API. mapping.resolve is the piece that has to survive a
    -- plugin reordering its parameters, and that is worth testing without
    -- needing a real plugin that reorders itself.
    local params = {"Threshold", "Ratio", "Attack"}
    local real = {
        GetNumParams = reaper.TrackFX_GetNumParams,
        GetParamName = reaper.TrackFX_GetParamName,
        GetParamEx = reaper.TrackFX_GetParamEx,
        GetParamNormalized = reaper.TrackFX_GetParamNormalized,
    }
    -- raw 0.5 in a 0..1 range, centre 0.25.
    local normalized = 0.5          -- equals the linear figure: linear plugin
    reaper.TrackFX_GetNumParams = function() return #params end
    reaper.TrackFX_GetParamName = function(_, _, i) return true, params[i + 1] or "" end
    reaper.TrackFX_GetParamEx = function(_, _, i) return 0.5, 0.0, 1.0, 0.25 end
    reaper.TrackFX_GetParamNormalized = function() return normalized end

    local slot = mapping.resolve_slot(nil, 0, {param = 0, name = "Threshold"})
    check(slot ~= nil, "resolves a matching slot")
    if slot then
        eq(slot.param, 0, "index unchanged when the name matches")
        eq(slot.name, "Threshold", "name reported")
        eq(slot.value, 0.5, "value normalised")
        eq(slot.default, 0.25, "default from the reported centre")
    end

    -- The parameter moved: stored index 0 now holds a different name.
    params = {"Ratio", "Attack", "Threshold"}
    local moved = mapping.resolve_slot(nil, 0, {param = 0, name = "Threshold"})
    check(moved ~= nil, "finds a moved parameter by name")
    if moved then
        eq(moved.param, 2, "follows the name to its new index")
        eq(moved.moved, true, "flags that it moved")
    end

    -- Gone entirely: must report broken rather than drive index 0.
    local gone = mapping.resolve_slot(nil, 0, {param = 0, name = "Nonexistent"})
    eq(gone, nil, "a vanished parameter resolves to nil, not to index 0")

    -- Out of range with no name to search by.
    local oor = mapping.resolve_slot(nil, 0, {param = 99, name = ""})
    eq(oor, nil, "out-of-range index with no name is nil")

    eq(mapping.find_param_by_name(nil, 0, "attack"), 1,
       "name search is case-insensitive as a fallback")

    -- OSC is 1-based, ReaScript is 0-based; the conversion lives in one place.
    eq(mapping.to_osc_param(0), 1, "param 0 -> OSC 1")
    eq(mapping.from_osc_param(1), 0, "OSC 1 -> param 0")

    -- A plugin whose curve is non-linear: REAPER's normalised value disagrees
    -- with the linear one. The value must follow REAPER, and the centre falls
    -- back to 0.5, because a raw centre cannot be pushed through the curve.
    params = {"Threshold", "Ratio", "Attack"}
    normalized = 0.8
    local curved = mapping.resolve_slot(nil, 0, {param = 0, name = "Threshold"})
    check(curved ~= nil, "resolves a non-linear parameter")
    if curved then
        eq(curved.value, 0.8, "value follows REAPER's normalisation")
        eq(curved.default, 0.5, "centre falls back to the normalised middle")
    end
    normalized = 0.5

    reaper.TrackFX_GetNumParams = real.GetNumParams
    reaper.TrackFX_GetParamName = real.GetParamName
    reaper.TrackFX_GetParamEx = real.GetParamEx
    reaper.TrackFX_GetParamNormalized = real.GetParamNormalized
end

-- --- mapping store ---------------------------------------------------------

do
    local data = mapping.empty()
    mapping.set_slot(data, "ReaComp.vst3", "ReaComp", "A", "encoders", 1,
                     {param = 12, name = "Thr"})
    eq(data.plugins["ReaComp.vst3"].layers.A.encoders[1].param, 12,
       "set_slot stores")
    eq(data.plugins["ReaComp.vst3"].display, "ReaComp", "display recorded")

    -- Both layers exist even if only one was written, so the panel can draw
    -- them side by side without nil checks everywhere.
    check(data.plugins["ReaComp.vst3"].layers.B ~= nil, "layer B created")

    mapping.clear_slot(data, "ReaComp.vst3", "A", "encoders", 1)
    eq(data.plugins["ReaComp.vst3"].layers.A.encoders[1], nil, "clear_slot")
    mapping.clear_slot(data, "nope", "A", "encoders", 1)  -- must not raise

    local ok, back = json.decode(json.encode(data, "  "))
    check(ok, "mapping store round-trips through json")

    -- A slot assigned to a gap: {[3]=slot} has an undefined length in Lua, so
    -- an un-densified save writes it as an object with numeric keys, which the
    -- encoder drops entirely. That loses the mapping with no error at all.
    local sparse = mapping.empty()
    mapping.set_slot(sparse, "P", "P", "A", "encoders", 3,
                     {param = 7, name = "Gap"})
    local captured
    local real_write = paths.write_atomic
    paths.write_atomic = function(_, text) captured = text; return true end
    mapping.save(sparse)
    paths.write_atomic = real_write

    local ok2, back2 = json.decode(captured)
    check(ok2, "sparse mapping serialises")
    if ok2 then
        local encs = back2.plugins.P.layers.A.encoders
        eq(type(encs), "table", "encoders survives as a table")
        eq(#encs, mapping.ENCODERS, "written as a dense 8-slot array")
        eq(encs[3] ~= json.NULL and encs[3].param or nil, 7,
           "the slot in the gap survives the round-trip")
        eq(encs[1], json.NULL, "empty slots are nulls, not missing keys")
    end
end

-- --- volume scales ---------------------------------------------------------

do
    local volume = require "xt.volume"

    -- REAPER's real taper, so the round trips below mean something. The
    -- constants are REAPER's: DB2SLIDER maps -150..+12 dB onto 0..1000.
    local real_db2s, real_s2db = reaper.DB2SLIDER, reaper.SLIDER2DB
    check(real_db2s ~= nil, "DB2SLIDER is available")
    check(real_s2db ~= nil, "SLIDER2DB is available")

    local function close(a, b, tol, label)
        check(math.abs(a - b) <= (tol or 0.001),
              string.format("%s (got %.6f, want %.6f)", label, a, b))
    end

    close(volume.gain_to_db(1.0), 0.0, 0.001, "unity gain is 0 dB")
    close(volume.gain_to_db(2.0), 6.0206, 0.001, "double is +6 dB")
    close(volume.gain_to_db(0.5), -6.0206, 0.001, "half is -6 dB")
    eq(volume.gain_to_db(0), volume.MIN_DB, "silence floors rather than -inf")
    eq(volume.gain_to_db(-1), volume.MIN_DB, "a negative gain floors too")

    close(volume.db_to_gain(0), 1.0, 0.001, "0 dB is unity")
    close(volume.db_to_gain(6.0206), 2.0, 0.001, "+6 dB doubles")
    eq(volume.db_to_gain(volume.MIN_DB), 0.0, "the floor is silence")

    -- The number that matters: unity must NOT come out as 0.25 (D_VOL/4, the
    -- scale the panel used to publish) and must not be 0.5 either.
    local unity = volume.gain_to_slider(1.0)
    close(unity, 0.716, 0.01, "unity gain is 0.716 of the way up the fader")
    check(math.abs(unity - 0.25) > 0.1,
          "unity is NOT D_VOL/4 -- that was the scale bug")

    check(volume.gain_to_slider(0) >= 0.0, "silence stays in range")
    check(volume.gain_to_slider(1000) <= 1.0, "a huge gain clamps to 1.0")

    -- Round trip, which is what makes the conversion safe to use in anger.
    for _, gain in ipairs({0.001, 0.05, 0.25, 0.5, 1.0, 2.0, 3.98}) do
        local back = volume.slider_to_gain(volume.gain_to_slider(gain))
        close(back, gain, math.max(0.001, gain * 0.01),
              string.format("gain %.3f survives the round trip", gain))
    end

    eq(volume.slider_to_text(volume.gain_to_slider(1.0)), "+0.0 dB",
       "unity reads as 0 dB")
    eq(volume.slider_to_text(0), "-inf dB", "the bottom reads as -inf")
end

-- --- fader settings --------------------------------------------------------

do
    local statefile = require "xt.state"

    eq(mapping.fader(nil).mode, "pickup", "no data defaults to soft pickup")
    eq(mapping.fader({}).mode, "pickup", "no fader block defaults too")

    local data = mapping.empty()
    eq(mapping.fader(data).end_zone, 3, "default end zone")
    eq(mapping.fader(data).sensitivity, 1.0, "default sensitivity")
    eq(mapping.fader(data).recal_timeout, 1.0, "default walk-back timeout")

    check(mapping.set_fader(data, "mode", "relative"), "mode change reports it")
    eq(mapping.fader(data).mode, "relative", "mode stored")
    check(not mapping.set_fader(data, "mode", "relative"),
          "setting the same mode reports no change")
    check(not mapping.set_fader(data, "mode", "sideways"),
          "an unknown mode is refused")
    eq(mapping.fader(data).mode, "relative", "and leaves the stored one alone")
    check(not mapping.set_fader(data, "nonsense", 1), "unknown key refused")

    -- Clamping, because the sliders are not the only way in: a hand-edited
    -- mappings.json reaches exactly the same code.
    mapping.set_fader(data, "sensitivity", 99)
    eq(mapping.fader(data).sensitivity, 8.0, "sensitivity clamps high")
    mapping.set_fader(data, "sensitivity", 0)
    eq(mapping.fader(data).sensitivity, 0.1, "sensitivity clamps low")
    mapping.set_fader(data, "end_zone", -4)
    eq(mapping.fader(data).end_zone, 0, "end zone clamps at zero")
    mapping.set_fader(data, "end_zone", 2.6)
    eq(mapping.fader(data).end_zone, 3, "end zone is a whole number of steps")

    -- A garbage value on disk must default rather than propagate: the daemon
    -- would clamp it too, but the panel would go on displaying the garbage.
    eq(mapping.fader({fader = {sensitivity = "loud"}}).sensitivity, 1.0,
       "an unparseable value falls back to the default")
    eq(mapping.fader({fader = "not a table"}).mode, "pickup",
       "a fader key of the wrong type defaults the lot")

    -- The whole block goes into state.json every time. A partial one would
    -- read to the daemon as "default everything I left out".
    local doc = statefile.build(nil, nil, {index = 1, name = "T", volume = 0.5},
                                {}, data.fader)
    eq(type(doc.fader), "table", "state.json carries the fader block")
    eq(doc.fader.mode, "relative", "mode reaches the daemon")
    for key in pairs(mapping.FADER_DEFAULTS) do
        check(doc.fader[key] ~= nil, "state.json carries fader." .. key)
    end

    -- Nothing published yet must still be a complete, valid block.
    local bare = statefile.build(nil, nil, {index = 1, name = "T", volume = 0},
                                 {}, nil)
    eq(bare.fader.mode, "pickup", "an unset fader publishes the default mode")
end

print(string.format("\n%d checks, %d failures", checks, fails))
if os.exit then os.exit(fails == 0 and 0 or 1) end
