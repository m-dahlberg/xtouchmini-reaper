-- Load, save and resolve per-plugin mappings.
--
-- Keyed by plugin ident (fx_ident), not display name: the ident is stable
-- across instances and projects, which is exactly what makes a mapping global.
--
-- Every slot stores both a parameter index and the parameter's name. Plugins
-- reorder their parameters between versions, and a mapping that silently
-- starts driving the wrong control is worse than one that reports itself
-- broken -- so resolve() checks the name and searches for it before giving up.

local json = require "xt.json"
local paths = require "xt.paths"

local M = {}

M.LAYERS = {"A", "B"}
M.ENCODERS = 8
M.BUTTONS = 8
M.RESERVED = 8
M.VERSION = 1

-- --- fader settings ---------------------------------------------------------
--
-- Global, not per plugin: the fader drives track volume and has nothing to do
-- with what is focused. Stored here rather than in the daemon's config.toml
-- because the Fader tab is where they are edited, and the panel is the
-- project's single authority on what the daemon should be doing -- same
-- reasoning as every resolved index in state.json.
--
-- Adding this key does NOT bump VERSION. A load() that finds it missing fills
-- the defaults in, and the daemon does the same with the block state.json then
-- carries, so an older mappings.json keeps working and an older daemon ignores
-- what it does not know. Bumping the version would discard every mapping on
-- disk, which is a spectacular price for one new table.
M.FADER_MODES = {"absolute", "pickup", "relative"}

M.FADER_DEFAULTS = {
    mode = "pickup",
    -- One full sweep of the fader moves the volume by one full range, times
    -- this. Relative only.
    sensitivity = 1.0,
    -- Raw counts at each end that mean "out of road". Relative only.
    end_zone = 3,
    -- A walk-back that stops for this long is finished. Relative only.
    recal_timeout = 1.0,
}

local FADER_BOUNDS = {
    sensitivity = {0.1, 8.0},
    end_zone = {0, 32},
    recal_timeout = {0.05, 10.0},
}

local function is_mode(value)
    for _, mode in ipairs(M.FADER_MODES) do
        if value == mode then return true end
    end
    return false
end

--- The fader settings, with every missing or unusable field defaulted.
function M.fader(data)
    local raw = (type(data) == "table" and type(data.fader) == "table")
                and data.fader or {}
    local out = {}
    for key, fallback in pairs(M.FADER_DEFAULTS) do
        local value = raw[key]
        if key == "mode" then
            out[key] = is_mode(value) and value or fallback
        else
            value = tonumber(value)
            local lo, hi = FADER_BOUNDS[key][1], FADER_BOUNDS[key][2]
            out[key] = value and math.max(lo, math.min(hi, value)) or fallback
        end
    end
    return out
end

--- Set one fader setting, clamped. Returns true if it actually changed.
function M.set_fader(data, key, value)
    if M.FADER_DEFAULTS[key] == nil then return false end
    data.fader = M.fader(data)
    local was = data.fader[key]
    if key == "mode" then
        if not is_mode(value) then return false end
    else
        value = tonumber(value)
        if not value then return false end
        local lo, hi = FADER_BOUNDS[key][1], FADER_BOUNDS[key][2]
        value = math.max(lo, math.min(hi, value))
        if key == "end_zone" then value = math.floor(value + 0.5) end
    end
    data.fader[key] = value
    return was ~= value
end

local function empty_layer()
    return {encoders = {}, buttons = {}}
end

local function empty_plugin(display)
    return {
        display = display or "",
        layers = {A = empty_layer(), B = empty_layer()},
    }
end

function M.empty()
    return {version = M.VERSION, reserved = {}, plugins = {},
            fader = M.fader(nil)}
end

function M.load()
    local text = paths.read(paths.mappings())
    if not text then return M.empty() end
    local ok, data = json.decode(text)
    if not ok or type(data) ~= "table" or data.version ~= M.VERSION then
        return M.empty(), (ok and "unsupported version" or data)
    end
    data.plugins = data.plugins or {}
    data.reserved = data.reserved or {}
    data.fader = M.fader(data)
    return data
end

