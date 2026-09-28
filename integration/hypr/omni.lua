-- Omni keybindings and window rules. install.py copies this to ~/.config/hypr/omni.lua;
-- load it by adding `require("hypr.omni")` to ~/.config/hypr/hyprland.lua.
-- Check `omarchy menu keybindings --print` for conflicts first. If you installed the old Omi
-- bindings (including its SUPER + CTRL + X dictation guard), remove them from hypr/bindings.lua.

local bin = os.getenv("HOME") .. "/.local/bin/"

-- Quick ask: a D-Bus nudge to the resident app is instant; fall back to starting it.
o.bind("SUPER + H", "Omni quick ask", "gapplication action dev.omni.Omni quick-ask || " .. bin .. "omni popover")
-- Push to talk: press to start, press again to send (or just stop talking).
o.bind("SUPER + SHIFT + H", "Talk to Omni", bin .. "omni listen")
-- Hands-free: every utterance becomes a request until toggled off.
o.bind("SUPER + ALT + H", "Omni hands-free listening", bin .. "omni voice continuous")
-- Stop speaking / interrupt the current task. Replaces Omarchy's Hardware menu binding
-- (still reachable from the Omarchy menu).
hl.unbind("SUPER + CTRL + H")
o.bind("SUPER + CTRL + H", "Stop Omni", bin .. "omni stop")

o.window({ class = "^dev\\.omni\\.Omni$", title = "^Omni Quick Ask$" }, {
  float = true,
  center = true,
  pin = true,
  size = { 680, 420 },
})
o.window({ class = "^dev\\.omni\\.Omni$", title = "^Omni$" }, { float = true, center = true, size = { 1100, 760 } })
