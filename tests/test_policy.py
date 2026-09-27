import tempfile
import unittest
from pathlib import Path

from omni.policy import ALLOW, ASK, DENY, Policy, split_commands, unwrap_shell


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "Downloads").mkdir()
        (self.home / "notes.txt").write_text("keep")
        self.data = self.home / ".local/share/omni"
        self.policy = Policy(safe_roots=[self.home / "Downloads", Path("/tmp")], protected=[self.data], home=self.home)

    def decide(self, command, cwd=None):
        return self.policy.command(f"/bin/zsh -lc '{command}'", str(cwd or self.home)).action

    def test_ordinary_commands_are_allowed(self):
        for command in ("ls -la", "git status", "git commit -m wip", "omarchy-theme-set tokyo-night",
                        "hyprctl clients -j", "cat ~/.ssh/config", "systemctl --user status omnid", "pacman -Qi foot",
                        "echo hi > /tmp/x", "mkdir -p ~/projects/new", "rm ~/Downloads/old.zip", "rm -rf /tmp/build"):
            self.assertEqual(self.decide(command), ALLOW, command)

    def test_consequential_commands_ask(self):
        for command in ("sudo pacman -Syu", "yay -S spotify", "omarchy-pkg-add obs", "git push origin main",
                        "git reset --hard HEAD~1", "git clean -fdx", "rm -rf ~/Documents", "rm notes.txt",
                        "systemctl --user restart pipewire", "reboot", "find . -name x -delete",
                        "ls && sudo rm -rf /", "echo hi > ~/notes.txt", "xargs rm < list.txt", "env FOO=1 sudo ls"):
            self.assertEqual(self.decide(command), ASK, command)

    def test_deleting_inside_the_working_directory_is_allowed(self):
        project = self.home / "project"
        project.mkdir()
        self.assertEqual(self.decide("rm -rf build dist", cwd=project), ALLOW)
        self.assertEqual(self.decide("rm -rf ../notes.txt", cwd=project), ASK)

    def test_omni_state_is_never_changed_by_shell(self):
        self.assertEqual(self.decide(f"rm -rf {self.data}"), DENY)
        self.assertEqual(self.decide(f"sqlite3 {self.data}/episodes.sqlite \"delete from episodes\""), DENY)
        self.assertEqual(self.decide("systemctl --user stop omnid"), DENY)
        self.assertEqual(self.decide(f"cat {self.data}/memory/USER.md"), ALLOW)

    def test_nested_shells_are_unwrapped(self):
        self.assertEqual(self.policy.command('bash -c "sudo ls"').action, ASK)

    def test_unparsable_commands_fall_back_to_keyword_scan(self):
        self.assertEqual(self.policy.command("echo $(sudo whoami)").action, ASK)
        self.assertEqual(self.policy.command("echo $(date)").action, ALLOW)

    def test_file_changes(self):
        project = self.home / "project"
        project.mkdir()
        allow = self.policy.file_change([{"path": str(project / "a.py"), "kind": {"type": "update"}}], str(project))
        self.assertEqual(allow.action, ALLOW)
        outside = self.policy.file_change([{"path": str(self.home / "notes.txt"), "kind": {"type": "update"}}], str(project))
        self.assertEqual(outside.action, ASK)
        new = self.policy.file_change([{"path": str(self.home / "new.txt"), "kind": {"type": "add"}}], str(project))
        self.assertEqual(new.action, ALLOW)
        own = self.policy.file_change([{"path": str(self.data / "memory/USER.md"), "kind": "update"}], str(project))
        self.assertEqual(own.action, DENY)

    def test_policy_file_extends_and_relaxes(self):
        path = self.home / "policy.toml"
        path.write_text('ask = ["docker"]\nallow = ["systemctl"]\nsafe_roots = ["~/scratch"]\n')
        policy = Policy.load(path)
        self.assertIn("docker", policy.ask_commands)
        self.assertNotIn("systemctl", policy.ask_commands)

    def test_parsing_helpers(self):
        self.assertEqual(unwrap_shell("/bin/zsh -lc 'ls -la'"), "ls -la")
        commands = list(split_commands("cd /tmp && rm -f a > log.txt; echo done | tee out"))
        self.assertEqual([argv for argv, _ in commands], [["cd", "/tmp"], ["rm", "-f", "a"], ["echo", "done"], ["tee", "out"]])
        self.assertEqual(commands[1][1], ["log.txt"])


if __name__ == "__main__":
    unittest.main()
