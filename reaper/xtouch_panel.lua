-- X-Touch Mini control panel. Dock this next to your FX windows.
--
-- Install: ln -sfn "$PWD/reaper" ~/.config/REAPER/Scripts/XtouchMini
-- then add this file as an action.

local src = debug.getinfo(1, "S").source:match("^@(.+)$")
local script_dir = src:match("^(.*[/\\])")

if not reaper.ImGui_GetBuiltinPath then
    reaper.MB("This script requires ReaImGui. Install it from ReaPack.",
              "X-Touch Mini", 0)
    return
end

package.path = script_dir .. "?.lua;"
            .. reaper.ImGui_GetBuiltinPath() .. "/?.lua;"
            .. package.path

local ImGui = require "imgui" "0.9"
local UI = require "xt.ui"

UI.start(ImGui, script_dir)
