-- Command IDs for the reserved-row built-ins.
--
-- Resolved by registering the scripts ourselves rather than asking the user to
-- run register_actions.lua and paste IDs in: AddRemoveReaScript is idempotent,
-- so doing it at panel start costs nothing and removes a manual step that
-- would otherwise silently leave the reserved row unbound.

local M = {}

-- label -> script filename (without .lua), in the order the picker shows them.
M.BUILTINS = {
    {id = "bypass", label = "Bypass focused FX", script = "XT Bypass Focused FX"},
    {id = "next_fx", label = "Next FX in chain", script = "XT Next FX"},
    {id = "prev_fx", label = "Previous FX in chain", script = "XT Prev FX"},
}

-- Fired by the daemon when the hardware layer button is pressed. Not offered
-- in the reserved-row picker: these exist so the device can tell the panel
-- something, which OSC can only do by triggering an action.
M.NOTIFY = {
    {id = "layer_a", label = "Layer A", script = "XT Layer A"},
    {id = "layer_b", label = "Layer B", script = "XT Layer B"},
}

local resolved = nil

--- Map each built-in id to its REAPER named command ID ("_RS...").
function M.resolve(script_dir)
    if resolved then return resolved end
    resolved = {}
    local all = {}
    for _, e in ipairs(M.BUILTINS) do all[#all + 1] = e end
    for _, e in ipairs(M.NOTIFY) do all[#all + 1] = e end
    for _, entry in ipairs(all) do
        local path = script_dir .. entry.script .. ".lua"
        local ok, command = pcall(reaper.AddRemoveReaScript, true, 0, path, true)
        if ok and command and command ~= 0 then
            local named = reaper.ReverseNamedCommandLookup(command)
            if named and named ~= "" then
                -- ReverseNamedCommandLookup returns the id WITHOUT its
                -- leading underscore ("RSfa17..."), but /action/ needs it.
                -- Without this every reserved button silently does nothing.
                resolved[entry.id] = named:sub(1, 1) == "_" and named
                                     or ("_" .. named)
            end
        end
    end
    return resolved
end

--- The named command ID for a built-in, or nil if it could not be registered.
function M.command_for(builtin_id)
    return resolved and resolved[builtin_id] or nil
end

function M.label_for(builtin_id)
    for _, entry in ipairs(M.BUILTINS) do
        if entry.id == builtin_id then return entry.label end
    end
    return builtin_id
end

--- The human name of a command ID, or nil if REAPER does not know it.
-- Works for both a native number ("40044") and a script ID ("_RS...").
function M.resolve_name(id)
    if type(id) ~= "string" or id == "" then return nil end
    local ok, command = pcall(reaper.NamedCommandLookup, id)
    if not ok or not command or command == 0 then return nil end
    if not reaper.APIExists("CF_GetCommandText") then return nil end
    local got, name = pcall(reaper.CF_GetCommandText, 0, command)
    if not got or type(name) ~= "string" or name == "" then return nil end
    -- "Script: XT Bypass Focused FX.lua" reads better as the bare name.
    name = name:gsub("^Script:%s*", ""):gsub("%.lua$", "")
    return name
end

--- The numeric command ID for a named one, as a string, or nil.
--
-- OSC addresses cannot carry every named command ID: SWS actions are named
-- like "_S&M_TOGLFXCHAIN", and REAPER's OSC action lookup does not resolve an
-- address containing "&" -- measured, the action simply never runs while the
-- same ID works through Main_OnCommand. Numbers are always safe.
--
-- The number is resolved fresh every time rather than stored: for scripts it
-- is assigned at registration and is not stable between sessions, so only the
-- named form belongs on disk.
function M.command_number(id)
    if type(id) ~= "string" or id == "" then return nil end
    local ok, command = pcall(reaper.NamedCommandLookup, id)
    if not ok or not command or command == 0 then return nil end
    return tostring(command)
end

--- Does REAPER recognise this command ID? A typo would otherwise bind a
-- button to nothing at all, silently.
function M.is_known(id)
    if type(id) ~= "string" or id == "" then return false end
    local ok, command = pcall(reaper.NamedCommandLookup, id)
    return ok and command ~= nil and command ~= 0
end

--- Fill in any missing reserved labels from REAPER's own action names.
-- Returns true if anything changed, so the caller knows to save.
function M.fill_labels(reserved)
    local changed = false
    for _, binding in pairs(reserved or {}) do
        if type(binding) == "table" and binding.id and binding.id ~= ""
           and (binding.label == nil or binding.label == "") then
            local name = M.resolve_name(binding.id)
            if name then
                binding.label = name
                changed = true
            end
        end
    end
    return changed
end

function M.reset() resolved = nil end

return M
