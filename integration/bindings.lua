-- Add these bindings after installing the user launchers.
-- Check for conflicts and unbind existing shortcuts before replacing them.
o.bind("SUPER + H", "Omi", os.getenv("HOME") .. "/.local/bin/omi")
o.bind("SUPER + SHIFT + H", "Omi voice command", os.getenv("HOME") .. "/.local/bin/omi-voice toggle")
o.bind("SUPER + ALT + H", "Omi continuous listening", os.getenv("HOME") .. "/.local/bin/omi-voice continuous toggle")
-- Replace Omarchy's Voxtype toggle with a guard that keeps dictation separate from Omi recording.
hl.unbind("SUPER + CTRL + X")
o.bind("SUPER + CTRL + X", "Toggle dictation", os.getenv("HOME") .. "/.local/bin/omi-dictate")
