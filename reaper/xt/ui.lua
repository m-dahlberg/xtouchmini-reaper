-- The docked panel: what the controller is doing, right now.
--
-- The point of the whole project. The X-Touch has no display, so this is the
-- legend -- click a slot here, touch the control in the plugin, done.
--
-- Four tabs:
--   Surface   map and label every slot on both layers
--   Reserved  the eight global buttons
--   Fader     the selected track's level, and how the fader drives it
--   Display   a wide, big-type readout that mirrors the device's layout,
--             meant to be docked along the bottom of the screen
--
-- Follows the house panel pattern: _init does everything except the defer
-- loop, _frame draws exactly one frame and is pcall'd by the caller, so a
-- stub-ImGui test can drive it without REAPER.

local mapping = require "xt.mapping"
local actions = require "xt.actions"
local focus = require "xt.focus"
local learn = require "xt.learn"
local statefile = require "xt.state"
local volume = require "xt.volume"

local M = {}

local ImGui, ctx, script_dir
local WIN_FLAGS, TABLE_FLAGS, GRID_FLAGS, COL_FIXED, COL_STRETCH
local FONT_BIG

-- The Display tab's type, and the row height that goes with it. One line per
-- slot, and the height is pinned rather than left to the content: an unmapped
-- slot holds exactly as much room as a mapped one, so the tab does not grow
-- and shrink under the arrange view as plugins come and go.
local DISPLAY_FONT = 17
local DISPLAY_ROW_H = DISPLAY_FONT + 7

-- How long the panel stays "ours" after the pointer leaves it. Clicking a slot
-- and then reaching for the plugin's control must not blank the panel
-- mid-gesture.
local PANEL_GRACE = 1.5

-- The daemon writes this when the device's layer button is pressed.
local EXT_SECTION, EXT_LAYER = "xtouch_mini", "layer"

local ST = {
    touched_at = 0,
    data = nil,          -- mappings.json
    ctx = nil,           -- active focus context
    layers = nil,        -- resolved slots for the active plugin
    err = nil,
    close = false,
    status = "",
}

local COL_DIM    = 0x808080FF
local COL_ACTIVE = 0x6FCF97FF
local COL_ARMED  = 0xF2C94CFF
local COL_RED    = 0xEB5757FF
local COL_LABEL  = 0xD0D0D0FF
local COL_VALUE  = 0x9BD1FFFF

-- The imgui shim raises on an unknown field rather than returning nil, so
-- `ImGui.A or ImGui.B` never works. Probe instead.
local function opt(name)
    local ok, v = pcall(function() return ImGui[name] end)
    if ok then return v end
    return nil
end

-- Counted push/pop. Tables, tabs and fonts are counted too: an error thrown
-- inside any of them otherwise leaves it open, and ImGui.End reports the
-- imbalance over the top of the real exception.
local depth = {style = 0, colour = 0, disabled = 0,
               table = 0, tab_item = 0, tab_bar = 0, font = 0}

local function push_colour(which, value)
    ImGui.PushStyleColor(ctx, which, value)
    depth.colour = depth.colour + 1
end

local function pop_colour(n)
    n = n or 1
    ImGui.PopStyleColor(ctx, n)
    depth.colour = depth.colour - n
end

local function begin_table(id, cols)
    if ImGui.BeginTable(ctx, id, cols, TABLE_FLAGS) then
        depth.table = depth.table + 1
        return true
    end
    return false
end

local function end_table()
    ImGui.EndTable(ctx)
    depth.table = depth.table - 1
end

local function begin_tab_bar(id)
    if ImGui.BeginTabBar(ctx, id) then
        depth.tab_bar = depth.tab_bar + 1
        return true
    end
    return false
end

local function end_tab_bar()
    ImGui.EndTabBar(ctx)
    depth.tab_bar = depth.tab_bar - 1
end

local function begin_tab_item(label)
    if ImGui.BeginTabItem(ctx, label) then
        depth.tab_item = depth.tab_item + 1
        return true
    end
    return false
