-- Draw real frames with the real ReaImGui.
--
-- The stub-frame suite cannot catch a function that exists under a different
-- name or takes a different number of arguments in the 0.9 API -- the stub
-- happily accepts whatever it is handed. This one does not.
--
-- Result goes to a file because reascript_test.py reports success as soon as
-- the file finishes LOADING, before any frame has been drawn.
--   python3 tools/run_panel_test.py

local here = debug.getinfo(1, "S").source:match("^@(.*[/\\])")
package.path = here .. "../?.lua;"
            .. reaper.ImGui_GetBuiltinPath() .. "/?.lua;"
            .. package.path

local RESULT = (os.getenv("TMPDIR") or "/tmp") .. "/xtouchmini-panel-result.txt"

local function report(text)
    local f = io.open(RESULT, "w")
    if f then f:write(text .. "\n"); f:close() end
end

local ok_shim, ImGui = pcall(function() return require "imgui" "0.9" end)
if not ok_shim then
    report("RESULT FAIL\nimgui shim: " .. tostring(ImGui))
    return
end

-- Never write the real mapping or state files from a test.
local paths = require "xt.paths"
paths.write_atomic = function() return true end

local UI = require "xt.ui"

local ok_init, err_init = pcall(UI._init, ImGui, here .. "../")
if not ok_init then
    report("RESULT FAIL\n_init: " .. tostring(err_init))
    return
end

local ctx = ImGui.CreateContext("XT Panel Test")

-- The Display tab uses a larger font, but ImGui only draws the tab that is
-- selected, so a plain frame loop never reaches PushFont. Exercise the font
-- path directly against the real API -- signatures differ between ReaImGui
-- versions and the panel guards it with pcall, which would hide a failure.
local font_note = ""
do
    local ok, font = pcall(ImGui.CreateFont, "sans-serif", 20)
    if not (ok and font) then
        report("RESULT FAIL\nCreateFont: " .. tostring(font))
        return
    end
    local attached, err = pcall(ImGui.Attach, ctx, font)
    if not attached then
        report("RESULT FAIL\nAttach: " .. tostring(err))
        return
    end
    FONT_PROBE = font
    font_note = " (font created and attached)"
end
local frames, MAX_FRAMES = 0, 6
local failure = nil

local function loop()
    frames = frames + 1
    local visible = ImGui.Begin(ctx, "XT Panel Test", true)
    if visible then
        local ok, err = pcall(UI._frame)
        if not ok and not failure then
            failure = tostring(err)
        end
        -- Same problem as the font, same answer: ImGui draws only the
        -- SELECTED tab, so a plain frame loop never enters the Fader tab and
        -- never touches the widgets that are unique to it. Call them here,
        -- with the exact signatures xt.ui uses, so a name or arity that is
        -- wrong for this ReaImGui fails the suite instead of waiting for
        -- someone to click the tab.
        if not failure then
            local ok_w, werr = pcall(function()
                ImGui.RadioButton(ctx, "probe", true)
                ImGui.TextWrapped(ctx, "probe")
                ImGui.SetNextItemWidth(ctx, 200)
                ImGui.SliderDouble(ctx, "probe d", 1.0, 0.1, 4.0, "%.2fx")
                ImGui.SetNextItemWidth(ctx, 200)
                ImGui.SliderInt(ctx, "probe i", 3, 0, 16, "%d steps")
                if ImGui.IsItemHovered(ctx) then
                    ImGui.SetTooltip(ctx, "probe")
                end
                ImGui.BeginDisabled(ctx, true)
                ImGui.SliderDouble(ctx, "probe t", 1.0, 0.1, 5.0, "%.2f s")
                ImGui.EndDisabled(ctx)
            end)
            if not ok_w then failure = "fader widgets: " .. tostring(werr) end
        end

        -- Push and pop the font inside a real frame, which is the only place
        -- ImGui accepts it.
        if not failure and FONT_PROBE then
            local pushed, perr = pcall(ImGui.PushFont, ctx, FONT_PROBE)
            if pushed then
                ImGui.Text(ctx, "font probe")
                local popped, qerr = pcall(ImGui.PopFont, ctx)
                if not popped then failure = "PopFont: " .. tostring(qerr) end
            else
                failure = "PushFont: " .. tostring(perr)
            end
        end
        ImGui.End(ctx)
    end

    if failure then
        report("RESULT FAIL\nframe " .. frames .. ": " .. failure)
        return
    end
    if frames >= MAX_FRAMES then
        local depth = UI._depth()
        if depth.colour ~= 0 or depth.disabled ~= 0 then
            report(string.format(
                "RESULT FAIL\nunbalanced stacks: colour=%d disabled=%d",
                depth.colour, depth.disabled))
        else
            report("RESULT OK\n" .. frames .. " frames drawn" .. font_note
                   .. " (fader widgets probed)")
        end
        return
    end
    reaper.defer(loop)
end

os.remove(RESULT)
reaper.defer(loop)
