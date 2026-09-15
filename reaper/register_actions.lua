-- Register the panel and the reserved-row built-ins, and print their command
-- IDs. Run once with:  reaper -nonewinst reaper/register_actions.lua
--
-- The daemon fires these as /action/<id>, so the panel needs the IDs to offer
-- them in its reserved-row picker.

local names = {
    "xtouch_panel",
    "XT Bypass Focused FX",
    "XT Next FX",
    "XT Prev FX",
    "XT Layer A",
    "XT Layer B",
}

local base = reaper.GetResourcePath() .. "/Scripts/XtouchMini/"
local out = {}
for _, name in ipairs(names) do
    local id = reaper.AddRemoveReaScript(true, 0, base .. name .. ".lua", true)
    if id and id ~= 0 then
        -- Printed with the leading underscore, which is the form that
        -- /action/<id> and NamedCommandLookup both expect;
        -- ReverseNamedCommandLookup omits it.
        local named = reaper.ReverseNamedCommandLookup(id)
        out[#out + 1] = string.format("%-28s _%s", name, (named:gsub("^_", "")))
    else
        out[#out + 1] = string.format("%-28s FAILED (missing file?)", name)
    end
end
reaper.ShowConsoleMsg(table.concat(out, "\n") .. "\n")