end

local function end_tab_item()
    ImGui.EndTabItem(ctx)
    depth.tab_item = depth.tab_item - 1
end

local function begin_disabled()
    ImGui.BeginDisabled(ctx, true)
    depth.disabled = depth.disabled + 1
end

local function end_disabled()
    ImGui.EndDisabled(ctx)
    depth.disabled = depth.disabled - 1
end

-- The house tooltip idiom in one place, because a tooltip attaches to the
-- widget before it and getting that order wrong is invisible until hovered.
local function set_tooltip(text)
    if ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, text) end
end

local function push_font(font)
    if not font then return false end
    local ok = pcall(ImGui.PushFont, ctx, font)
    if ok then depth.font = depth.font + 1 end
    return ok
end

local function pop_font()
    if depth.font > 0 then
        ImGui.PopFont(ctx)
        depth.font = depth.font - 1
    end
end

-- Innermost first: tables live inside tab items, which live inside the bar.
local function unwind()
    if depth.colour > 0 then ImGui.PopStyleColor(ctx, depth.colour) end
    if depth.style > 0 then ImGui.PopStyleVar(ctx, depth.style) end
    while depth.disabled > 0 do
        ImGui.EndDisabled(ctx); depth.disabled = depth.disabled - 1
    end
    while depth.font > 0 do
        ImGui.PopFont(ctx); depth.font = depth.font - 1
    end
    while depth.table > 0 do
        ImGui.EndTable(ctx); depth.table = depth.table - 1
    end
    while depth.tab_item > 0 do
        ImGui.EndTabItem(ctx); depth.tab_item = depth.tab_item - 1
    end
    while depth.tab_bar > 0 do
        ImGui.EndTabBar(ctx); depth.tab_bar = depth.tab_bar - 1
    end
    depth.colour, depth.style = 0, 0
end

-- --- data ------------------------------------------------------------------

local function reload()
    local data, err = mapping.load()
    ST.data = data
    if err then ST.status = "mappings.json: " .. tostring(err) end
end

local function save()
    local ok, err = mapping.save(ST.data)
    ST.status = ok and "saved" or ("save failed: " .. tostring(err))
end