-- Slot lists must be written as dense arrays. A table holding only {[3]=slot}
-- has an undefined length in Lua, so the JSON encoder cannot tell it is an
-- array and writes it as an object -- whose keys are numbers, not strings, so
-- they are dropped and the mapping is silently lost on the next load.
-- resolve_slot already treats a null as unassigned, so filling the gaps costs
-- nothing elsewhere.
local function densify(entry)
    entry.layers = entry.layers or {}
    for _, layer in ipairs(M.LAYERS) do
        local block = entry.layers[layer] or empty_layer()
        for kind, count in pairs({encoders = M.ENCODERS, buttons = M.BUTTONS}) do
            local list, dense = block[kind] or {}, {}
            for i = 1, count do dense[i] = list[i] or json.NULL end
            block[kind] = dense
        end
        entry.layers[layer] = block
    end
end

function M.save(data)
    for _, entry in pairs(data.plugins or {}) do densify(entry) end
    return paths.write_atomic(paths.mappings(), json.encode(data, "  "))
end

function M.plugin(data, ident)
    return data.plugins[ident]
end

--- The stored slot at a position, or nil. Distinct from resolve_slot, which
-- returns the live view; this is what is actually on disk.
function M.stored_slot(data, ident, layer, kind, index)
    local entry = data.plugins[ident]
    if not entry or not entry.layers or not entry.layers[layer] then return nil end
    local slot = entry.layers[layer][kind][index]
    if type(slot) ~= "table" or type(slot.param) ~= "number" then return nil end
    return slot
end

function M.ensure(data, ident, display)
    local entry = data.plugins[ident]
    if not entry then
        entry = empty_plugin(display)
        data.plugins[ident] = entry
    elseif display and display ~= "" then
        entry.display = display
    end
    entry.layers = entry.layers or {}
    for _, layer in ipairs(M.LAYERS) do
        entry.layers[layer] = entry.layers[layer] or empty_layer()
    end
    return entry
end

--- Assign a control. kind is "encoders" or "buttons"; index is 1-based.
function M.set_slot(data, ident, display, layer, kind, index, slot)
    local entry = M.ensure(data, ident, display)
    entry.layers[layer][kind][index] = slot
    return entry
end

--- Set (or clear, with nil/"") the custom label on an existing slot.
function M.set_label(data, ident, layer, kind, index, label)
    local entry = data.plugins[ident]
    if not entry then return end
    local slot = entry.layers[layer][kind][index]
    if type(slot) ~= "table" or type(slot.param) ~= "number" then return end
    slot.label = (label and label ~= "") and label or nil
end

M.TAPERS = {"linear", "log", "exp"}

--- Set the response curve for a slot. taper is one of M.TAPERS.
function M.set_taper(data, ident, layer, kind, index, taper)
    local entry = data.plugins[ident]
    if not entry then return end
    local slot = entry.layers[layer][kind][index]
    if type(slot) ~= "table" or type(slot.param) ~= "number" then return end
    slot.taper = (taper and taper ~= "linear") and taper or nil
end

--- Set the per-slot speed multiplier. 1.0 is the default and is not stored.
function M.set_sensitivity(data, ident, layer, kind, index, value)
    local entry = data.plugins[ident]
    if not entry then return end
    local slot = entry.layers[layer][kind][index]
    if type(slot) ~= "table" or type(slot.param) ~= "number" then return end
    value = tonumber(value) or 1.0
    value = math.max(0.1, math.min(8.0, value))
    slot.sensitivity = (math.abs(value - 1.0) > 0.001) and value or nil
end

--- What to show for a slot: the user's label if there is one.
function M.display_name(slot)
    if type(slot) ~= "table" then return nil end
    if slot.label and slot.label ~= "" then return slot.label end
    return slot.name
end

function M.clear_slot(data, ident, layer, kind, index)
    local entry = data.plugins[ident]
    if not entry then return end
    entry.layers[layer][kind][index] = nil
end

-- REAPER's OSC addresses are 1-based; ReaScript's parameter indices are
-- 0-based. Every crossing of that boundary goes through these two, so the
-- off-by-one lives in one place instead of eight.
function M.to_osc_param(index) return index + 1 end
function M.from_osc_param(index) return index - 1 end