local function selected_track()
    local track = reaper.GetSelectedTrack(0, 0) or reaper.GetMasterTrack(0)
    if not track then return {index = -1, name = "", volume = 0.0} end
    local index = math.floor(
        reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER"))
    local _, name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
    -- As a FADER POSITION, not as the raw D_VOL gain factor: state.json's
    -- volume has to be in the same scale the daemon sends on and receives
    -- feedback in, or its shadow flips between two of them. See xt.volume.
    local gain = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    return {
        index = index,
        name = name or "",
        volume = volume.gain_to_slider(gain),
    }
end

local function value_text(slot)
    if not slot or not ST.ctx then return "" end
    -- pcall prepends its own success flag, so the REAPER call's own retval is
    -- the SECOND result and the string is the third. Reading `ok, text` here
    -- put the boolean retval into `text` and handed it to ImGui.Text, which
    -- real ImGui rejects.
    local ok, retval, text = pcall(reaper.TrackFX_GetFormattedParamValue,
                                   ST.ctx.track, ST.ctx.fx, slot.param, "")
    if ok and retval and type(text) == "string" and text ~= "" then
        return text
    end
    return string.format("%.2f", slot.value or 0)
end

--- Which layer the hardware is on. The daemon sets this when the device's
-- layer button is pressed; there is no other way to know.
local function device_layer()
    local value = reaper.GetExtState(EXT_SECTION, EXT_LAYER)
    return value == "B" and "B" or "A"
end

-- Build the \0-separated combo strings rather than writing them as literals.
-- In Lua "\0" followed by a DIGIT is read as a multi-digit decimal escape, so
-- "0.25x\00.5x\01x\0" silently becomes "0.25x" \0 ".5x" \1 "x" -- one usable
-- entry and then garbage.
local function menu(labels)
    return table.concat(labels, "\0") .. "\0"
end

local TAPER_VALUES = {"linear", "log", "exp"}
local TAPER_ITEMS = menu(TAPER_VALUES)
local SENS_VALUES = {0.25, 0.5, 1.0, 2.0, 4.0}
local SENS_ITEMS = menu({"0.25x", "0.5x", "1x", "2x", "4x"})

local function index_of(list, want, default)
    for i, v in ipairs(list) do
        if v == want then return i - 1 end
    end
    return default
end

local function sens_index(value)
    value = tonumber(value) or 1.0
    local best, bestd = 2, math.huge
    for i, v in ipairs(SENS_VALUES) do
        local d = math.abs(v - value)
        if d < bestd then best, bestd = i - 1, d end
    end
    return best
end

-- --- surface tab -----------------------------------------------------------

local function slot_row(layer, kind, index, slot)
    local armed = learn.is_armed(layer, kind, index)

    ImGui.TableNextRow(ctx)

    ImGui.TableNextColumn(ctx)
    push_colour(ImGui.Col_Text, COL_DIM)
    ImGui.Text(ctx, tostring(index))
    pop_colour()

    -- The click target is the parameter cell alone, NOT the whole row: a
    -- selectable spanning all columns sits on top of the label field and eats
    -- its clicks, so the field can never be typed into.
    ImGui.TableNextColumn(ctx)
    local shown
    if armed then
        shown = "waiting -- touch a control"
        push_colour(ImGui.Col_Text, COL_ARMED)
    elseif slot then
        shown = slot.name
        push_colour(ImGui.Col_Text, COL_ACTIVE)
    else
        shown = "--"
        push_colour(ImGui.Col_Text, COL_DIM)
    end

    local id = string.format("%s##%s.%s.%d", shown, layer, kind, index)
    if ImGui.Selectable(ctx, id, armed) then
        if armed then
            learn.cancel()
            ST.status = "learn cancelled"
        elseif not ST.ctx then
            ST.status = "no plugin focused -- open one first"
        else
            learn.arm(layer, kind, index)
            ST.status = "touch a control in the plugin"
        end
    end
    pop_colour()

    if ImGui.IsItemClicked(ctx, 1) and slot and ST.ctx then
        mapping.clear_slot(ST.data, ST.ctx.ident, layer, kind, index)
        save()
        ST.status = "cleared"
    end

    if ImGui.IsItemHovered(ctx) then
        ImGui.SetTooltip(ctx, slot
            and string.format("%s\nparameter %d\n\nleft-click to remap"
                              .. "\nright-click to clear", slot.name, slot.param)
            or "left-click, then touch a control in the plugin")
    end

    -- Custom label. Registered parameter names are often long and unhelpful,
    -- and this is what the Display tab shows when it is set.
    ImGui.TableNextColumn(ctx)
    if slot and ST.ctx then
        ImGui.SetNextItemWidth(ctx, -1)
        local field = string.format("##lbl.%s.%s.%d", layer, kind, index)
        local edited, text = ImGui.InputText(ctx, field, slot.label or "")
        if edited then
            mapping.set_label(ST.data, ST.ctx.ident, layer, kind, index, text)
        end
        -- Saving on every keystroke would rewrite the file per character.
        if opt("IsItemDeactivatedAfterEdit")
           and ImGui.IsItemDeactivatedAfterEdit(ctx) then
            save()
        end
    else
        ImGui.Text(ctx, "")
    end

    -- Feel: how a detent maps to a change in this parameter. A fixed step in
    -- the normalised domain is not a fixed step in what the parameter means.
    ImGui.TableNextColumn(ctx)
    if slot and ST.ctx then
        ImGui.PushID(ctx, layer .. kind .. index)
        ImGui.SetNextItemWidth(ctx, 78)
        local ti = index_of(TAPER_VALUES, slot.taper or "linear", 0)
        local tchanged, tpicked = ImGui.Combo(ctx, "##taper", ti, TAPER_ITEMS)
        if tchanged then
            mapping.set_taper(ST.data, ST.ctx.ident, layer, kind, index,
                              TAPER_VALUES[tpicked + 1])
            save()
        end
        ImGui.SameLine(ctx)
        ImGui.SetNextItemWidth(ctx, 66)
        local si = sens_index(slot.sensitivity)
        local schanged, spicked = ImGui.Combo(ctx, "##sens", si, SENS_ITEMS)
        if schanged then
            mapping.set_sensitivity(ST.data, ST.ctx.ident, layer, kind, index,
                                    SENS_VALUES[spicked + 1])
            save()
        end
        ImGui.PopID(ctx)
    else
        ImGui.Text(ctx, "")
    end

    ImGui.TableNextColumn(ctx)
    push_colour(ImGui.Col_Text, COL_LABEL)
    ImGui.Text(ctx, (slot and not armed) and value_text(slot) or "")
    pop_colour()
end

local function slot_table(id, layer, kind, count, list)
    if not begin_table(id, 5) then return end
    ImGui.TableSetupColumn(ctx, "#", COL_FIXED, 24)
    ImGui.TableSetupColumn(ctx, "Parameter", COL_STRETCH)
    ImGui.TableSetupColumn(ctx, "Label", COL_STRETCH)
    ImGui.TableSetupColumn(ctx, "Feel", COL_FIXED, 156)
    ImGui.TableSetupColumn(ctx, "Value", COL_FIXED, 90)
    ImGui.TableHeadersRow(ctx)
    for i = 1, count do
        slot_row(layer, kind, i, list[i])
    end
    end_table()
end

local function layer_block(layer)
    local block = ST.layers and ST.layers[layer] or {encoders = {}, buttons = {}}
    local live = (device_layer() == layer) and "   [live]" or ""

    ImGui.SeparatorText(ctx, "Layer " .. layer .. " -- encoders" .. live)
    slot_table("enc" .. layer, layer, "encoders", mapping.ENCODERS,
               block.encoders or {})

    ImGui.SeparatorText(ctx, "Layer " .. layer .. " -- buttons 1-8")
    slot_table("btn" .. layer, layer, "buttons", mapping.BUTTONS,
               block.buttons or {})
end

-- --- display tab -----------------------------------------------------------

local function display_cell(slot)
    ImGui.TableNextColumn(ctx)
    if not slot then
        push_colour(ImGui.Col_Text, COL_DIM)
        ImGui.Text(ctx, "--")
        pop_colour()
        return
    end
    push_colour(ImGui.Col_Text, COL_ACTIVE)
    ImGui.Text(ctx, mapping.display_name(slot) or "")
    pop_colour()
    local value = value_text(slot)
    if value ~= "" then
        ImGui.SameLine(ctx)
        push_colour(ImGui.Col_Text, COL_VALUE)
        ImGui.Text(ctx, value)
        pop_colour()
    end
end

local function grid(id, kind, count, list, headers)
    if not ImGui.BeginTable(ctx, id, count, GRID_FLAGS) then return end
    depth.table = depth.table + 1
    for i = 1, count do
        ImGui.TableSetupColumn(ctx, tostring(i), COL_STRETCH, 1.0)
    end
    if headers then ImGui.TableHeadersRow(ctx) end
    ImGui.TableNextRow(ctx, 0, DISPLAY_ROW_H)
    for i = 1, count do
        display_cell(list[i])
    end
    end_table()
end

--- A wide readout matching the device's physical layout: one row of encoders
-- above one row of buttons. Follows the hardware's own layer, so what is on
-- screen is what is under your hands. Kept to the two grid rows and nothing
-- else: this tab sits under REAPER's transport, where every line of chrome is
-- a line of arrange view. The column numbers are drawn once, at the top --
-- the buttons sit in the same columns as the encoders above them.
local function display_tab()
    local layer = device_layer()
    local block = ST.layers and ST.layers[layer] or {encoders = {}, buttons = {}}

    push_font(FONT_BIG)
    grid("disp_enc", "encoders", mapping.ENCODERS, block.encoders or {}, true)
    grid("disp_btn", "buttons", mapping.BUTTONS, block.buttons or {}, false)
    pop_font()
end

-- --- reserved tab ----------------------------------------------------------

--- Buttons 9-16: a text field for a REAPER action command ID, and a label.
-- A picker was worse: the useful actions are ones you find in REAPER's action
-- list and copy the ID of, and no dropdown can enumerate those. The label
-- fills itself in from REAPER's own action name and stays editable, because
-- "Transport: Play/stop" is a fine starting point but "Play" is what you want
-- on a legend.
local function reserved_tab()
    ImGui.Text(ctx, "Buttons 9-16. Global -- these stay live with no plugin "
                 .. "focused.")
    ImGui.Text(ctx, "Paste a command ID: _RS... for a script, or a number "
                 .. "like 40044 for a built-in action.")
    ImGui.Separator(ctx)

    ST.data.reserved = ST.data.reserved or {}

    if begin_table("reserved", 3) then
        ImGui.TableSetupColumn(ctx, "#", COL_FIXED, 32)
        ImGui.TableSetupColumn(ctx, "Action ID", COL_STRETCH)
        ImGui.TableSetupColumn(ctx, "Label", COL_STRETCH)
        ImGui.TableHeadersRow(ctx)

        for i = 1, mapping.RESERVED do
            local binding = ST.data.reserved[i]
            if type(binding) ~= "table" then binding = nil end

            ImGui.TableNextRow(ctx)
            ImGui.PushID(ctx, i)

            ImGui.TableNextColumn(ctx)
            push_colour(ImGui.Col_Text, COL_DIM)
            ImGui.Text(ctx, tostring(i + mapping.ENCODERS))
            pop_colour()

            -- Action ID
            ImGui.TableNextColumn(ctx)
            local known = binding and actions.is_known(binding.id)
            if binding and not known then
                push_colour(ImGui.Col_Text, COL_RED)
            end
            ImGui.SetNextItemWidth(ctx, -1)
            local edited, text = ImGui.InputText(ctx, "##res",
                                                 binding and binding.id or "")
            if binding and not known then pop_colour() end

            if edited then
                text = text:match("^%s*(.-)%s*$")
                if text == "" then
                    ST.data.reserved[i] = nil
                else
                    local was = binding and binding.id
                    ST.data.reserved[i] = {
                        kind = "action", id = text,
                        -- A label written for one action must not survive
                        -- being pointed at a different one.
                        label = (was == text) and binding.label or nil,
                    }
                end
            end
            if opt("IsItemDeactivatedAfterEdit")
               and ImGui.IsItemDeactivatedAfterEdit(ctx) then
                actions.fill_labels(ST.data.reserved)
                save()
            end
            if binding and not known and ImGui.IsItemHovered(ctx) then
                ImGui.SetTooltip(ctx, "REAPER does not know this command ID")
            end

            -- Label
            ImGui.TableNextColumn(ctx)
            binding = ST.data.reserved[i]
            if type(binding) == "table" then
                ImGui.SetNextItemWidth(ctx, -1)
                local ledit, ltext = ImGui.InputText(ctx, "##reslbl",
                                                     binding.label or "")
                if ledit then binding.label = ltext end
                if opt("IsItemDeactivatedAfterEdit")
                   and ImGui.IsItemDeactivatedAfterEdit(ctx) then
                    save()
                end
            else
                ImGui.Text(ctx, "")
            end

            ImGui.PopID(ctx)
        end
        end_table()
    end

    -- The built-ins are registered scripts, so their IDs are only knowable at
    -- run time. List them here to be copied rather than hiding them behind a
    -- picker that could not offer anything else.
    ImGui.SeparatorText(ctx, "This project's actions -- copy an ID above")
    if begin_table("builtins", 2) then
        ImGui.TableSetupColumn(ctx, "Action", COL_STRETCH)
        ImGui.TableSetupColumn(ctx, "Command ID", COL_STRETCH)
        ImGui.TableHeadersRow(ctx)
        for _, entry in ipairs(actions.BUILTINS) do
            ImGui.TableNextRow(ctx)
            ImGui.TableNextColumn(ctx)
            ImGui.Text(ctx, entry.label)
            ImGui.TableNextColumn(ctx)
            local command = actions.command_for(entry.id)
            if command then
                ImGui.SetNextItemWidth(ctx, -1)
                -- An input rather than a label: it can be selected and copied,
                -- which plain text cannot.
                ImGui.InputText(ctx, "##cmd" .. entry.id, command)
            else
                push_colour(ImGui.Col_Text, COL_DIM)
                ImGui.Text(ctx, "not registered")
                pop_colour()
            end
        end
        end_table()
    end
end

-- --- frame -----------------------------------------------------------------

local function header()
    if ST.ctx then
        push_colour(ImGui.Col_Text, COL_ACTIVE)
        ImGui.Text(ctx, string.format("%s  >  %s",
            ST.ctx.track_name ~= "" and ST.ctx.track_name
                or ("Track " .. ST.ctx.track_index),
            ST.ctx.name))
        pop_colour()
    else
        push_colour(ImGui.Col_Text, COL_DIM)
        ImGui.Text(ctx, "no plugin focused -- encoders are inactive")
        pop_colour()
    end
end

-- --- fader tab -------------------------------------------------------------

-- What each mode is, in the terms the person turning it on cares about. Short
-- enough to sit under the radio buttons without the tab needing to be tall.
local MODE_HELP = {
    absolute = "The track jumps to wherever the fader is. One to one, "
            .. "always -- so a track change moves the volume as soon as you "
            .. "touch the fader.",
    pickup   = "The fader does nothing until it crosses the track's current "
            .. "volume, then takes over. Nothing jumps; you have to find the "
            .. "level first.",
    relative = "Every move nudges the volume by how far you moved, so the "
            .. "two faders are never aligned. Run out of travel and output "
            .. "pauses -- walk the fader back to the middle and carry on. "
            .. "Unless the track is already at that end too, in which case "
            .. "they do agree and it just works.",
}

local MODE_LABEL = {absolute = "Absolute", pickup = "Soft pickup",
                    relative = "Relative"}

local function fader_readout(sel)
    -- dB, not the 0..1 position: the position is what the wire needs and no
    -- one reads a mix in it. The bar still shows the position, so the two
    -- together say the same thing REAPER's own track panel does.
    ImGui.Text(ctx, string.format("%s   %s",
        sel.name ~= "" and sel.name or ("Track " .. sel.index),
        volume.slider_to_text(sel.volume)))
    ImGui.SameLine(ctx)
    ImGui.ProgressBar(ctx, sel.volume, -1, 0, "")
end

-- Apply while dragging, write on release.
--
-- The value has to reach state.json live or the slider cannot be judged with
-- the fader in your hand, which is the only way any of these three get chosen.
-- mappings.json is a different matter: saving per frame would rewrite nine
-- kilobytes thirty times a second for one drag. Same split the label field
-- already makes, for the same reason.
local function fader_slider(widget, label, key, value, lo, hi, fmt, help)
    ImGui.SetNextItemWidth(ctx, 200)
    local changed, picked = widget(ctx, label, value, lo, hi, fmt)
    if changed then mapping.set_fader(ST.data, key, picked) end
    if opt("IsItemDeactivatedAfterEdit")
       and ImGui.IsItemDeactivatedAfterEdit(ctx) then
        save()
    end
    set_tooltip(help)
end

local function fader_tab(sel)
    local fader = mapping.fader(ST.data)

    ImGui.SeparatorText(ctx, "Selected track")
    fader_readout(sel)

    ImGui.SeparatorText(ctx, "Mode")
    for i, mode in ipairs(mapping.FADER_MODES) do
        if i > 1 then ImGui.SameLine(ctx) end
        if ImGui.RadioButton(ctx, MODE_LABEL[mode], fader.mode == mode) then
            if mapping.set_fader(ST.data, "mode", mode) then
                save()
                ST.status = "fader: " .. MODE_LABEL[mode]
            end
        end
    end
    push_colour(ImGui.Col_Text, COL_DIM)
    ImGui.TextWrapped(ctx, MODE_HELP[fader.mode] or "")
    pop_colour()

    -- Relative is the only mode with anything to tune, and disabling rather
    -- than hiding keeps the tab from changing height as the mode changes --
    -- the same reason the Display tab pins its row height.
    ImGui.SeparatorText(ctx, "Relative")
    local relative = fader.mode == "relative"
    if not relative then begin_disabled() end

    fader_slider(ImGui.SliderDouble, "Sensitivity", "sensitivity",
                 fader.sensitivity, 0.1, 4.0, "%.2fx",
                 "How far the volume moves per unit of fader travel. 1.00x is "
              .. "one full sweep per full range. Lower is finer, and runs out "
              .. "of travel less often.")

    fader_slider(ImGui.SliderInt, "End zone", "end_zone",
                 fader.end_zone, 0, 16, "%d steps",
                 "How many of the 128 fader steps at each end count as out of "
              .. "road. Reaching one pauses output until you walk the fader "
              .. "back. 0 disables the pause entirely.")

    fader_slider(ImGui.SliderDouble, "Walk-back timeout", "recal_timeout",
                 fader.recal_timeout, 0.1, 5.0, "%.2f s",
                 "A walk-back also ends the moment you reverse direction. "
              .. "This is the other way out: hold still for this long and the "
              .. "fader is live again.")

    if not relative then end_disabled() end
end

function M._frame()
    -- IsWindowFocused alone is not enough: docked in REAPER, clicking the
    -- panel focuses REAPER's main window and ImGui does not report our window
    -- as focused. Hovering is the reliable signal for a docked panel, and the
    -- grace period covers the reach from the panel to the plugin's control.
    local now = reaper.time_precise()
    local focused = ImGui.IsWindowFocused(
        ctx, opt("FocusedFlags_RootAndChildWindows") or 0)
    local hovered = ImGui.IsWindowHovered(
        ctx, opt("HoveredFlags_RootAndChildWindows") or 0)
    local item_active = ImGui.IsAnyItemActive(ctx)
    if focused or hovered or item_active then ST.touched_at = now end

    local panel_focused = focused or hovered or item_active
        or (now - (ST.touched_at or 0)) < PANEL_GRACE
        or learn.state() == learn.ARMED

    ST.ctx = focus.poll(panel_focused)

    if ST.ctx then
        local entry = mapping.ensure(ST.data, ST.ctx.ident, ST.ctx.name)
        ST.layers = mapping.resolve(entry, ST.ctx.track, ST.ctx.fx)
    else
        ST.layers = nil
        learn.cancel()
    end

    local captured = learn.poll(ST.ctx)
    if captured then
        -- Re-pointing a slot at the SAME parameter keeps everything you have
        -- tuned about it. Pointing it at a DIFFERENT one starts clean: a
        -- label reading "Threshold" while driving "Attack" would be worse
        -- than no label, and a taper chosen for a frequency control is
        -- meaningless on a ratio.
        local prev = mapping.stored_slot(ST.data, ST.ctx.ident, captured.layer,
                                         captured.kind, captured.index)
        local same = prev ~= nil and prev.param == captured.param

        local label, taper, sensitivity
        if same then
            local kept = prev.label
            -- Seed the label so it is there to be shortened rather than typed
            -- out, but never over one the user has written.
            label = (kept and kept ~= "") and kept or captured.name
            taper, sensitivity = prev.taper, prev.sensitivity
        else
            label = captured.name
            taper, sensitivity = nil, nil   -- linear, 1x
        end

        mapping.set_slot(ST.data, ST.ctx.ident, ST.ctx.name,
                         captured.layer, captured.kind, captured.index,
                         {param = captured.param, name = captured.name,
                          label = label, taper = taper,
                          sensitivity = sensitivity,
                          mode = captured.kind == "buttons" and "toggle" or nil})
        save()
        ST.status = string.format("%s %d -> %s",
            captured.kind == "encoders" and "encoder" or "button",
            captured.index, captured.name)
        ST.layers = mapping.resolve(
            mapping.plugin(ST.data, ST.ctx.ident), ST.ctx.track, ST.ctx.fx)
    end

    local sel = selected_track()
    statefile.publish(ST.ctx, ST.layers, sel, ST.data.reserved, ST.data.fader)

    if begin_tab_bar("tabs") then
        if begin_tab_item("Surface") then
            header()
            ImGui.Separator(ctx)
            layer_block("A")
            layer_block("B")
            end_tab_item()
        end
        if begin_tab_item("Reserved") then
            reserved_tab()
            end_tab_item()
        end
        -- The fader's live readout lives here rather than under the Surface
        -- tab's slots: it is the one control that is not per-plugin, and the
        -- mode it is in is the thing you most want to see while wondering why
        -- it is not doing what you expect.
        if begin_tab_item("Fader") then
            fader_tab(sel)
            end_tab_item()
        end
        if begin_tab_item("Display") then
            display_tab()
            end_tab_item()
        end
        end_tab_bar()
    end

    ImGui.Separator(ctx)
    push_colour(ImGui.Col_Text, COL_DIM)
    ImGui.Text(ctx, ST.status)
    pop_colour()

    unwind()
    return true
end

function M._init(imgui, dir)
    ImGui, script_dir = imgui, dir
    WIN_FLAGS = 0
    TABLE_FLAGS = (opt("TableFlags_Borders") or 0)
                | (opt("TableFlags_RowBg") or 0)
                | (opt("TableFlags_SizingFixedFit") or 0)
    -- Equal-width columns: the display is meant to read as a picture of the
    -- device, so cell 5 must sit under knob 5. Content-sized columns put them
    -- wherever the parameter names happened to fall.
    GRID_FLAGS = (opt("TableFlags_Borders") or 0)
               | (opt("TableFlags_SizingStretchSame") or 0)
    COL_FIXED = opt("TableColumnFlags_WidthFixed") or 0
    COL_STRETCH = opt("TableColumnFlags_WidthStretch") or 0

    ctx = ImGui.CreateContext("X-Touch Mini")

    -- Bigger type for the Display tab, which is meant to be read at a glance
    -- from across the room rather than studied.
    local ok, font = pcall(ImGui.CreateFont, "sans-serif", DISPLAY_FONT)
    if ok and font then
        if pcall(ImGui.Attach, ctx, font) then FONT_BIG = font end
    end

    actions.reset()
    actions.resolve(script_dir)
    focus.reset()
    learn.reset()
    statefile.reset()
    reload()
    if actions.fill_labels(ST.data.reserved) then save() end
    return ST
end

function M.start(imgui, dir)
    M._init(imgui, dir)

    local function loop()
        ImGui.SetNextWindowSize(ctx, 760, 620, ImGui.Cond_FirstUseEver)
        local visible, open = ImGui.Begin(ctx, "X-Touch Mini", true, WIN_FLAGS)
        if visible then
            local ok, err = pcall(M._frame)
            if not ok then
                unwind()
                ST.err = tostring(err)
                ImGui.TextColored(ctx, COL_RED, ST.err)
            end
            ImGui.End(ctx)
        end
        if open and not ST.close then reaper.defer(loop) end
    end

    reaper.defer(loop)
end

-- Exposed for tests: the combo strings, which are easy to get wrong.
M._menus = function()
    return {taper = TAPER_ITEMS, taper_values = TAPER_VALUES,
            sens = SENS_ITEMS, sens_values = SENS_VALUES}
end

M._state = function() return ST end
M._depth = function() return depth end

return M