local function normalize(value, lo, hi)
    if hi == lo then return 0.0 end
    local n = (value - lo) / (hi - lo)
    return math.max(0.0, math.min(1.0, n))
end

--- Find a parameter by exact name, then case-insensitively. Returns index or nil.
function M.find_param_by_name(track, fx, name)
    if not name or name == "" then return nil end
    local count = reaper.TrackFX_GetNumParams(track, fx)
    local lowered = name:lower()
    local fallback
    for i = 0, count - 1 do
        local _, pname = reaper.TrackFX_GetParamName(track, fx, i, "")
        if pname == name then return i end
        if not fallback and pname:lower() == lowered then fallback = i end
    end
    return fallback
end

--- Turn one stored slot into the live form state.json carries.
-- Returns nil when the parameter cannot be located at all.
function M.resolve_slot(track, fx, stored)
    if type(stored) ~= "table" or type(stored.param) ~= "number" then
        return nil
    end
    local count = reaper.TrackFX_GetNumParams(track, fx)
    local index = stored.param
    local ok = index >= 0 and index < count
    if ok and stored.name and stored.name ~= "" then
        local _, actual = reaper.TrackFX_GetParamName(track, fx, index, "")
        if actual ~= stored.name then ok = false end
    end
    if not ok then
        index = M.find_param_by_name(track, fx, stored.name)
        if not index then return nil end
    end

    local raw, lo, hi, mid = reaper.TrackFX_GetParamEx(track, fx, index)
    local _, name = reaper.TrackFX_GetParamName(track, fx, index, "")

    -- The value MUST be REAPER's own normalised figure, not a linear
    -- normalisation of the raw one: REAPER applies the plugin's curve, and for
    -- anything non-linear the two disagree. Measured on ReaEQ's "Gain-Low
    -- Shelf": raw 0.250 in a 0..1 range is linear 0.25 but normalised 0.50.
    -- Seeding the shadow with 0.25 made the first detent snap the parameter to
    -- 0.5 -- the jump you see on ReaEQ but not on ReaComp, whose parameters
    -- happen to be linear.
    local value = reaper.TrackFX_GetParamNormalized(track, fx, index)

    -- The default is harder: GetParamEx reports the centre in RAW units and
    -- there is no API to push a raw value through the plugin's curve. Where
    -- the curve is linear (checked against the current value) the raw centre
    -- normalises directly; where it is not, 0.5 is the centre of the
    -- normalised range, which is what a gain or pan control resets to.
    local linear_now = normalize(raw, lo, hi)
    local default
    if math.abs(value - linear_now) < 0.001 then
        default = normalize(mid, lo, hi)
    else
        default = 0.5
    end

    return {
        param = index,
        name = name,
        -- A user-supplied label. Registered parameter names are often long and
        -- unhelpful ("Gain-Low Shelf"); the display reads this instead when it
        -- is set.
        label = stored.label,
        -- How a detent maps to a change in this parameter. See docs/taper.md.
        taper = stored.taper,
        sensitivity = stored.sensitivity,
        value = value,
        default = default,
        mode = stored.mode,
        moved = index ~= stored.param or nil,
    }
end

--- Resolve every slot of a plugin entry for a live FX instance.
function M.resolve(entry, track, fx)
    local out = {}
    for _, layer in ipairs(M.LAYERS) do
        local src = (entry and entry.layers and entry.layers[layer]) or empty_layer()
        local dst = {encoders = {}, buttons = {}}
        for kind, count in pairs({encoders = M.ENCODERS, buttons = M.BUTTONS}) do
            for i = 1, count do
                -- nil, not json.NULL: the sentinel is a table, so callers
                -- testing type(slot) == "table" would take an unassigned slot
                -- for a real one and then index a field that is not there.
                -- state.lua turns the holes back into nulls when it writes.
                dst[kind][i] = M.resolve_slot(track, fx, (src[kind] or {})[i])
            end
        end
        out[layer] = dst
    end
    return out
end

return M
